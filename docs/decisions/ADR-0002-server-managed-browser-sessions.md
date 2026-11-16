# ADR-0002: Server-managed browser sessions

- Status: Accepted for implementation
- Date: 2026-11-10
- Deciders: Aurora Studio maintainers

## Context

The initial web client stores bearer access JWTs in `sessionStorage`. Any JavaScript executing in the application origin can read those credentials. The API currently validates bearer JWTs but has no server-side browser-session record, session revocation, or logout invalidation.

The application is a same-origin development setup today: Vite proxies `/api` to FastAPI. Production guidance in the security hardening document also prefers a same-origin reverse proxy. The browser session design therefore uses a single configured frontend origin and does not introduce wildcard credentialed CORS.

## Decision

Use an opaque, server-managed browser session for the first-party web client:

1. After successful browser login, generate a cryptographically random session secret with at least 256 bits of entropy.
2. Send the secret only in an `HttpOnly; Secure; SameSite=Lax; Path=/` cookie. In production, use a `__Host-` cookie name, omit `Domain`, and require HTTPS. Local development may use a non-prefixed cookie name when HTTP is in use.
3. Persist only a SHA-256 hash of the random session secret in PostgreSQL. Never persist the raw cookie secret.
4. Enforce server-side idle and absolute expiry. Revoke the database session on logout and reject sessions for inactive users.
5. For cookie-authenticated unsafe methods, require an exact configured `Origin` match and a double-submit CSRF token in a host-only cookie plus a custom request header. Use constant-time comparison. SameSite is defense in depth, not the only CSRF control.
6. Keep the existing bearer-JWT authentication path for non-browser API clients during migration. Browser authentication must not receive or persist a bearer token.
7. Do not rotate the session cookie on ordinary API requests. Create a new session at login and on future explicit reauthentication/session-renewal flows. This avoids false session-reuse detection caused by concurrent tabs while still providing server-side revocation and bounded lifetime.

## Session lifecycle

- Proposed idle timeout: 30 minutes, extended only by authenticated activity.
- Proposed absolute timeout: 12 hours from login, regardless of activity.
- Login creates a fresh session; an existing anonymous CSRF token is replaced after successful authentication.
- Logout revokes the current session in the database before clearing cookies.
- Expired, revoked, unknown, or inactive-user sessions return 401.
- Session secrets and CSRF values must never appear in URLs, logs, analytics, or error messages.

The timeout values are configuration defaults and must be validated as positive bounded values. Production cookie security is mandatory.

## API compatibility and migration

- Preserve `POST /auth/login` and bearer JWT behavior for existing non-browser clients.
- Add a browser-only login/session endpoint that authenticates credentials, creates the server-side session, sets cookies, and returns only the user profile.
- Add a session bootstrap endpoint for page reloads and a logout endpoint.
- Extend the current-user dependency to accept either a bearer JWT or a valid browser session. CSRF validation applies only when authentication is cookie-based.
- Migrate the React client in a separate reviewable change: remove all token reads/writes from `sessionStorage`, bootstrap the session on load, send credentials and CSRF headers, and call server-side logout.
- Keep YouTube OAuth credentials, connected-channel rows, organization memberships, and existing user records untouched.

## Security and operational requirements

- Require exact same-origin validation for browser session login, logout, and other cookie-authenticated unsafe methods.
- Do not trust arbitrary `X-Forwarded-For` values; proxy trust remains an explicit deployment setting.
- Return `Cache-Control: no-store` for session bootstrap/login/logout responses.
- Redact cookies, authorization headers, and OAuth query parameters from application and reverse-proxy logs.
- Do not enable credentialed cross-origin browser sessions. If a future deployment requires separate frontend/API origins, revisit the cookie, SameSite, CSRF, and CORS model explicitly rather than weakening these defaults.
- Add additive migrations only. Never delete or rewrite existing user, organization, or YouTube integration data.

## Validation plan

Backend tests must cover:
- Login success and invalid credentials.
- Session cookie flags and absence of bearer tokens in browser-login responses.
- Valid, expired, idle-expired, revoked, unknown, and inactive-user sessions.
- Logout revocation and cookie clearing.
- CSRF missing/mismatch and Origin mismatch rejection for unsafe cookie-authenticated requests.
- Bearer JWT compatibility without requiring CSRF.
- Session bootstrap after page reload.
- Session hash storage (raw cookie secret is never persisted).
- Migration upgrade/downgrade and full backend regression suite.

Frontend tests must cover:
- No `sessionStorage` or `localStorage` authentication-token usage.
- Session restoration after reload.
- CSRF bootstrap/header behavior.
- Logout and stale-request handling.
- Organization and YouTube channel flows remain functional after authentication migration.

## Consequences

The browser no longer has a JavaScript-readable long-lived bearer credential. Session revocation becomes immediate and centrally enforceable. Each authenticated browser request performs a session lookup, adding database work; indexes and bounded activity updates are required. The existing bearer path remains available until API-client usage and migration policy are explicitly reviewed.
