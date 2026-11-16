# Engineering Roadmap and Delivery Principles

## Purpose

This document separates product engineering readiness from the later decision to operate a hosted production service. It is the source of truth for phase status, acceptance criteria, quality gates and deployment constraints.

## Product acceptance target

For every connected YouTube channel, Aurora Studio should autonomously produce one approximately 30-second short-form video per day: topic discovery and research, evidence-backed story/script, claim verification, narration, visuals, rendering, quality/compliance checks and YouTube publication. Scheduling must be durable, per-channel and timezone-aware, with pause controls, idempotency, retries, reconciliation and audit history.

The channel owner must explicitly grant channel-bound publishing consent and enable automatic publishing. Human approval remains the safe default until that setting is deliberately enabled.

## Current status — 13 November 2026

**Current execution phase: Phase 3 — Production Engine (in progress).** The repository does not use numbered sprints. Progress is tracked through phases, issues and pull requests.

Latest verification on 13 November 2026: `develop` passed 815 backend tests, Ruff and container/Compose validation after fixing the FFmpeg crop filter chain. Frontend CI passed 16 tests and a production build on the latest frontend change. This verifies CI behavior, not live external provider credentials, a configured hosted deployment, or a real YouTube upload.

PR #96 remains the end-to-end media baseline: it advances a PostgreSQL-backed production run through every durable media stage, renders a real approximately 30-second 1080 × 1920 MP4 using FFmpeg, verifies it with ffprobe, persists video/caption artifacts and reaches `awaiting_review` without automatic approval. Its synthetic tone intentionally has unknown rights and confirms that publishing remains blocked. The rights-cleared path is still an open acceptance criterion.

PR #128 merged into `develop` as `d677801740a4362dd3086e69dbf1cbdad8342dd2`; it verifies rendered attribution credits against the source-asset manifest so required credits cannot silently disappear between asset selection and the final render. PR #130 merged as `6af40864b01350c135f881b9109baa68bce0f45c`; it removes slow remote pnpm/pip cache restore/save operations on persistent self-hosted runners, restores frontend-only path filters while keeping backend CI universal, and moves FFmpeg installation ahead of application-source copies to preserve BuildKit cache. PR #132 merged as `ac9af1b01d0982ee4dc797c55950c25dcc9afb1e`; it normalizes streaming-WAV size sentinels from OpenAI-compatible TTS servers and documents Kokoro-82M/Kokoro-FastAPI as an open-source evaluation candidate. PR #133 merged as `a852b72b43ec9edd224e8c2665663bacb4a935e4`; it adds structured source-level rights metadata, captures Wikipedia revision/license and Crossref author provenance, and preserves rights snapshots through bounded story context, story artifacts and claim-verification integrity checks. The final media/render/publish rights gate remains outstanding. PR #134 merged as `be5cc95713b329af92019c3e3e0c7dda5a8e8c79`; it versions source identity snapshots in StoryArtifact 1.3.0, carries rights into direction/render manifests, rebuilds attribution from the verified story, and blocks the final quality gate for unknown, unresolved or incompatible source rights. PR #135 merged as `8c63e8a818c203722c67ee2dcb7b585cf973dae2`; it adds tenant-scoped reusable source-rights profiles, immutable review audit records, owner/admin review APIs, tenant-member history APIs, evidence/revision validation and rights inheritance across future research runs. Its CI passed 696 backend tests, Ruff, dependency audits, migration application and Docker Compose/container validation. PR #136 merged as `873bcdcbcaa2e0a415b84a70b78bf9fe84e42fa0`; it adds the source-rights review workspace with rights metadata, exact revision and license references, owner/admin decision form, supporting evidence and immutable audit history. Frontend CI passed 16 tests across 2 files and the TypeScript/Vite production build; backend CI passed 696 tests and Docker Compose/container validation. PR #137 merged as `19c76334ef285fc8500f4733feffb24d07f83ade`; it adds channel-owner automatic-publishing consent distinct from OAuth upload scope, immutable consent history and revocation on connection/scope changes, plus a tenant-scoped, idempotent publishing-intent API that rechecks explicit approval, upload scope, final quality gate, rights and attribution before persisting a queued job. CI passed 701 backend tests, Ruff, dependency audits, Alembic migration application and Docker Compose/container validation. The response explicitly reports that automatic execution is not yet available. PR #141 merged as `2c5baff7526adc287ea837a827e466f7ff7424aa`; it adds the Google YouTube Data API resumable-upload adapter, tenant/job-bound encrypted upload sessions, chunk/offset recovery, leased worker retries and processing reconciliation. Videos are staged privately and approved public/unlisted visibility is applied only after successful processing. A dedicated Squid proxy allowlists the Google OAuth/YouTube API hosts, while the worker has no direct public egress and remains disabled by default. CI passed 716 backend tests, Ruff, dependency audits, Alembic migration application and Docker Compose runtime egress validation.

The optimized CI was validated on both self-hosted runners (`aurora-ci-wsl` and `aurora-ci-wsl-2`). Frontend dependency installation fell from a 9m12s remote-cache restore timeout to 7.2 seconds on a cold local store and 111–130 ms on a warm store; 16 frontend tests and the production build passed. Removing the remote pip cache eliminated an approximately 30-second post-job cache upload while retaining hash-locked installs; backend CI passed 671 tests on PR #130, 672 tests on PR #132, 684 tests on PR #133, 690 tests on PR #134 and 696 tests on PR #135. The first FFmpeg image build was a cold install; the next source-change build hit the cached FFmpeg layer and completed the media image build without reinstalling FFmpeg. Compose worker-isolation/egress smoke tests passed. These results validate CI mechanics, not staging acceptance or production readiness.

PR #101 added EBU-style loudness normalization, true-peak measurement and black-frame detection; PR #102 added a dedicated FFmpeg-enabled media image and opt-in Compose worker profile. PR #100 added opt-in rights-filtered Wikimedia Commons still-image retrieval, restricted to CC0 1.0 and CC BY 4.0 with source/license/attribution provenance. PR #100 merged into `develop` as commit `28dba63a3176b62a3d98c7936490860ad28d8666`; its pull-request CI and post-merge Backend CI run #558 both passed. A green CI run certifies only the checks configured in that workflow; it is not a staging acceptance or production-readiness certification.

### Integrated capabilities

- **Platform foundation:** multi-tenant identity and organization roles, server-managed browser sessions, CSRF, Redis-backed rate limiting, encrypted email outbox, Resend adapter, worker leases and operational health metrics.
- **YouTube connection and publishing:** Google OAuth, read-only defaults, explicit upload-scope consent, exact-channel selection and server-derived publishing capability; tenant-scoped approval-gated publishing intents; encrypted resumable upload sessions; chunk/offset recovery; leased worker retries; YouTube processing reconciliation; private-first upload with approved visibility applied only after processing. PR #142 hardens token/authorization handling and PR #144 adds the consent-gated production-to-publishing-job handoff. The worker and its `youtube-publish` Compose profile remain disabled by default; production deployment and real-channel acceptance are outstanding.
- **Research and discovery:** tenant-scoped research runs, source/evidence persistence, PostgreSQL worker leases/retries/recovery, deterministic mock provider, opt-in SSRF-resistant Wikipedia and Crossref scholarly-abstract adapters, and bounded multi-source composition with duplicate/conflict handling. PRs #108–#110 add provider-neutral trend ranking, opt-in Hacker News discovery and operator-curated RSS/Atom feeds; PRs #113–#114 integrate channel-aware selection and curated-feed discovery into daily dispatch. Trend observations are discovery leads, never factual evidence. PR #104 adds profile-aware candidate diversification/novelty ranking. PR #106 hardens S3-compatible download integrity.
- **Intelligence:** provider-neutral LLM contracts, deterministic mock inference, OpenAI-compatible inference adapter, evidence-linked story contracts, bounded tenant context, schema-validated story generation, atomic claim extraction/evidence assessment and immutable story artifacts.
- **Skills/tools:** exact-version registry, strict schemas, explicit tool allowlists, server-side authorization, bounded execution and immutable sanitized audit records. Research and story-generation/claim-verification stages use the permissioned runtime.
- **Editorial workflow:** versioned channel profiles, immutable profile snapshots, production runs/stages, idempotency, leases, cancellation, bounded retry backoff, approval identity/timestamp and a story/evidence review workspace.
- **Daily scheduling:** timezone-aware per-channel schedules, pause/catch-up behavior and transactional dispatch from the research worker. A due schedule creates a channel/profile-bound research run and hands completed research into production orchestration; after final QA, explicit channel-owner automatic-publishing consent can create a durable publishing job (PR #144).
- **Media:** tenant-scoped media artifact registry, secure local storage, private S3-compatible storage, configurable OpenAI-compatible TTS (including streaming-WAV compatibility), local abstract visuals by default plus opt-in Wikimedia Commons CC0/CC BY 4.0 still-image retrieval, deterministic scene planning, FFmpeg/ffprobe rendering, readable WebVTT cues, EBU-style loudness/true-peak QA, black-frame detection and rendered attribution-manifest verification. Kokoro-82M/Kokoro-FastAPI is documented as an open-source evaluation candidate; no TTS runtime/voice is selected or enabled by default. Source-level rights metadata is persisted and snapshotted into StoryArtifact 1.3.0, propagated into rendered-video attribution metadata, and enforced by the final media QA gate. PR #135 adds the audited backend rights-review workflow and reusable tenant rights profiles; PR #136 adds the frontend review workspace. The publishing-intent and consent foundation is merged in PR #137; the resumable upload adapter/worker, encrypted session recovery, Google processing-state reconciliation and isolated Google-only egress proxy are merged in PR #141. The worker remains disabled by default and has not been deployed against a real channel. Daily automatic dispatch, production worker health/observability, operator reconciliation UI and a separately approved real test-channel acceptance run remain outstanding. The dedicated media-worker Compose profile packages FFmpeg separately from the API image.
- **Engineering gates:** backend/frontend CI, hash-locked dependencies, runtime/development dependency audits, migrations, Docker image/Compose validation and runtime smoke tests. Persistent self-hosted runners use local pnpm/pip caches; frontend checks are path-filtered, backend checks remain universal, and the FFmpeg image layer is cache-stable across application source changes.

### Current operational defaults and boundaries

- `RESEARCH_PROVIDER=mock` remains the default. `wikipedia_http`, `crossref`, `multi_source` and the opt-in `multi_source_searxng` factual adapter are available; the new SearXNG page-fetch path has not passed live instance acceptance. `TREND_DISCOVERY_PROVIDER=none` remains the default; `hacker_news`, operator-configured `curated_rss` and SearXNG discovery are separate opt-in signals, never factual evidence. Source authority/freshness/independence evaluation, broader primary-source coverage, evidence conflict reconciliation and tenant BYOK/model budgets remain incomplete. Hosted live retrieval remains gated on issue #41's platform-level egress controls.
- Hosted live research remains blocked until issue #41's network-level egress controls are implemented and verified.
- `STORY_WORKER_ENABLED=false`, `MEDIA_WORKER_ENABLED=false` and `YOUTUBE_PUBLISH_WORKER_ENABLED=false` by default. Workers require explicit operator configuration and are not silently activated. The YouTube worker additionally requires the isolated `youtube-publish` Compose profile and its restricted Squid proxy.
- TTS is an endpoint adapter. The operator must select, license and operate the TTS model/runtime/voice pack. No model weights or paid provider are bundled.
- Local abstract PNGs remain the default visual provider. The opt-in Wikimedia Commons adapter retrieves existing CC0/CC BY 4.0 still images with source/license/attribution provenance; it does not generate images or motion-video clips. Production review supports tenant-scoped video/caption preview, downloads and stage diagnostics. Automated image moderation and timeline/scene editing remain outstanding.
- S3-compatible storage supports AWS S3, Cloudflare R2 and MinIO-style endpoints with private path-style requests and SigV4. Live interoperability smoke tests and retention/lifecycle policy remain outstanding.
- Captions emit word-level WebVTT cues, but timing is estimated; a configured forced-alignment provider is not part of the default worker. Technical QA includes black-frame detection and EBU-style loudness/true-peak measurement; freeze-frame detection, detailed clipping/speech-quality checks and verified word/phoneme alignment remain outstanding.
- A native ElevenLabs TTS adapter and FFmpeg background-music mixing/ducking input are available. They require operator configuration; there is no default music-generation/source provider. A branded thumbnail composer utility exists but is not integrated into production artifacts, review or upload.
- Media stages currently call typed provider interfaces directly; they are not all composed through the permissioned skill/tool runtime.
- The daily dispatcher enqueues channel-bound research and production runs; story/media workers execute the downstream stages. Final QA can create a durable YouTube publishing job under explicit channel-owner automatic-publishing consent (PR #144).
- Production approval and YouTube publishing consent remain separate controls. The approval-gated publish-intent API, resumable upload worker and consent-gated publishing handoff are implemented (PRs #137, #141, #142 and #144). The publisher worker is disabled by default and has not been deployed or accepted against a real channel.
- No staging or production cloud resources have been selected or provisioned.

## Phase 2B — Intelligence Layer MVP

**Status: integrated foundations; acceptance work remains.**

The core evidence-backed story vertical slice exists:
1. Tenant-scoped research runs and source/evidence persistence.
2. Bounded research context assembly with source/evidence provenance.
3. Provider-neutral inference and schema-validated approximately 30-second story generation.
4. Separate atomic claim extraction and evidence assessment.
5. Immutable story artifact persistence and editorial review UI.
6. Durable story-stage execution through the permissioned, audited runtime.

Remaining Phase 2B work:
- Complete the partially integrated trend-discovery and multi-source research subsystem defined in ADR-0006: provider-aware trend ranking, opt-in Hacker News and curated RSS/Atom discovery, Crossref abstracts, bounded Wikipedia/Crossref composition and channel-aware dispatch are integrated through PRs #108–#114. Broader policy-reviewed adapters and quality evaluation remain.
- Live-accept and policy-review the opt-in SearXNG factual adapter; add source-quality ranking, publisher lineage, source independence/syndication detection, freshness and evidence conflict reconciliation.
- Expand deterministic evaluation coverage. PR #116 merged adversarial prompt-injection regression fixtures for instruction override, fake role/delimiter injection, credential-exfiltration requests, false evidence authority and multilingual injection. These serialization-boundary tests complement, but do not replace, runtime authorization, schema validation, evidence-manifest validation and editorial review.
- Add tenant-configurable provider selection, encrypted BYOK credentials, model selection by stage, usage visibility, budgets and rate/cost controls.
- Exercise the actual topic-to-reviewed-script flow with a configured local/self-hosted model, not only deterministic fixtures.
- Verify hosted egress restrictions before enabling live retrieval in any hosted environment.

**Exit criteria:** a deterministic local run from a selected topic produces a reviewable, evidence-backed script with claim statuses, source provenance, stage history and recoverable failure behavior. The same workflow can use an explicitly selected compatible model without changing domain logic. Human review must be available, and tenant isolation must be tested.

## Phase 3 — Production Engine

**Status: in progress; durable media workflow and real rendering are integrated.**

Integrated increments:
- PR #85 — tenant-scoped media artifact registry; binaries remain outside PostgreSQL.
- PR #86 — secure atomic local filesystem storage.
- PR #87 — lease-aware artifact persistence, idempotency and compensation on database failure.
- PR #88 — provider-neutral TTS/visual contracts, OpenAI-compatible TTS adapter and deterministic local abstract visuals.
- PR #89 — FFmpeg/ffprobe portrait renderer and initial technical QA.
- PR #90 — enforce output-size limits during encoding and avoid buffering subprocess stderr.
- PR #91 — opt-in durable worker for scene direction, narration, visual generation, rendering and quality checks; valid runs transition to editorial review.
- PR #92 — media-stage orchestration, provenance, integrity and rights-gate tests.
- PR #93 — narration scene partition validation and bounded audio-duration drift.
- PR #94 — real FFmpeg/ffprobe render integration test.
- PR #95 — private S3-compatible storage adapter.
- PR #96 — PostgreSQL-backed end-to-end media worker test with real FFmpeg rendering and persisted artifacts.
- PR #98 — readable scene-local caption cues with estimated word-count timing.
- PR #101 — EBU-style audio loudness normalization, true-peak QA and black-frame detection.
- PR #102 — dedicated FFmpeg-enabled media-worker image, persistent media volume and hardened Compose profile.
- PR #100 — opt-in rights-filtered Wikimedia Commons still-image retrieval, source/license/attribution provenance, exact CC0 1.0 and CC BY 4.0 allowlist, bounded image validation and rendering integration; merged into `develop` and verified by passing PR CI and post-merge Backend CI run #558. Hosted enablement remains blocked on issue #41 network egress controls, and image moderation/scene review remain outstanding.
- PR #128 — validate rendered attribution credits against the source-asset manifest; merged into `develop` as `d677801740a4362dd3086e69dbf1cbdad8342dd2`.
- PR #132 — normalize bounded streaming-WAV RIFF/data size sentinels for OpenAI-compatible TTS servers and document Kokoro-82M/Kokoro-FastAPI as an open-source evaluation candidate; merged into `develop` as `ac9af1b01d0982ee4dc797c55950c25dcc9afb1e`. This does not select or enable a production TTS runtime.
- PR #130 — self-hosted CI optimization: persistent local pnpm/pip caches, frontend path filters, and cache-stable FFmpeg image layer; merged into `develop` as `6af40864b01350c135f881b9109baa68bce0f45c`.
- PR #133 — typed source rights metadata persisted in PostgreSQL, exposed by the research API, captured from Wikipedia revisions/Crossref author records, bounded in story context, snapshotted in StoryArtifact schema 1.2.0 and checked against tampering during claim verification; merged into `develop` as `a852b72b43ec9edd224e8c2665663bacb4a935e4`.
- PR #134 — source identity snapshots in StoryArtifact schema 1.3.0, source rights in direction/render manifests, source attribution in video metadata, and fail-closed final media QA enforcement for unresolved or incompatible source rights; merged into `develop` as `be5cc95713b329af92019c3e3e0c7dda5a8e8c79`.
- PR #135 — tenant-scoped reusable source-rights profiles, immutable review audit records, owner/admin review APIs, tenant-member history APIs, evidence/revision validation and rights inheritance across future research runs; merged into `develop` as `8c63e8a818c203722c67ee2dcb7b585cf973dae2`.
- PR #136 — source-rights review workspace showing rights metadata, exact revision and license references; owner/admin clear/restrict form; evidence URL handling; and immutable review history; merged into `develop` as `873bcdcbcaa2e0a415b84a70b78bf9fe84e42fa0`.
- PR #137 — channel-owner automatic-publishing consent separate from OAuth upload scope, immutable consent history and revocation controls; tenant-scoped idempotent publishing intent with explicit approval, channel-scope, final QA, rights and attribution gates; queued job and audit models; merged into `develop` as `19c76334ef285fc8500f4733feffb24d07f83ade`.

Remaining acceptance work:
- Select and license a production-quality TTS runtime/model/voice pack; validate intelligibility, pronunciation, latency and cost.
- Complete topic-relevant visual selection and safety/moderation around the opt-in Wikimedia Commons adapter; expose scene-level review and ensure CC BY credits are presented in final publishing metadata. Add a local generative-image provider only after model/weight licenses, resource limits and safety controls are reviewed.
- Complete issue #48's remaining media-asset rights review and moderation workflow: ensure per-image/audio/video asset rights can be reviewed with the same audited, tenant-scoped controls; require per-asset license/attribution checks before production use; and expose those findings in rendered-video review.
- Add word/phoneme-level subtitle timing and alignment.
- Add freeze-frame detection, detailed clipping/speech-quality analysis, accessibility checks and true word/phoneme subtitle alignment; black-frame and loudness/true-peak checks are already integrated.
- Complete rights-cleared end-to-end testing with a selected real TTS adapter and assets whose licenses have been reviewed. The current integration test intentionally exercises the blocked-rights path.
- Add orphan-object garbage collection and an approved retention/lifecycle policy.
- Run live interoperability smoke tests against an approved AWS S3, Cloudflare R2 or MinIO environment.
- Compose narration, visual, rendering and QA operations through versioned skills/tools with durable audit events.
- Add resource, concurrency and cost limits for media workers; keep GPU/model serving outside the API container.
- Complete scene-level edit/repair controls and unify per-asset rights/quality findings in the frontend review flow; basic rendered-video/caption preview, download and stage diagnostics are already implemented.

**Exit criteria:** a local deterministic run from an approved story and rights-cleared assets produces a reviewable video with verified narration, captions, media metadata, source/license provenance, stage history, reproducible rendering and tested failure/retry behavior. The rights-cleared path must pass; a deliberately blocked test alone is insufficient.

## Phase 4 — YouTube Publishing and Daily Automation

**Status: integration implemented; operational acceptance outstanding.** OAuth connection, explicit channel-bound publishing consent, daily research-to-production dispatch, consent-gated publishing-job creation and the disabled-by-default resumable upload worker are integrated. Scheduled release timing, production deployment, operational monitoring/alerts and real-channel acceptance remain outstanding.

Required work:
- [x] Implement the YouTube Data API `videos.insert` resumable upload adapter using the selected channel's encrypted refresh token.
- [x] Enforce active tenant/channel binding, upload scope, completed QA, approved production run and exact rendered-artifact integrity before creating an external upload job.
- [x] Require explicit owner/admin approval by default; keep channel-owner automatic-publishing consent separate from OAuth upload scope.
- [x] Persist idempotent upload intent, tenant/job-bound encrypted resumable session state, byte progress, YouTube video ID, processing/visibility state and immutable audit history.
- [x] Handle chunk retries, lease expiry, interrupted uploads, server-offset reconciliation and expired-session outcomes without blindly creating duplicate videos.
- [x] Bind the uploaded artifact, approval, production run/profile snapshot and connected channel through tenant-scoped identifiers.
- [x] Upload privately first and apply approved public/unlisted visibility only after YouTube confirms successful processing; scheduled uploads remain private until their scheduled time.
- [ ] Prove the complete daily schedule → research → production → media QA → consent-gated publishing-job → resumable upload lifecycle with mocked providers and YouTube, preserving timezone/DST, pause/disable, catch-up and idempotency semantics.
- [ ] Deploy the worker with production secrets and verify health, metrics, retry queues, quota handling and operator reconciliation.
- [ ] Add a separately approved real test-channel acceptance run. Never use a real channel without explicit approval.

**Exit criteria:** an explicitly approved test channel completes a controlled end-to-end upload without duplicate videos, cross-tenant access or credential exposure. Automatic daily publication remains disabled unless the channel owner explicitly enables it.

## Phase 5 — Product hardening and beta readiness

- Complete threat modeling and independent security review.
- Add production observability for API, workers, stage latency, queue backlog, provider failures, upload reconciliation and cost.
- Define quotas, per-tenant budgets, resource limits, abuse controls and retention.
- Test backup/restore, disaster recovery, rollback and object-store recovery.
- Load-test media processing and concurrent per-channel schedules.
- Complete accessibility, usability, browser and onboarding checks.
- Establish support, incident response and beta release criteria.

**Exit criteria:** documented acceptance evidence and an explicit beta release decision.

## Phase 6 — Production deployment decision

Production hosting remains deliberately deferred until workload, media storage, processing duration, concurrency, GPU/CPU requirements, region/data residency, availability, recovery objectives, privacy obligations and budget are known.

Compare total cost of ownership (compute, GPU, database, Redis, storage, egress, email, logs and monitoring), deployment/rollback controls, backup/restore guarantees, networking/IAM, secrets, regional availability, operational burden and migration/exit paths.

Do not create billable resources, expose the service publicly, use real customer data, send external test emails or connect a real publishing channel without the required explicit approval. A free tier is suitable for experiments only when its sleep, quota, storage, uptime and recovery limitations are acceptable.

## Delivery strategy

1. Maintain `develop` as the integration branch and `main` as the release branch.
2. Deliver focused changes through short-lived feature branches and pull requests.
3. Define acceptance criteria before implementation.
4. Add deterministic unit tests for domain rules and regression tests for defects.
5. Exercise database, API, queue, provider and workflow boundaries with integration tests.
6. Run linting, tests, migration checks, dependency audits and build validation in CI.
7. Review security, privacy, tenant isolation, failure handling and operational impact with each change.
8. Update architecture decisions, roadmap and runbooks alongside implementation.
9. Merge only after the relevant checks pass and the change is reviewed.
10. Never claim a test, audit, deployment or recovery exercise passed without evidence.

## Architecture principles

### Modular monolith first

Keep a modular monolith until measured scale, independent release needs or clear ownership justify extracting a service. Separate modules by business capability and enforce boundaries through typed contracts, dependency direction and tests.

### Ports and adapters

Keep domain/application logic independent of infrastructure vendors. Use narrow interfaces for:
- LLM inference and model serving.
- Research/search and source retrieval.
- Skills and bounded tool integrations.
- TTS, visual generation/retrieval and media inspection.
- Object/media storage.
- Durable background execution.
- YouTube and future publishing platforms.

Provider-specific SDKs, credentials, HTTP details and retry semantics belong in adapters. Never select a paid provider implicitly.

### Durable workflows

Persist each production run, stage, attempt, lease, artifact reference, approval and terminal outcome. Make handlers idempotent or define explicit reconciliation at external side-effect boundaries. PostgreSQL is authoritative for production workflow state; do not rely on in-memory state or Redis queue state for work that must survive restarts.

### Tenant isolation and authorization

Derive tenant context from authenticated identity and active membership. Enforce authorization on every tenant-scoped read/write, worker operation, media artifact and external side effect. Add negative tests proving that one tenant cannot access another tenant's records, channels, credentials, jobs or media.

### Security, provenance and human control

Use least privilege, secret management, secure sessions, encryption for sensitive tokens, strict input validation, safe logging and dependency hygiene. Treat prompts, retrieved pages, captions, files and model outputs as untrusted. Preserve source/evidence provenance for factual claims and per-asset license/attribution metadata for media. Publishing requires explicit human approval by default; no worker may bypass channel-bound consent or workflow state.

### Identity-aware production

ADR-0004 defines future tenant-scoped voice/character identities, consent-aware voice profiles, AI Director plans, performance/continuity checks and targeted regeneration. These capabilities are architectural direction, not current implementation claims. Voice cloning requires documented, purpose-bound consent and revocation/deletion controls; standard TTS remains available without cloning.

## Testing and quality gates

| Gate | Expected evidence |
|---|---|
| Static quality | Ruff, TypeScript/build checks and relevant type validation pass |
| Unit behavior | Domain rules, schemas, authorization and edge cases are covered |
| Integration | PostgreSQL, Redis, API, worker, provider and storage boundaries are exercised |
| Security | Tenant isolation, secrets, SSRF, prompt injection, consent and rights gates are tested |
| Reliability | Timeouts, retries, leases, idempotency, cancellation and recovery are covered |
| Data changes | Alembic migrations are applied from a clean database and upgrade paths are checked |
| Media | Real FFmpeg/ffprobe integration, size/duration/codec/audio/caption checks and artifact hashes |
| Publishing | Mocked resumable uploads, exact-channel checks and uncertain-outcome reconciliation |
| Frontend | Vitest, TypeScript production build and error-state coverage |
| Dependencies | Lockfiles are current; runtime and development dependencies are audited |
| Containers | Docker/Compose validation and disposable runtime smoke test pass |
| Documentation | Setup, configuration, behavior, limitations and operations match the implementation |

A green CI run certifies only the checks configured in that workflow. Missing checks must remain explicit acceptance gaps.

## Open engineering issues

- **#21:** staging deployment plan — deferred; no hosting provider or cloud resources selected.
- **#34:** email outbox retention/archive policy — no automatic deletion until policy approval.
- **#35:** end-to-end daily acceptance, production worker deployment/health, quota/operational observability, operator reconciliation tooling and approved real-channel acceptance. The resumable upload worker, token hardening and consent-gated publishing handoff are implemented in PRs #141, #142 and #144.
- **#37:** immutable container base-image digests and production dependency/image update policy; Python lockfiles and dependency audits are already integrated.
- **#38:** branch protection for `develop` and `main` — GitHub API currently reports protection disabled and required status-check enforcement off for both branches; settings remain unchanged.
- **#41:** deployment-level network egress enforcement before hosted live research.
- **#48:** source/media licensing, attribution and reuse constraints across the complete workflow.
- **#49:** remaining Intelligence Layer acceptance work, including broader research, evaluations, BYOK and budgets.
- **#50:** remaining Production Engine acceptance work described above.
- **#51:** full skill/tool composition, evaluations, media-stage tools, UI catalog, BYOK and budgets.

## Per-channel editorial profile and scheduling checklist

- [x] Bind each production run to an active YouTube channel in the same workspace.
- [x] Store immutable, versioned editorial profiles and snapshot the exact version into each production run.
- [x] Persist per-channel daily schedule, IANA timezone, local posting time, pause state and catch-up policy.
- [x] Require separate YouTube upload consent before enabling a daily publishing schedule.
- [x] Dispatch due schedules transactionally into channel/profile-bound research runs.
- [x] Hand completed scheduled research into an idempotent production run.
- [x] Bind the completed video artifact, approval, production run/profile snapshot and connected channel through tenant-scoped identifiers.

## Release and hosting policy

`develop` is the active integration branch. `main` is the release branch and is substantially behind current product development. Branch protection must be verified for both branches before production release; do not assume required checks or force-push restrictions are active.

The development/demo target is free or near-zero cost with local Docker as the reliable baseline. Keep the application portable through OCI containers, PostgreSQL, documented environment configuration, provider adapters, S3-compatible storage and explicit migration procedures. Production approval is a separate decision; completing product development does not automatically authorize provisioning or spending.

## Knowledge and Media Library — architecture accepted, implementation not started

ADR-0005 and the [Knowledge and Media Library architecture](../architecture/knowledge-and-media-library.md) define the proposed subsystem for curated creative knowledge, source provenance, rights-aware assets, ingestion, search and portable object storage. This is an architectural decision, not a claim that these capabilities are already implemented.

The design reuses PostgreSQL, the existing media artifact registry, MediaStorage, LocalMediaStorage, S3CompatibleMediaStorage and the opt-in FFmpeg worker. It does not add a second storage abstraction or select a production provider. SeaweedFS, RustFS and Garage remain self-hosted candidates; Cloudflare R2, AWS S3, Backblaze B2 and other managed services remain optional provider choices pending compatibility, recovery, security and workload-cost tests.

Delivery gates are: (1) audit and harden current storage contracts, (2) rights/provenance registry and production-use enforcement, (3) tenant-safe knowledge and asset catalogue, (4) approved source connectors and safe ingestion, (5) full-text search followed by optional embeddings/time-coded segments, and (6) permissioned agent retrieval. Unknown, research-only, restricted, expired or revoked assets must not enter production. No copyrighted film/episode/script bulk ingestion, paid fallback, new cloud resource or worker activation is implied by this architecture.



## Topic discovery and multi-source research — partial implementation integrated

ADR-0006 and the [Topic Discovery and Research Intelligence architecture](../architecture/topic-discovery-and-research-intelligence.md) establish separate discovery and factual-research ports. Implemented increments now include deterministic trend-observation ranking (PR #108), opt-in Hacker News discovery (#109), operator-curated RSS/Atom feeds (#110), Crossref scholarly abstracts (#111), bounded Wikipedia/Crossref multi-source composition (#112), channel-aware trend selection (#113) and curated-RSS daily dispatch (#114). Trend observations remain discovery leads and must never be treated as factual evidence. RSS headlines are not fetched as evidence; factual research remains a separate workflow.

Remaining increments are: (1) live acceptance and policy review of the SearXNG factual adapter with SSRF-resistant result fetching; (2) broader primary/secondary source coverage; (3) source class, freshness, publisher lineage, content-hash and independence/syndication assessment; (4) evidence conflict/reconciliation and atomic claim-to-excerpt traceability; (5) richer per-channel discovery-to-reviewed-script evaluation; and (6) tenant-level provider/model configuration, encrypted BYOK, budgets and quotas. Google Trends supported interfaces, YouTube Data API search metadata, policy-reviewed Reddit API and Wikidata remain candidates, not integrated providers. No undocumented endpoint, prohibited scraping, paid fallback or unreviewed provider is assumed.

Hosted live retrieval remains disabled until issue #41 egress controls are implemented and verified. CI and default local development remain mock-first. Source licensing/terms, privacy, rate limits, prompt-injection defenses, tenant isolation and cost/quota limits are acceptance gates.
