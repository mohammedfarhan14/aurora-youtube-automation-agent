# Topic Discovery and Research Intelligence

## Status and purpose

This document defines the next intelligence subsystem for Aurora Studio. It extends deterministic channel-topic diversification in PR #104; it does not claim that live trend discovery or multi-source research is already implemented.

The subsystem answers two different questions:

- **Discovery:** What should this channel investigate now?
- **Research:** What can we responsibly substantiate about the selected topic?

A trend signal is a lead, not evidence. Popularity, velocity, or social engagement must never be treated as factual corroboration.

## Product behavior

For each connected channel and its immutable editorial-profile snapshot, the system should:

1. Collect candidate topics from explicitly enabled trend/search adapters.
2. Normalize, cluster, and deduplicate candidates across providers and the channel's prior research/published history.
3. Score momentum, recency, novelty, channel relevance, audience fit, source diversity, saturation, and editorial constraints.
4. Explain why a candidate was ranked and retain originating trend observations.
5. Research selected candidates through independent sources, preserving exact URL, publisher, timestamps, source type, excerpts, content hash, and retrieval method.
6. Assess source quality and independence, identify stale or conflicting material, and build an evidence graph linking each atomic factual claim to supporting and contradicting excerpts.
7. Pass only bounded, provenance-preserving evidence to story generation and claim verification.
8. Preserve a reviewable audit trail and make low confidence, missing corroboration, and failures explicit.

A channel may use a human-selected topic. Discovery is an assistive upstream stage and must not remove editorial control.

## Architecture boundaries

Keep the existing modular monolith and durable PostgreSQL worker. Introduce separate ports:

- Trend discovery provider: returns bounded candidate observations, not factual documents.
- Research search provider: searches for source documents for a topic/query.
- Research document provider: fetches and normalizes an exact source document.
- Source policy: evaluates source eligibility, domain restrictions, source class and reuse constraints.
- Topic ranker: deterministic, explainable scoring over normalized observations and channel context.
- Evidence reconciler: clusters duplicate/syndicated sources, records conflicts and maps excerpts to atomic claims.

Keep domain contracts independent of vendor SDKs and HTTP details. Provider-specific implementations remain replaceable adapters. Baseline ranking, source-policy decisions, and evidence identity must not require LLM calls.

## Trend signal model

Each observation should retain provider and observation IDs; topic/entity text and normalized cluster key; provider-reported observed/published time and Aurora retrieval time; geography, language and category where available; provider-specific raw metrics with names and units; canonical source/landing URL and publisher; confidence metadata and adapter version.

Trend ranking should use normalized, provider-aware features rather than compare raw view counts across unrelated systems. Suggested feature families:

- Momentum and acceleration within the same provider/metric.
- Recency and observation freshness.
- Channel-niche and audience relevance.
- Novelty against recent research and published topics.
- Cross-provider presence as a discovery signal.
- Saturation and repetition penalties.
- Channel exclusions, sensitive-topic policy and editorial constraints.

Do not fabricate missing metrics. A provider without a comparable historical baseline must not receive a false velocity score. Persist scoring inputs and version so past rankings are reproducible.

## Multi-source research model

Use a staged search-then-fetch workflow:

1. Search adapters return candidate source references and search metadata.
2. Source policy validates provider, scheme, hostname, content type, size, redirects, terms requirements and configured egress rules.
3. Fetch adapters retrieve bounded content and normalize it into the existing ResearchDocument contract (or a backward-compatible extension).
4. Canonical URL and content hashes detect duplicate documents and changed versions.
5. Publisher, author, source class, publication time, retrieval time and content language are retained.
6. Evidence excerpts include stable locators such as section, paragraph, page, timestamp or API record ID where available.

Source classes distinguish primary/official records, peer-reviewed research, reputable journalism, specialist reporting, reference material, user-provided material and social/community posts. Classification is contextual: a government agency is authoritative for its own statistics, not necessarily every interpretation.

## Reliability, independence and conflict handling

Do not collapse source reliability into a single opaque global score. Store explainable dimensions: publisher identity and accountability practices; primary vs secondary reporting; expertise and relevance to the specific claim; publication date and whether the claim may have changed; directness of evidence and stable locator availability; corrections/retractions/limitations; and independence from other cited sources.

Two URLs are not two independent sources if they reproduce the same press release, wire story or syndicated article. Cluster by canonical URL, content hash, publisher lineage and near-duplicate content. Require appropriate independent corroboration for consequential claims. An authoritative primary source may stand alone when it is the original record, but expose that evidence basis.

Contradictions must be represented, not averaged away. Preserve competing excerpts, dates, source classes and claim status. If evidence is insufficient or materially conflicting, the claim remains unsupported/conflicted and is excluded from confident narration unless the script explicitly explains uncertainty.

## Candidate provider strategy

Use adapters selected by explicit configuration and documented terms/limits. Initial candidates for evaluation:

- Google Trends data/RSS or an officially supported interface where available and permitted; do not depend on undocumented endpoints as a production contract.
- YouTube Data API search metadata for channel/topic discovery only, with quota accounting; it is not a general replacement for a trending feed.
- Hacker News official API for technology/startup signal.
- Reddit API only after reviewing current API terms, access approval, rate limits and permitted commercial use; no scraping or unauthenticated workarounds.
- Curated publisher RSS/Atom feeds and official newsroom/agency feeds.
- Wikimedia/Wikipedia and Wikidata for reference discovery, not as sole corroboration for fast-moving news.
- Operator-hosted SearXNG or another self-hosted metasearch adapter as an optional broad search port, subject to outbound network and upstream policy controls.

No provider is mandatory. A source must pass current terms, licensing, reliability, privacy, rate-limit and operational review before enablement. Paid services must never be silently selected as fallback.

## Security and privacy

- Treat retrieved text, feed metadata, HTML, PDFs, captions and model output as untrusted data and potential prompt injection.
- Retrieved instructions never gain tool permissions or override system policy.
- Enforce URL validation, DNS/IP pinning, redirect revalidation, strict content-type and response-size limits, timeouts, concurrency caps and deployment-level egress controls.
- Do not allow arbitrary tenant-supplied URLs to become an SSRF primitive. Use approved source registries or separately reviewed domain policies.
- Avoid storing unnecessary personal data from social/community sources. Honor deletion and retention requirements.
- Enforce organization and channel scope on every query, observation, source, evidence item, cache entry and worker operation. No cross-tenant retrieval or training by default.
- Store provider credentials encrypted; never log tokens, raw authorization headers or sensitive query parameters.
- Keep live network retrieval disabled by default in CI and local test runs; tests use deterministic fixtures and mock transports.

## Evaluation and acceptance

Create a deterministic, versioned evaluation corpus containing trend spikes, decaying trends, evergreen topics, duplicate spellings and cross-provider aliases; misleading engagement spikes; stale/updated articles, corrections and retractions; syndicated/near-duplicate reports; primary-source vs commentary conflicts; prompt-injection payloads; SSRF/redirect/DNS-rebinding/oversized-response/malformed-feed/timeout fixtures; and tenant-isolation/channel-history boundaries.

Track discovery relevance, novelty, freshness, source diversity, claim support rate, conflict detection, citation completeness, retrieval latency, provider failures and cost/quota consumption. Do not optimize engagement at the expense of factuality or editorial suitability.

Acceptance requires reproducible ranking explanations, multiple genuinely independent sources where available, atomic claim-to-excerpt traceability, safe failure on unsupported/conflicting claims, deterministic CI, and an explicitly configured local/self-hosted end-to-end path. Hosted live retrieval remains blocked until issue #41 network egress controls are implemented and verified.


## Persisted source provenance

Each retrieved source now retains a conservative publisher hostname, a descriptive
source class, a deterministic independence-group key, the basis for that grouping,
and a classifier version. The classes describe source format/domain signals
(`scholarly`, `journalism`, `official_domain`, `reference`, `community`,
`video`, `unknown`); they are not reliability ratings or endorsements.

Independence grouping is run-scoped and deterministic. Sources from the same
normalized hostname are grouped together. Cross-host sources are grouped when
their normalized content is identical or their five-token-shingle Jaccard
similarity is at least 0.80. This detects some syndicated/near-duplicate material,
but it is not a complete publisher ownership or wire-service lineage graph.
Hostname matching deliberately avoids guessing registrable domains without a
Public Suffix List, so separate subdomains may remain separate publishers.

The classifier does not assign an opaque truth score. Primary-source status,
claim-specific expertise, correction history, source accountability and
independent corroboration still require richer source-policy/evidence assessment.
Existing rows are marked `unknown` / `unassessed` by the migration; no historical
publisher or reliability claims are fabricated.


## Claim verification uses source independence

The story-research context carries the persisted publisher and independence metadata into the claim-verification stage. The verifier receives the independence-group key as untrusted metadata and is explicitly instructed not to treat repeated or syndicated copies within one group as independent corroboration. The application, not the model, computes the number of distinct supporting and contradicting groups for each claim and stores those counts on the factual-claim contract.

Legacy sources without an assessed group key are treated as separate, unassessed source groups rather than being silently merged or assigned fabricated lineage. These counts describe provenance diversity only; they do not establish source quality, truth, or sufficient corroboration. Claim status and human editorial review remain separate decisions.


## Claim outcomes distinguish contradiction from conflict

A verifier assessment containing only contradicting excerpts produces the `contradicted` status: the supplied evidence challenges the claim, but the pipeline has not found competing supporting evidence. The `conflicting` status is reserved for cases where at least one excerpt supports the claim and at least one excerpt contradicts it. Both outcomes retain their complete excerpt/source references and independent-group counts. Neither status is resolved by majority vote or by counting syndicated copies.

The new `contradicted` value is additive. Historical artifacts labelled `conflicting` remain readable under the legacy validation rule; newly generated artifacts use the more precise distinction. This remains an automated evidence assessment, not a definitive truth determination.


## Deterministic claim-verification evaluation fixtures

The backend test suite includes a versioned JSON fixture corpus for the application-level claim-verification invariants: single-source support, independent corroboration, syndicated copies counted once, unopposed contradiction, independent support/contradiction conflicts, syndicated support alongside an independent contradiction, missing excerpts, no available sources, and retrieved prompt-injection text remaining inside the untrusted reference-data boundary.

These fixtures exercise deterministic contract mapping and provenance aggregation with mocked model responses. They do not measure model factual accuracy, retrieval recall, or real-world source reliability. Those require a separately curated and human-reviewed benchmark with adjudicated labels and provider/model-specific evaluation.
