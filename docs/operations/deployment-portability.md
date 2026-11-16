# Portable deployment profiles

Aurora Studio is designed to run from one backend container image with separate
runtime processes. Hosting-provider configuration must not leak into application
business logic.

## Runtime contract

Build the image from the repository root:

```bash
docker build -f infrastructure/docker/backend.Dockerfile -t aurora-studio-api:local .
```

Run the API process:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Run a continuously supervised email worker:

```bash
python -m app.workers.run_email_outbox
```

For a scheduled invocation, use bounded drain mode:

```bash
python -m app.workers.run_email_outbox --drain --max-runtime-seconds 240
```

Drain mode claims only currently available messages, processes each claimed batch,
and exits when no immediately available work remains or the runtime limit is
reached. In-flight deliveries are allowed to finish and have their own configured
delivery timeout. Overlapping invocations are safe at the claim layer because
PostgreSQL row locks with SKIP LOCKED prevent two workers claiming the same row.

The API and worker use the same image, Python package, environment contract and
database schema. They are separate processes and must be deployed as separate
services. Do not start the worker inside FastAPI lifespan: doing so would couple
worker availability to API replica count and lifecycle.

## Deployment profiles

### Free / demonstration

A free profile is best-effort and quota-limited, not an availability commitment.
A possible provider combination is static frontend hosting, a scale-to-zero
container API, hosted PostgreSQL, object storage, and a scheduled worker invocation.
Scheduled delivery is less responsive than a continuously running worker and
must use the same PostgreSQL claim/lease/idempotency safeguards.

Check provider billing requirements, idle suspension, database sleep/expiry,
backup policy, execution limits and egress charges before onboarding a client.

### Starter / production

Use an always-on API service and a separately supervised, continuously running
worker service. Use managed PostgreSQL with automated backups and a restore
procedure. Add managed Redis/Valkey only for capabilities that require it;
PostgreSQL remains the durable source of truth for the email outbox.

### Enterprise

Support dedicated accounts/projects, private networking, isolated databases,
centralized secrets, audit logging, defined recovery objectives and independently
scaled worker pools. These are deployment choices, not separate application forks.

## Portability requirements

- PostgreSQL is the durable system of record; use migrations for schema changes.
- Use environment variables or a platform secret manager for configuration.
- Never bake credentials, .env files, tokens or customer data into the image.
- Use S3-compatible object storage through an application-owned adapter.
- Keep email delivery behind the existing EmailSender interface.
- Keep worker entry points independent of the hosting provider.
- Use structured logs on stdout/stderr and platform-neutral health/metrics signals.
- Pin and review runtime/base-image updates; scan images before production release.

## Current operational limitations

The email worker now supports continuous polling, bounded drain execution,
graceful SIGTERM/SIGINT stop requests, atomic PostgreSQL claims, lease renewal/
recovery, retry classification and provider idempotency. A stop request prevents
new claims and lets the active batch finish within its per-delivery timeout.

This is not yet a complete production operations system. Before advertising
production-grade delivery, add and test:
- host-specific alerts for missing heartbeats, growing backlog and permanent failures;
- export queue/worker telemetry to a durable metrics and monitoring system if
  required beyond structured log-based monitoring;
- backup/restore and provider-outage runbooks;
- a staging deployment with end-to-end verification.

The worker heartbeat and queue-age snapshot are implemented. The integrated
GitHub Actions runtime smoke test applies migrations, starts PostgreSQL, Redis,
API and worker, verifies API readiness and observes the worker heartbeat. These
checks do not replace a user-local smoke test, staging deployment or production
monitoring configuration.

Do not report the API as email-worker-ready merely because the API and database
are healthy. API readiness should describe dependencies required to serve API
requests; worker health and queue health must be monitored separately.

## Secrets and environment

Provide production secrets through the hosting platform's secret manager. At
minimum, configure the database URL, JWT secret, rate-limit identity secret,
email outbox keyring and active key version, Resend API key and verified sender
address. If YouTube OAuth is enabled, configure its independent integration-token
keyring and active version. Legacy single-key variables remain supported only as
version-1 compatibility fallbacks. Configure Redis for the shared rate limiter.
Never commit .env.

For production, use a verified sending domain with the email provider. Provider
sandbox sender addresses are intended for testing and may restrict recipients.

## Release and recovery

1. Build one immutable image and deploy that exact image to API and worker.
2. Apply database migrations as a controlled release step, not independently in
   every API or worker replica.
3. Start/roll out API and worker services independently.
4. Verify API liveness/readiness, worker process logs, queue age and a real
   end-to-end email delivery.
5. Roll back the image if application health regresses; use backward-compatible
   expand/contract migrations for schema changes.
6. Test PostgreSQL backup restoration periodically.

This document describes the portability contract. Provider-specific deployment
manifests should be added only after the target provider and its current limits
have been verified.


## Email outbox operational visibility

The continuous email worker emits a privacy-safe queue heartbeat at the configured
`EMAIL_OUTBOX_HEARTBEAT_INTERVAL_SECONDS` interval (default: 60 seconds). Each
heartbeat reports pending, immediately available pending, and processing counts,
plus the age of the oldest immediately available pending message and oldest
active processing lease. Lifetime sent/failed totals are deliberately excluded so
heartbeat work does not scan unbounded delivery history. It does not log recipient
addresses, message bodies, verification tokens or provider credentials.

Configure hosting-platform alerts for a missing heartbeat, sustained
available-pending backlog, excessive pending age, stale processing leases, and
permanent-failure events in sanitized worker logs. Lifetime sent/failed counts
are not emitted by the periodic heartbeat.
These structured logs provide observability, not a durable metrics store or
built-in alerting service; alert delivery and retention depend on the host.


## Research worker operations

The research worker is a separate process from the API. It is opt-in in the
local Compose configuration through the `app` profile and is not started by
the default Compose invocation.

Start it explicitly only when the environment is ready to process research
jobs:

```bash
docker compose --profile app up -d research-worker
```

The worker currently uses the deterministic mock research provider. It must
not be treated as a production research integration until real provider
adapters, their credentials, and their operational limits have been reviewed.

URL normalization in this foundation is canonicalization only; it is not an
SSRF defense and it does not fetch or validate remote hosts. Before adding any
HTTP retrieval provider, implement and test outbound URL controls: allow only
HTTP(S), resolve and reject loopback/private/link-local/multicast/reserved IP
ranges (including IPv4-mapped IPv6), revalidate every redirect target and DNS
resolution to mitigate rebinding, block access to cloud metadata endpoints,
apply connection/read/total timeouts and response-size limits, and enforce
egress restrictions at the network layer. Do not rely on URL parsing alone.

### Enqueue rate limits

Research-run creation is protected by the shared Redis fixed-window limiter.
The defaults are 20 enqueue requests per user per hour and 100 per organization
per hour. Both limits must allow a request; a rejected request returns HTTP 429
with a `Retry-After` header. Limits are configurable with
`RESEARCH_RUN_USER_LIMIT` and `RESEARCH_RUN_ORGANIZATION_LIMIT`. If Redis is
unavailable, the endpoint fails closed with HTTP 503 rather than accepting
unthrottled queue writes.

### Queue, claims and leases

PostgreSQL is the durable queue and source of truth for research runs. Workers
claim eligible runs in short transactions using row locks with `SKIP LOCKED`,
so concurrent workers do not claim the same available run.

A claim records an attempt number, a unique lease token and an expiry time.
The worker periodically renews its lease while research is running. Writes
that complete a run are conditional on the worker still owning a valid lease.
This prevents a worker whose lease has expired from completing work after
another worker has recovered the run.

Expired leases are recovered by a later worker. Runs below the configured
maximum attempt count are scheduled for retry using capped exponential
backoff. Runs that exhaust their attempts are marked failed. Recovery requires
a worker to be running; an expired lease does not itself start another worker.

### Retry and timeout configuration

The research worker configuration is controlled by environment settings.
Current defaults include:

| Setting | Default |
|---|---:|
| Batch size | 5 |
| Maximum concurrency | 3 |
| Poll interval | 5 seconds |
| Lease duration | 300 seconds |
| Heartbeat interval | 30 seconds |
| Provider timeout | 120 seconds |
| Maximum attempts | 5 |
| Initial retry delay | 30 seconds |
| Maximum retry delay | 1,800 seconds |
| Shutdown grace period | 120 seconds |

Tune these values against the actual provider's latency, rate limits, database
capacity and hosting platform execution limits. The heartbeat interval must
remain shorter than the lease duration.

### Shutdown contract

On SIGTERM or SIGINT, the worker stops claiming new runs and allows its
currently claimed batch to finish within the configured application shutdown
grace period. When that deadline is reached, outstanding tasks are cancelled
and the worker makes a bounded cleanup attempt. A run interrupted before its
completion is eligible for recovery after its lease expires.

The current local Compose service has a Docker `stop_grace_period` of
180 seconds. This is longer than the default 120-second application shutdown
period, leaving up to 60 seconds for process and container cleanup before
Docker's final termination boundary. If the process does not exit before that
boundary, Docker may forcibly terminate the container.

The worker entrypoint uses a process-owned event loop with a bounded final
cleanup window instead of `asyncio.run()`. This is intentional: `asyncio.run()`
waits for all pending tasks during loop shutdown and can hang if a provider
coroutine suppresses cancellation. After the application shutdown grace period
and the final bounded cleanup window, the worker closes its event loop and
exits. If cancellation-resistant tasks remain, the entrypoint uses a final
non-returning process exit after flushing logs; this also terminates executor
threads that normal Python interpreter shutdown could otherwise wait for. The
uncompleted database lease is recovered by a later worker.

The same fail-stop rule applies when a provider exceeds its execution timeout
and then ignores cancellation: the worker does not record a retry while that
provider task may still be running. It propagates a fatal worker error, exits
the process, and leaves the lease for normal expiry/recovery. This prevents
overlapping attempts inside a still-running worker process.

This is a process-level termination boundary, not proof that an external
service has rolled back a request already received. Provider calls must still
use bounded network timeouts, cancellation-aware clients and idempotency keys
where the upstream service supports them. Queue execution is at-least-once:
a process can die after an upstream side effect but before persisting completion.
Monitor expired leases and repeated retries so abandoned work does not remain
unnoticed.

The lifecycle regression test launches a separate Python process with a
deliberately cancellation-resistant task and verifies that the worker runner
exits within its bounded cleanup window. Repeat this test against the actual
provider adapter and target hosting platform before enabling live retrieval.
