# ADR-0005: Knowledge and media library

- **Status:** Accepted for incremental implementation; provider selections remain provisional.
- **Date:** 2026-11-12
- **Decision owners:** Aurora Studio maintainers

## Context

Aurora Studio needs a reusable knowledge base for cinema, novels, films, series, anime, animation, storytelling, scripts, production techniques and technical resources. It also needs to manage source media and generated images, video, audio, subtitles, proxies and renders.

These concerns must support multiple tenants, source provenance, rights restrictions, search, media processing and provider portability. A publicly accessible file is not necessarily licensed for reuse. Film metadata and analysis must not be confused with permission to copy or publish the underlying film, soundtrack, screenplay or frames.

The repository already has PostgreSQL, tenant-scoped media artifact records, a MediaStorage protocol, local filesystem storage, an S3-compatible SigV4 adapter and an opt-in FFmpeg media worker. The architecture should extend those foundations rather than introduce a parallel media subsystem.

## Decision

Build the Knowledge and Media Library as a domain subsystem inside the existing modular monolith. Keep knowledge metadata, binary object storage, ingestion and retrieval as separate logical responsibilities with typed interfaces.

### 1. Knowledge and media are distinct domains

Knowledge records represent works, documents, concepts, techniques, story structures, scripts, transcripts, scene analysis and relationships. Media assets represent binary files and derived versions. Knowledge records may refer to media assets, but do not implicitly own or grant rights to them.

Collections are configurable and support cinema/film language, books/novels, movies/series, anime/animation, storytelling/screenwriting, production resources and media assets. Avoid hardcoding this taxonomy as an enum.

### 2. PostgreSQL remains the metadata system of record

Reuse existing organization, media artifact, research source and evidence patterns where appropriate. Add new tables only after auditing current schemas and identifying missing semantics. Store metadata, object keys, rights evidence references, processing state and relationships in PostgreSQL. Store large binary objects in object storage, never as database blobs.

Start with PostgreSQL relational filters and full-text search. Evaluate pgvector as the first semantic retrieval option because it reuses the existing database. Add Qdrant or OpenSearch only when measured workload or capability requirements justify the operational overhead.

### 3. Object storage is provider-neutral

Extend the existing MediaStorage port and S3CompatibleMediaStorage adapter. Local storage remains suitable for development and single-node use. Evaluate SeaweedFS, RustFS and Garage as self-hosted S3-compatible candidates; evaluate Cloudflare R2, AWS S3, Backblaze B2, Wasabi and other managed providers through the same adapter where protocol compatibility is proven.

No provider is selected as a universal production default by this ADR. Selection requires compatibility tests, restore/recovery tests, security review and workload-based cost analysis. Credentials are secret references, not tenant database values. Buckets remain private; authorization is enforced by the application.

### 4. Rights and provenance are first-class gates

Every ingested asset must retain source identity, creator/uploader when available, licence or authorization evidence, attribution, permitted purposes, restrictions, verification timestamp and revocation/expiry state. Unknown, restricted, expired and revoked assets are blocked from production. Research-only materials cannot flow into rendering or publishing. Rights are checked again at asset selection, rendering and publishing.

Candidate connectors include Wikimedia Commons, Openverse, Internet Archive, Wikidata, TMDB, IMDb datasets/APIs, AniList and user-authorized uploads. Each connector requires terms, licence, rate-limit, caching and commercial-use review before activation. Do not bypass DRM, scrape subscription services or bulk-ingest copyrighted movies, episodes or screenplay repositories without authorization.

### 5. Ingestion and processing are asynchronous and bounded

Ingestion uses safe fetch/upload, quarantine, signature and size checks, malware/resource limits, FFprobe inspection, FFmpeg derivatives and optional OCR, transcription, scene segmentation and embeddings. Jobs are idempotent, retryable and observable. Outbound connectors require SSRF defenses and network egress controls. Workers remain disabled by default until explicitly configured.

### 6. Tenant isolation applies to every layer

Tenant-owned knowledge, assets, chunks, embeddings, object keys, caches, search results and derived files are organization-scoped. Curated global reference knowledge is explicitly marked global and read-only unless a tenant imports/copies it. No tenant media is used for model training or cross-tenant retrieval by default. Authorization is enforced for every API, worker, search and object operation.

### 7. Paid services are explicit opt-ins

Open-source/self-hosted options are preferred when operationally and legally appropriate. Paid storage, metadata, OCR, transcription, embedding or vision services require explicit operator/tenant configuration, documented cost budgets and clear data-sharing policy. No silent paid fallback and no billable infrastructure provisioning under this ADR.

## Consequences

### Positive

- A single searchable library can support research, story generation, visual planning, media selection and QA.
- Existing PostgreSQL, media artifact and storage code can be reused.
- Storage providers can be changed without rewriting domain workflows.
- Rights, provenance and tenant isolation are enforced as domain invariants.
- Open-source and paid options can coexist behind explicit configuration.

### Trade-offs and constraints

- Rights verification and provenance increase ingestion complexity and may prevent use of otherwise discoverable media.
- Self-hosted object storage requires backup, monitoring, security updates and operational expertise; free software does not mean zero operating cost.
- Semantic and multimodal search introduce model, index and compute costs and should follow a measured baseline.
- Provider compatibility is not guaranteed by the label S3-compatible; each required operation needs contract tests.
- Metadata API terms may restrict caching, commercial use, redistribution or image access.

## Implementation sequence

1. Audit and harden existing storage and media artifact contracts; document threat model and provider capabilities.
2. Add rights/provenance records and enforce production eligibility.
3. Add tenant-safe knowledge collections, works, documents and asset browsing.
4. Add ingestion connectors one at a time after terms and security review.
5. Add full-text search, then optional embeddings and time-coded media segments.
6. Integrate permissioned retrieval with research, story, director and media-selection workflows.

Detailed design and acceptance criteria are maintained in [Knowledge and Media Library Architecture](../architecture/knowledge-and-media-library.md).
