# Research retrieval threat model

## Current implementation

Aurora Studio's research worker defaults to `RESEARCH_PROVIDER=mock`. The
`wikipedia_http` provider is an explicit opt-in and currently retrieves at
most three article extracts from the English Wikipedia MediaWiki API. It is
not a general-purpose crawler or search engine.

The live provider accepts only HTTPS/HTTP URLs on the exact host
`en.wikipedia.org`. Redirects are disabled in the HTTP library and followed
manually only after URL, scheme, port, hostname and destination validation.
A redirect to any other hostname is rejected.

An additional opt-in `multi_source_searxng` mode composes this provider and
Crossref with pages discovered by an operator-configured HTTPS SearXNG
instance. Search snippets are used only to rank candidate URLs; each candidate
is separately fetched as HTTPS, exact-host-allowlisted for that request, and
revalidated by the research HTTP client and configured egress proxy. Redirects
to another host are rejected. HTML is parsed as text with active and common
navigation/advertising elements suppressed. Result pages with unknown licenses
remain review-required and are not cleared for reuse.

## SSRF controls

- URL parsing rejects non-HTTP(S) schemes, user information, malformed and
  ambiguous numeric hosts, scoped IP literals, local suffixes and nonstandard
  ports. Only ports 80 and 443 are accepted for their matching schemes.
- IPv4 and IPv6 literals are checked before any connection. Loopback, private,
  link-local, multicast, reserved, unspecified, IPv4-mapped IPv6 and known
  cloud metadata addresses are rejected.
- The custom aiohttp resolver resolves a hostname once for each new connection,
  validates every A/AAAA result, and returns only those exact numeric
  addresses to the connector. A mixed public/private answer set is rejected
  in full. The connector connects to the returned numeric address while
  retaining the URL hostname for HTTP Host, TLS SNI and certificate validation.
  DNS is not re-resolved between validation and socket connection.
- DNS caching is disabled. Redirect targets are independently validated and
  independently resolved for their new connection.
- The HTTP client does not trust proxy environment variables and does not
  automatically decompress response bodies or follow redirects.
- Connect, socket-connect, read, per-request total and whole-provider wall
  clock limits are enforced. Responses are streamed with a one-megabyte hard
  limit; declared lengths are checked before reading.
- The Wikipedia and Crossref adapters parse their expected JSON responses.
  SearXNG factual retrieval also accepts bounded `text/html` and
  `application/xhtml+xml` result pages, parses them as text, and does not execute
  scripts, render pages, or load embedded resources.
- Provider errors are sanitized. Request URLs, response bodies and upstream
  exception text are not written to application logs.

## Compose network isolation

The repository Compose environment now attaches the research worker only to
an internal network shared with PostgreSQL and Redis. The media worker is
attached only to a separate internal network shared with PostgreSQL. These
Docker networks are declared `internal: true`; neither worker receives the
ordinary Compose default network or a default external route. PostgreSQL and
Redis remain on the default application network for the API and other
services, as well as on the narrowly scoped data network required by each
worker.

This is a fail-closed local/development baseline, not a substitute for
production network policy. Do not attach either worker to an ordinary
internet-connected network to make a live provider work. Hosted deployments
must enforce the worker-specific DNS and destination policy at the host,
container platform, firewall, or a reviewed transparent egress gateway.
Verify the actual deployed network path, including IPv4 and IPv6, before
enabling live retrieval. The research HTTP client intentionally does not
trust proxy environment variables; setting `HTTP_PROXY` or `HTTPS_PROXY`
does not establish an approved egress path.

## Residual risks and deployment requirements

Application-level destination validation is not a substitute for network
segmentation. Before enabling any live research provider in a hosted environment:

1. Run the research worker in a dedicated network/security group with outbound
   egress denied by default.
2. Permit DNS only to the deployment's trusted resolver and TCP 443 only to
   approved endpoints through an explicitly reviewed egress policy. SearXNG
   result fetching reaches public publisher hosts, so the approved proxy must
   enforce the reviewed destination policy; do not treat application URL
   validation as permission for unrestricted outbound access.
3. Keep the API and other workers without general-purpose research egress.
4. Re-run the SSRF test suite against the exact aiohttp version in the built
   image and inspect the connector/resolver behavior on upgrades.
5. Review provider terms, source licensing, attribution and factuality before
   using retrieved material in generated videos.

The current provider is opt-in only; the default mock provider remains
unchanged. This implementation does not authorize YouTube uploads or change
publishing permissions. A passing test suite does not by itself approve
production enablement.
