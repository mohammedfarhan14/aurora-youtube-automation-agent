# Worker network isolation and hosted egress contract

## Compose default

The local Compose topology applies fail-closed network separation:

- `research-worker` is attached only to the internal `research-data`
  network. PostgreSQL and Redis join that network for the worker's required
  data access. The worker is not attached to the ordinary Compose default
  network.
- `media-production-worker` is attached only to the separate internal
  `media-data` network. PostgreSQL joins that network for job and artifact
  metadata access. The media worker is not attached to the ordinary Compose
  default network.
- The API and other application services retain the default application
  network. Data services can join both the application network and their
  worker-specific internal data network.

Docker Compose `internal: true` removes ordinary external connectivity from a
bridge network. For local development, research and media workers reach public
HTTPS destinations through their own Squid proxy while remaining disconnected
from the ordinary application network. The media proxy also has one explicit
HTTP route to `host.docker.internal:8880` for the local Qwen bridge. The
YouTube publishing proxy remains limited to Google OAuth/API hosts.

## Hosted deployment requirements (not implemented by Compose)

A production platform must implement and verify network policy independently
for each worker. The Compose network declaration is not an FQDN firewall and
does not prove a hosted environment is restricted.

### Research worker

- Deny outbound traffic by default.
- Permit DNS only to the platform's trusted resolver.
- The local Compose proxy permits public TCP 443 while denying
  private/loopback/link-local/multicast/reserved destinations. This is a local
  development convenience, not an adequate hosted policy.
- For hosted deployment, permit TCP 443 only to configured source and LLM
  provider hostnames, or use a reviewed gateway that enforces that list.
- Do not allow direct general internet access, arbitrary proxy environment
  variables, private network destinations, metadata services, or alternate
  DNS resolvers.

### Media worker

- The local Compose proxy permits public TCP 443 and rejects private/reserved
  destinations. The local Qwen exception is `host.docker.internal:8880` only.
- Hosted deployment must permit only the configured visual/TTS providers and
  exact remote object-storage endpoint.
- If remote object storage is configured, allow only the exact operator-
  approved storage endpoint from the worker that needs it. Do not create a
  shared egress rule for research and media.
- Any future remote TTS/model endpoint requires its own explicit worker,
  hostname, port, and policy approval.

### Verification before enabling a live provider

1. Record the deployment platform, worker network/security-group identifiers,
   resolver, firewall or gateway policy, and immutable image digests.
2. From local research/media worker networks, verify public HTTPS works only
   through the assigned proxy, while alternate ports, private/loopback/link-
   local destinations, metadata IPs and direct IPv4/IPv6 egress fail.
3. For hosted deployment, verify exact provider allowlists and redirect/private
   address boundaries from the deployed image.
4. Confirm API, email, and story workers do not inherit research/media
   destination allowlists.
5. Re-run provider SSRF/boundary tests against the exact deployed image and
   HTTP-client dependency versions.
6. Attach command output or platform policy evidence to issue #41 before
   enabling hosted live research or Commons media retrieval.

## Important limitations

- Docker Compose network isolation is a development/runtime baseline; it
  cannot enforce hostname-specific egress rules.
- Docker's internal network does not configure a trusted DNS resolver or
  guarantee host-level firewall behavior.
- This repository change does not provision cloud firewalls, security groups,
  NAT gateways, DNS policies, or egress proxies.
- Live providers may use the local demo proxy with configured credentials and
  application URL checks. Hosted deployments must keep providers disabled
  until their narrower platform-specific controls above are verified.
