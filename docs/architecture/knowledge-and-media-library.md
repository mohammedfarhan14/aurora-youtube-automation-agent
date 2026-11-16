# Knowledge and Media Library Architecture

- **Status:** Proposed; implementation proceeds through reviewed increments.
- **Related decision:** [ADR-0005 — Knowledge and media library](../decisions/ADR-0005-knowledge-and-media-library.md)

## 1. Product intent

The Knowledge and Media Library is a reusable creative-intelligence subsystem for Aurora Studio. It is not a public dump of films or a bucket browser. It provides governed, searchable knowledge and reusable assets to research, story, storyboard, media-selection, rendering and quality workflows.

It has two connected but separately governed domains:

1. **Knowledge:** works and entities, research notes, articles, books, scripts, story structures, cinematography and animation techniques, production guides, transcripts, scene descriptions and evaluations.
2. **Media:** source files and derived objects such as images, video, audio, subtitles, thumbnails, proxies, generated assets and final renders.

A work's descriptive metadata or analysis does not grant rights to reuse its underlying screenplay, frames, soundtrack or video. Rights and permitted purposes are checked on every production-use path.

## 2. Domain boundaries

Keep this within the existing modular monolith initially. Do not create microservices solely to mirror the diagram.

| Domain | Responsibility | Existing foundation |
|---|---|---|
| Knowledge catalogue | Collections, documents, works, concepts and relationships | PostgreSQL |
| Source/provenance | Origin, retrieval time, source identifiers, checksums and evidence | Existing research source/evidence patterns |
| Rights registry | Licence evidence, attribution, restrictions, expiry, authorization and revocation | Existing Wikimedia provenance patterns |
| Media registry | Asset metadata, tenant ownership, versions, processing state and object references | Existing media artifact registry |
| Object storage | Private binary objects behind a provider-neutral port | Existing MediaStorage, LocalMediaStorage and S3CompatibleMediaStorage |
| Ingestion | Safe upload/import, inspection, extraction and derivative generation | Existing media worker and FFmpeg |
| Retrieval | Keyword, relational and semantic search with authorization filters | PostgreSQL first; optional pgvector/Qdrant later |

The current S3 adapter already targets AWS S3, Cloudflare R2 and MinIO-compatible endpoints. Extend and test this abstraction rather than introducing a competing storage interface.

## 3. Knowledge collections

The initial collection taxonomy should be configurable, not a rigid enum:

- **Cinema and film language:** cinematography, lenses, shot sizes, composition, blocking, camera movement, lighting, color, editing, sound design and visual storytelling.
- **Books and novels:** bibliographic metadata, themes, plot summaries, narrative structures and licensed/public-domain text where permitted.
- **Movies and series:** title metadata, genres, creators, release data, high-level analysis, user notes and authorized reference materials.
- **Anime and animation:** animation principles, storyboarding, movement, character-design concepts, backgrounds, effects and genre conventions.
- **Story and screenwriting:** hooks, pacing, conflict, arcs, scene construction, screenplay formats, original examples and authorized scripts.
- **Production resources:** technical documentation, tutorials, datasets, workflow guides, prompts, rubrics and evaluation examples.
- **Media assets:** images, video clips, audio, subtitles, thumbnails, proxies and generated outputs.

Collection membership is many-to-many. A work can belong to several genres, styles, periods, franchises or learning topics.

## 4. Source connectors and ingestion policy

Candidate connectors, each requiring a dedicated adapter and policy review:

| Source | Potential use | Required safeguards |
|---|---|---|
| Wikimedia Commons | Images, audio and video | Validate each file's licence, attribution and restrictions; preserve source page and revision metadata |
| Openverse | Discovery of openly licensed media | Treat results as discovery, then verify the original source and licence |
| Internet Archive | Historical media, texts and recordings | Verify item-level rights; archive availability is not a reuse licence |
| Wikidata | Structured works, people, genres and relationships | Preserve entity IDs and query provenance; observe service limits |
| TMDB / IMDb datasets or APIs | Film and television metadata | Review API terms, attribution, caching, commercial use and image restrictions before enabling |
| AniList / other anime catalogues | Anime metadata | Review API terms, rate limits and permitted caching/use |
| User uploads and connected sources | Tenant-provided material | Require uploader authority/authorization declaration and tenant-scoped access |

Do not scrape subscription services, bypass DRM, download copyrighted movies/series, or ingest entire screenplay repositories without documented authorization. A metadata record or source link may be retained when permitted even when the underlying media cannot be copied.

### Ingestion stages

1. Resolve connector policy and request limits.
2. Fetch only permitted metadata or media; enforce outbound network controls, SSRF defenses, redirect restrictions, byte limits and content-type validation.
3. Persist source URL/identifier, retrieval timestamp, source revision when available, uploader/creator, licence URL/text, attribution and original checksum.
4. Quarantine new files until file signature, size, malware and media parsing checks pass.
5. Inspect with FFprobe; generate thumbnails, safe proxies and technical metadata with bounded FFmpeg jobs.
6. Extract captions/subtitles where present; optional OCR, speech recognition and scene segmentation are separate opt-in processing capabilities.
7. Generate text/visual embeddings only when source and processing rights permit it.
8. Mark the asset searchable only after ingestion and rights policy checks complete.

Ingestion must be idempotent by connector/source identity and content checksum. Failed or partial jobs must be retryable without duplicate assets.

## 5. Rights and provenance model

Rights are not inferred from a URL, file extension, search result label or the fact that a file is publicly viewable.

Every asset should carry a rights decision with evidence: source and source page; creator/uploader and attribution text; licence identifier/version and canonical URL, or authorization document reference; permitted purposes; restrictions; verification method, verifier, timestamp and policy version; status, revocation/expiry and audit history.

Suggested states: verified_public_domain, verified_cc0, verified_cc_by, verified_commercial_license, tenant_owned, user_authorized, research_only, restricted, unknown, expired and revoked.

These are workflow states, not legal conclusions. CC BY requires attribution and licence-version compliance. Non-commercial and share-alike materials must not be treated as generally available for monetized production.

Default policy:
- unknown, restricted, expired and revoked assets are blocked from production.
- research_only assets can be retrieved only by research/analysis flows permitted by source terms; they cannot be sent to a renderer or publishing pipeline.
- Production use requires a positive rights decision for the specific purpose and tenant.
- Rights checks are repeated at selection/render/publish boundaries; a previously approved asset can later be revoked.
- Preserve attribution requirements into final video descriptions/credits where applicable.

## 6. Media storage architecture

The database stores metadata and opaque object keys; object storage holds bytes. Never store large MP4s or binary media in PostgreSQL.

### Existing storage ports

Use and strengthen the existing MediaStorage contract and adapters:
- LocalMediaStorage: development and single-node use.
- S3CompatibleMediaStorage: private path-style S3 requests with SigV4; currently intended for AWS S3, Cloudflare R2 and MinIO-compatible endpoints.

Do not expose public buckets or change ACLs. Serve assets through authorization-checked API routes or short-lived signed URLs once a safe signed-URL capability is implemented. Credentials must come from environment/secret references, never database rows or client payloads.

### Provider evaluation matrix

| Option | Model | Fit and trade-offs |
|---|---|---|
| Local filesystem | Open/local | Lowest-friction development; not durable or horizontally shared without external backup and shared-volume design |
| SeaweedFS | Open-source, self-hosted object/file storage | Candidate for large media collections and distributed deployments; validate S3 API coverage, operational maturity and backup/restore |
| RustFS | Open-source, self-hosted S3-compatible storage | Candidate S3-first deployment; validate maturity, multipart behavior, lifecycle/versioning, security and recovery before production |
| Garage | Open-source distributed S3-compatible storage | Candidate for lightweight distributed setups; validate required S3 feature coverage and operational fit |
| Ceph RGW | Open-source distributed object storage | Broad capabilities and scale, but substantial operational complexity; defer until scale justifies it |
| Cloudflare R2 | Managed S3-compatible object storage | Attractive egress economics; model storage, operations, retrieval and account limits |
| AWS S3 | Managed object storage | Mature lifecycle, replication, IAM and ecosystem; model request, retrieval and transfer costs |
| Backblaze B2 | Managed object storage | Cost-conscious option; model transaction, retrieval and egress terms |
| Wasabi | Managed object storage | Evaluate minimum-duration, capacity and usage terms against workload |
| Google Cloud Storage / Azure Blob | Managed object storage | Enterprise integrations and storage classes; provider-specific adapter or S3 gateway decision required |

Do not select a provider based only on advertised per-GB price. Benchmark representative short renders and source files, multipart uploads, concurrent reads, thumbnail/proxy generation, download/egress, request counts, lifecycle transitions, recovery time and total monthly cost.

Do not make MinIO Community Edition the default new dependency without a fresh maintenance and licensing review. Do not assume a provider's free tier is a permanent production plan.

### Configuration model

Operator-level configuration should support backend type and endpoint, region, bucket/container, path-style/virtual-host behavior; credentials by secret reference with rotation and least-privilege permissions; TLS; maximum object size; multipart thresholds; bounded timeouts; private access; encryption; lifecycle and retention policies by asset class; organization quotas and budgets; health checks, metrics, backup and restore verification.

Keep provider credentials out of tenant-facing settings unless a separately designed BYOK feature is implemented with encryption, validation, audit and explicit tenant consent.

### Object keys and resilience

Use opaque UUID-based identifiers, not user-supplied filenames or personal information. Suggested layout:

    organizations/{organization_uuid}/assets/{asset_uuid}/original
    organizations/{organization_uuid}/assets/{asset_uuid}/derived/{derivative_uuid}

Store filename, MIME type, checksum, size, dimensions, duration and storage backend in metadata. Reject path traversal and enforce tenant ownership on every object operation.

Originals, derivatives, temporary uploads, renders and logs have distinct retention classes. Deletion must use a durable deletion/outbox workflow and reconciliation job; database deletion must not silently orphan bytes. Replication is not a backup. Test restore and object/database consistency. Support tenant export and deletion. Do not enable lifecycle deletion until retention/legal-hold rules and restore behavior are tested.

## 7. Metadata model

Proposed logical entities; reuse existing media_artifacts, research_sources, evidence_items and organization ownership where suitable.

| Entity | Key responsibilities |
|---|---|
| knowledge_collections | Tenant or curated-global collection, title, description, visibility and taxonomy |
| knowledge_documents | Text/document metadata, language, content hash, extraction state and source reference |
| creative_works | Book, film, series, episode, anime, short, game or other work metadata |
| knowledge_relationships | Typed relations between works, people, concepts, techniques, documents and assets |
| media_assets | Organization/owner, type, MIME, checksum, byte size, storage key, source, status and rights reference |
| media_asset_versions | Original, proxy, thumbnail, extracted audio, subtitle and render lineage |
| media_sources | Connector/source identity, URL, revision, fetched time and provenance |
| media_rights_records | Licence/authorization evidence, allowed purpose, restrictions, expiry and revocation |
| media_analysis | Technical metadata, transcript, OCR, scene descriptions and model/version provenance |
| media_segments | Time ranges, keyframes, dialogue, shot/scene descriptions and derived-object references |
| knowledge_chunks | Chunk text, offsets, document version and retrieval metadata |
| knowledge_embeddings | Embedding model/version, vector and source chunk/asset reference |

All tenant-owned rows must have organization scope and enforce it in repository queries and authorization checks. Curated global knowledge must be explicitly marked global and read-only to tenants unless copied/imported. Never make tenant content global by default.

## 8. Search and retrieval

Implement in layers:
1. PostgreSQL relational filters and full-text search for initial collections and metadata.
2. Optional pgvector for text embeddings in the existing PostgreSQL deployment.
3. Qdrant only when independent vector scaling, hybrid retrieval or multimodal workloads justify a separate service.
4. OpenSearch only when advanced text indexing/analytics justify its operational cost.

Search must apply tenant/visibility/rights filters before returning candidates. Semantic similarity never overrides access control or rights. Search results include provenance and rights status, not just a similarity score.

For video, index time-coded segments and representative frames rather than embedding entire MP4 binaries. Preserve links from each segment back to the original asset and its rights record.

## 9. Processing and AI capabilities

Prefer open-source, replaceable workers where quality and deployment constraints permit:
- FFmpeg/FFprobe for media inspection, proxies, thumbnails and transformations.
- Whisper/faster-whisper for speech transcription, subject to model and source-use policy.
- PaddleOCR or Tesseract for OCR.
- Apache Tika for document text extraction.
- Open embedding and vision models behind explicit provider adapters.

Every model/processor record includes name, version, configuration, license, source asset and processing timestamp. Hosted/paid APIs require explicit opt-in and budgets; no silent paid fallback. Workers remain disabled by default in development and CI.

## 10. Security and privacy

- Authorize organization and asset access on every API request, job, signed URL and object operation.
- Never trust tenant IDs, object keys, MIME types, file extensions or source URLs supplied by clients.
- Enforce upload byte limits, streaming limits, file signature inspection, malware scanning and decompression/resource limits.
- Apply SSRF-resistant outbound fetch rules, DNS/IP validation, redirect limits and egress controls to all connectors.
- Quarantine untrusted uploads; do not parse or execute embedded scripts/macros.
- Treat documents, subtitles, transcripts, metadata and video frames as untrusted content. Defend against prompt injection by separating retrieved data from instructions and using bounded, typed context.
- Do not use tenant media for model training or cross-tenant retrieval by default.
- Encrypt secrets and sensitive metadata; log access and rights changes without logging credentials or raw private content.
- Support deletion/revocation propagation to derived assets, indexes, caches and provider copies where controllable.

## 11. Phased implementation and acceptance

### Increment A — Contracts and design
Document provider capabilities, asset lifecycle, rights states and tenant ownership. Audit existing media artifact and storage models before migrations. Add architecture decision and threat-model review. No new infrastructure or external provider credentials.

### Increment B — Storage hardening
Add contract tests for local and S3-compatible adapters. Test tenant key isolation, malformed keys, size limits, checksum integrity, concurrent operations, timeouts and error handling. Add multipart/resumable upload only with bounded part sizes and abort cleanup. Test against at least one local S3-compatible implementation and one managed provider when credentials are explicitly available. Add backup/restore and orphan reconciliation before production.

### Increment C — Rights-aware asset registry
Add structured source and licence records, versioning, revocation and policy checks. Enforce rights checks in media selection and rendering. Add tenant-safe asset browsing and metadata APIs.

### Increment D — Knowledge ingestion and search
Add document/metadata connectors one at a time with explicit terms review. Add safe extraction, chunking, keyword search and optional embeddings. Add time-coded video segments and multimodal search only after rights and processing controls are proven.

### Increment E — Agent integration
Expose typed, permissioned retrieval tools to research, script, director and media-selection workflows. Require evidence/asset provenance in agent outputs. Preserve human review and current publishing-consent gates.

### Acceptance gates
- No cross-tenant read/write/search/object access in adversarial tests.
- No unknown/restricted/revoked asset reaches production.
- Every result carries source and rights provenance.
- Provider-independent tests pass without paid credentials.
- No worker or cloud resource is enabled implicitly.
- Object/database restore and deletion reconciliation are demonstrated.
- Cost/retention limits are enforced and observable.
