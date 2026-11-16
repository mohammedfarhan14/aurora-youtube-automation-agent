# Research worker and trend discovery

The research worker defaults to deterministic mock retrieval. Live retrieval and
trend discovery are separate, explicit operator choices; neither is enabled by
default.

## Research retrieval versus topic discovery

- `RESEARCH_PROVIDER` controls factual research retrieval. Supported modes are
  `mock`, `wikipedia_http`, `crossref`, `multi_source` and
  `multi_source_searxng`.
- `multi_source` queries Wikipedia, Crossref and official NASA.gov article
  search. NASA article text is useful as evidence, but its rights remain
  `review_required` because pages can contain third-party material. The adapter
  never infers publication clearance from NASA's general media policy.
- `TREND_DISCOVERY_PROVIDER` controls optional daily topic-discovery signals.
  Supported modes are `none`, `hacker_news`, `curated_rss` and `searxng`.
- Trend observations are candidate-selection signals only. They are not stored
  as research sources or evidence and must never be cited as factual support.
  The selected topic still enters the configured research retrieval pipeline,
  where source documents and evidence are collected separately.

## Optional SearXNG discovery

SearXNG is an open-source metasearch engine that can be operated by the
Aurora Studio deployment owner. It is disabled unless all of the following
are explicitly configured:

```dotenv
TREND_DISCOVERY_PROVIDER=searxng
TREND_DISCOVERY_SEARXNG_BASE_URL=https://search.example.com
TREND_DISCOVERY_SEARXNG_QUERY=science technology research discoveries
```

The endpoint must be an HTTPS DNS hostname in production. The provider requests
only the SearXNG JSON search endpoint and bounds the result count to 30. It
validates the search endpoint with the research SSRF-safe URL policy, resolves
only globally routable addresses, disables environment proxies and automatic
redirects, and rejects non-allowlisted hosts. The worker's deployment egress
policy must still restrict and monitor outbound traffic; application-level
validation is not a substitute for issue #41's hosted DNS, IPv4 and IPv6
controls.

Only result titles, HTTPS source URLs, publisher hostnames, published dates and
within-response result ranks are converted into trend observations. Results
without a trustworthy publication timestamp are skipped rather than presented
as fresh. Duplicate source URLs are removed. SearXNG snippets are deliberately
not copied into evidence records. Search-result rank is normalized only within
the single SearXNG response; raw ranks are not compared with Hacker News votes
or RSS counts.

SearXNG failure is non-fatal to schedule dispatch: the dispatcher logs the
provider error type and falls back to deterministic channel-profile topic
selection. No search provider is silently activated, and SearXNG is not used
for factual claim verification.

## Optional SearXNG factual retrieval

To add general web/news/science pages to factual retrieval, configure a
trusted SearXNG instance separately from trend discovery:

```dotenv
RESEARCH_PROVIDER=multi_source_searxng
RESEARCH_SEARXNG_BASE_URL=https://search.example.com
```

This mode composes Wikipedia, Crossref and SearXNG results. Search titles and
snippets select candidate URLs only; Aurora does not store snippets as
evidence. It fetches up to three relevant HTTPS result pages, excludes active
HTML elements while extracting text, and records page excerpts with the source
URL and retrieval time. Redirects to a different hostname are rejected. Page
content with an unverified license remains `review_required`; no search result
or retrieved page is automatically cleared for reuse in a published video.

The SearXNG URL is a deployment-level setting, not a tenant-provided URL. The
worker uses its configured research egress proxy and exact per-request source
host validation. Configure an operator-owned, rate-limited instance and verify
both it and representative result pages from the target worker before enabling
this mode. Search quality, source authority, freshness, publisher
independence, and contradictory claims still require review and evaluation.

## Operational prerequisites

- Operate a SearXNG instance with JSON format enabled for the search endpoint.
- Expose it through a controlled HTTPS origin reachable from the research
  worker. Do not expose an unauthenticated public search instance without
  appropriate rate limits and abuse controls.
- Allow outbound worker traffic only to the configured SearXNG endpoint and
  separately approved factual-retrieval hosts. Hosted egress enforcement remains
  tracked in issue #41.
- Keep the default `TREND_DISCOVERY_PROVIDER=none` until network controls and
  provider behavior have been verified in the target environment.

## Safety boundary

Search results are untrusted discovery inputs. Their titles are topic leads,
not instructions. Retrieved source pages and excerpts remain untrusted content
and must pass through the existing research serialization, evidence, claim
verification and editorial-review controls. A high search rank does not imply
source credibility, factual accuracy, independence or licensing permission.


## Persisted source provenance and independence

Research persistence now records structural provenance for each fetched source:

- `publisher_domain`: normalized hostname (with a leading `www.` removed).
- `source_class`: conservative descriptive classification based on the declared source type and a small set of recognizable domains.
- `independence_group_key`: deterministic group key scoped to the research run.
- `independence_basis`: whether grouping came from a shared hostname, exact content, near-duplicate content, or a unique source.
- `classification_version`: classifier version for reproducibility.

The classifier does not produce a reliability score and does not assert that a domain is authoritative for a claim. Existing rows are migrated to `unknown` / `unassessed`; historical metadata is not guessed.

Grouping uses same-host matching and cross-host exact/near-duplicate text detection. The near-duplicate check uses five-token shingles and a Jaccard threshold of 0.80. It is a bounded heuristic, not a complete syndication/wire-service ownership graph. It can miss lightly rewritten copies and may group genuinely similar source texts. Keep the raw sources visible to reviewers; do not count an independence group as multiple independent corroborating publishers.

The assessment is bounded to 20 documents, 100,000 characters per document and 1,000,000 characters per set. It uses no external network or LLM calls. Richer publisher identity, ownership, correction/retraction and claim-specific reliability assessment remain follow-up work.
