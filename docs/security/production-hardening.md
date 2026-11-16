# Production Security Hardening

This document records deployment controls required before exposing Aurora Studio's API to untrusted traffic. It distinguishes application controls from controls that must be enforced by the deployment platform.

## 1. Require HTTPS for production application and OAuth URLs

Production configuration must use an HTTPS `FRONTEND_URL` with no credentials, path, query, or fragment. When YouTube OAuth is enabled, `YOUTUBE_REDIRECT_URI` must use HTTPS and `YOUTUBE_FRONTEND_RESULT_URL` must use the configured frontend's exact HTTPS origin. These checks are enforced by application startup configuration validation. Register the exact redirect URI with the Google OAuth client configuration. The application cannot verify the TLS configuration of the hosting platform; validate certificate issuance, renewal, and HTTPS redirects at the deployment edge.

## 2. Disable debug mode in production

The API must run with `DEBUG=false` in production. Debug mode can expose detailed exception information and must never be enabled on an internet-facing deployment. Application startup validation rejects `APP_ENV=production` with `DEBUG=true`. Set the production environment variable explicitly to false rather than relying on a development example file.

## 3. Redact OAuth callback query strings from access logs

The YouTube OAuth callback receives short-lived authorization parameters in its query string.

- The callback redirect response sets `Cache-Control: no-store`, `Pragma: no-cache`, and `Referrer-Policy: no-referrer`. These headers supplement, but do not replace, query-string protection in access logs.
- The backend Docker image and the Compose API service run Uvicorn with `--no-access-log` by default, preventing Uvicorn access logs from recording OAuth callback request targets. If a deployment overrides the container command, preserve this setting unless an alternative redaction configuration has been tested.
- Apply the same rule at the reverse proxy, ingress, load balancer, and APM layer. Do not record query strings for the OAuth callback route.
- Keep application logs focused on event names and non-sensitive correlation IDs. Never log OAuth codes, state values, access tokens, refresh tokens, authorization headers, or full callback URLs.
- Restrict access to existing logs and apply an appropriate retention period.

Example standalone command:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
```

For Nginx, use a log format that records the normalized URI path (`$uri`) rather than the original request line (`$request`) or request URI (`$request_uri`). This avoids logging query arguments for every route:

```nginx
log_format aurora_safe
    '$remote_addr - $remote_user [$time_local] '
    '"$request_method $uri $server_protocol" '
    '$status $body_bytes_sent "$http_referer" "$http_user_agent"';

access_log /var/log/nginx/aurora-access.log aurora_safe;
```

Do not add `$args`, `$query_string`, `$request`, or `$request_uri` to this format. Validate the effective configuration and inspect a test log entry for the OAuth callback before production. Apply equivalent redaction in any upstream ingress, CDN, load balancer, tracing, or APM service.

The command and Nginx snippet are examples; production binding, TLS termination, trusted proxy configuration, and process supervision must be configured for the target environment.

## 4. Add shared login and account-endpoint rate limiting

The API implements shared Redis-backed fixed-window rate limiting. Do not replace it with a process-local in-memory counter: that resets on restart and does not coordinate multiple API workers or replicas. The current application limits cover:

- `POST /api/v1/auth/login` — fixed-window limits by source IP and normalized account identifier.
- `POST /api/v1/auth/register` — fixed-window limit for automated account creation.
- `POST /api/v1/auth/email-verification/resend` — retain the account-level cooldown/daily limit and add source-IP throttling.

Return HTTP 429 with a `Retry-After` header. Avoid permanent account lockouts that allow attackers to deny service to known users. Trust forwarded client IP headers only when they are set by a controlled proxy that strips untrusted incoming values.

The shared limiter returns HTTP 429 with `Retry-After` when a limit is exceeded and fails closed with HTTP 503 if Redis is unavailable. Current configurable defaults are 30 login attempts per source IP/minute, 10 login attempts per normalized account/minute, and 10 registration or verification-resend attempts per source IP/hour. Tune these thresholds against expected traffic and operational telemetry before public launch. Configure Uvicorn trusted-proxy handling only for known proxy addresses; the application deliberately does not trust arbitrary `X-Forwarded-For` headers. The limiter uses HMAC-SHA256 pseudonyms for account/IP identifiers. Production must supply a dedicated random `RATE_LIMIT_IDENTITY_SECRET` of at least 32 bytes. Do not reuse the JWT or encryption keys.

## 5. Define the CORS and origin deployment boundary

The FastAPI application currently does not install CORS middleware or expose an allowed-origin setting.

Preferred initial deployment: serve the frontend and API behind one trusted origin and route `/api` to the backend using a reverse proxy. The Vite development server already proxies `/api` to the backend, so local development does not require permissive CORS. If separate production origins are required, add explicit CORS configuration:

- Use a finite allowlist of exact frontend origins.
- Do not combine wildcard origins with credentialed requests.
- Permit only required methods and headers.
- Keep the allowlist environment-specific and review it as part of deployment configuration.

CORS is a browser policy, not authentication or authorization; API endpoints must continue to enforce their own access controls.

## 6. Version and rotate encryption keys safely

Email-outbox payloads and YouTube refresh tokens now support independent versioned keyrings. New writes use the configured active key version; decryption uses the version persisted with each record. Existing single-key environment variables remain supported as version 1 for backward compatibility.

The bounded rotation command is implemented in `app.services.encryption_key_rotation`. It processes email and YouTube records in separate transactions, uses row locks, updates ciphertext and key version atomically, and can be safely rerun after interruption. It fails closed if an old key is missing or ciphertext authentication fails. The application never rotates keys automatically at startup. The CLI defaults to the email domain; operators must explicitly select `--domain youtube` or `--domain all` when those keyrings are configured.

See the [Encryption Keyring and Rotation Runbook](encryption-key-rotation.md) for configuration, deployment ordering, commands, verification, backup retention, rollback, and compromise procedures.

Production operators must still:
- back up the database and preserve the corresponding old keys before changing active versions;
- deploy keyring-capable code to every API and worker instance before enabling a new active key;
- retain every old key needed by live records and recoverable backups;
- run and verify rotation separately for each configured domain;
- test database restore with its matching keyring before retiring old keys.

Email-outbox and integration-token keys are separate domains and must never be reused. No live key has been rotated as part of this code change.

## 7. Email provider failure policy

The email outbox worker classifies provider failures using explicit retryability:

- Retry transient network failures, timeouts, HTTP 408, HTTP 429, and provider 5xx responses using bounded exponential backoff.
- Treat permanent request/authentication failures (for example, invalid payload or invalid API credentials) as non-retryable and move the message to a terminal failure state with a sanitized error code.
- Preserve idempotency keys and avoid logging response bodies that may contain personal data or provider details.

This behavior is implemented in the email provider and outbox worker. Continue to validate provider-specific status/error mappings against the provider's current API contract.

## 8. Treat browser bearer-token storage as a production risk

The browser application uses opaque server-managed sessions rather than storing the access JWT in JavaScript-readable `sessionStorage`. The session cookie is `HttpOnly` and uses `SameSite=Lax`; HTTPS frontend configuration enables the `Secure` cookie attribute. State-changing requests are protected with a CSRF token and Origin validation. Session records are stored server-side and enforce idle and absolute expiry.

This reduces the risk of direct session-token theft through JavaScript, but it does not make cross-site scripting harmless: injected same-origin script may still issue authenticated requests or read application data. Maintain strict XSS prevention and validate the response-header baseline below. Confirm cookie behavior, trusted-origin configuration, logout/revocation, expiry, and CSRF enforcement in integration tests for every production origin.

## 9. Set browser security response headers at the HTTPS edge

The production reverse proxy or hosting platform should set security headers on the frontend responses. A baseline to validate against the built frontend is:

```nginx
add_header Strict-Transport-Security "max-age=31536000" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "strict-origin-when-cross-origin" always;
add_header X-Frame-Options "DENY" always;
add_header Content-Security-Policy "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; img-src 'self' https://*.googleusercontent.com https://yt3.ggpht.com; connect-src 'self'; font-src 'self' https://fonts.gstatic.com; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'; upgrade-insecure-requests" always;
```

The current frontend stylesheet imports Google Fonts. Accordingly, the policy explicitly permits the Google Fonts stylesheet origin (`https://fonts.googleapis.com`) in `style-src` and the font-file origin (`https://fonts.gstatic.com`) in `font-src`. Keep these origins only while the application loads fonts from Google; self-host the font assets to remove these external allowances. Validate the final policy against the production build and browser CSP reports before enforcement.

Only enable HSTS after the entire hostname is served over HTTPS. Add `includeSubDomains` only if every subdomain is HTTPS-capable, and do not enable preload without meeting the browser preload requirements. Validate the CSP against the production build and all required integrations before enforcement; do not add `unsafe-eval` or broad wildcard sources to silence violations. If the API is deployed on a separate origin, update `connect-src` narrowly and configure CORS independently. The example assumes frontend and API share one origin.

## Release checklist

- [ ] Production frontend sends validated HSTS, CSP, clickjacking, MIME-sniffing, and referrer-policy headers.
- [x] Application authentication uses server-managed HttpOnly browser sessions with CSRF and Origin validation; validate the production origin and cookie behavior during staging.
- [x] Application startup validates production HTTPS frontend/OAuth URL constraints and rejects production debug mode.

- [ ] Production frontend and YouTube OAuth URLs use valid HTTPS origins and certificates at the deployment edge.

- [ ] Production environment explicitly sets `APP_ENV=production` and `DEBUG=false`.

- [ ] Uvicorn and proxy access logs do not record OAuth callback query strings.
- [x] Shared Redis rate limiting protects login, registration, and verification-resend endpoints; production supplies a dedicated rate-limit identity secret.
- [ ] The deployed frontend/API origin model is explicit; CORS is narrowly configured if needed.
- [x] Application supports independent versioned email/YouTube keyrings and bounded restart-safe rotation; operator runbook is documented.
- [ ] Perform rotation only after deployment approval, then verify database restore with the retained historical keyring before retiring old keys.
- [x] Email provider transient/permanent failure classification is implemented; validate provider mappings and alerting during staging.
- [ ] Production secrets are injected by a secret manager and are absent from source control and logs.


## LLM endpoint transport security

The story worker accepts HTTP endpoints for local development (for example, a Docker-network Ollama service). Production configuration requires HTTPS for every configured LLM endpoint, whether or not an API key is configured. This protects prompts and generated content in transit as well as provider credentials. For self-hosted Ollama or vLLM, place the inference service behind a TLS-terminating reverse proxy or service-mesh gateway and restrict the upstream to a private network. Do not expose an unauthenticated inference endpoint publicly. Keep API keys in the deployment secret store; never place them in source control or logs.
