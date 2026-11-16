# ADR-0004: Identity-aware AI media production and directed continuity

- **Status:** Accepted
- **Date:** 2026-11-12
- **Decision owners:** Aurora Studio maintainers

## Context

Aurora Studio is not merely a script-to-video wrapper. It must support reusable voice and character identities, expressive performance direction, deliberate cinematography, and coordinated production across independent media workers. These capabilities must coexist with tenant isolation, evidence provenance, provider independence, open-source-first operation, portable deployment, and explicit human control over publishing.

The local development machine is not a suitable target for assuming that large video-generation models can run. The control plane must remain usable without a local GPU, and expensive remote inference must never be selected implicitly.

## Decision

Implement identity-aware production as domain capabilities in the existing modular monolith. Keep the orchestration durable and provider-neutral. Do not introduce microservices solely to represent the conceptual engines described here; define module boundaries and worker contracts first.

### 1. Identity Registry

The Identity Registry is the tenant-scoped system of record for reusable production identities and their authorized assets.

It manages:

- Voice identity profiles, including whether a profile uses standard TTS or consent-based cloning.
- Character identity profiles with canonical descriptions, reference images/assets, appearance attributes, clothing, hairstyle, proportions, distinctive features, style constraints and version history.
- Optional relationships between characters and stable identifiers used by scripts, scenes and shots.
- Consent, authorization, provenance, lifecycle state, revocation and deletion records for identity assets.

Every identity and asset must be organization-scoped. Authorization is checked server-side for every read, write, generation request and worker operation. Asset references must not be transferable across tenants by guessing identifiers.

Voice cloning is permitted only for the user's own voice or a voice for which the user has documented authorization. Consent records must identify the subject/authorizing party, permitted purpose and scope, capture/recording provenance, timestamp, terms/version and revocation state. A revoked identity must be blocked from future generation. Deletion must cover source recordings, derived embeddings where controllable, cached derivatives and provider-side copies where the provider supports deletion; limitations must be disclosed and audited. Standard non-cloned TTS remains available.

### 2. AI Director

The AI Director converts an approved story and script into a typed, versioned production plan. It coordinates domain decisions but does not itself gain unrestricted tools or execute arbitrary code.

Its plan includes:

- Scene and shot breakdowns, duration targets and dialogue assignments.
- Voice identity, language, pronunciation, pacing, emphasis, pauses, pitch/intensity and emotional-delivery instructions.
- Character identity references and per-shot continuity constraints.
- Performance direction: emotion, facial expression, eye direction, gestures, posture, movement and dialogue timing.
- Cinematography: shot size/type, composition, camera angle and movement, lens characteristics, lighting, focus, depth of field and transitions.
- Audio direction, music/SFX cues, captions and lip-sync requirements.
- Required assets, generation capabilities, rights/attribution requirements and quality gates.

Plans are persisted as immutable versions. Regeneration creates a new version or targeted replacement with lineage; it must not silently overwrite approved artifacts.

### 3. Performance and Continuity Engine

The Performance and Continuity Engine coordinates scene-level performance and cross-shot consistency. It compares generated media against the approved direction and identity registry, including:

- Voice identity and delivery consistency.
- Character appearance, wardrobe, hairstyle, proportions and distinguishing features.
- Emotion and facial-expression alignment.
- Eye direction, gestures, posture, movement and dialogue timing.
- Lip-sync and audio/video alignment.
- Shot-to-shot visual continuity and compliance with cinematographic intent.

Checks produce structured findings, confidence/measurement metadata where available, severity and affected time ranges/assets. Low-confidence automated checks must not be represented as certainty. Failed checks may request targeted regeneration, alternate providers or human review. The engine must not claim perfect generative consistency.

### 4. Independent media workers and provider adapters

Narration, voice conversion/cloning, image generation, video generation, animation, lip-sync, music/SFX, media inspection and FFmpeg rendering are independent capabilities behind typed ports.

Each adapter declares supported capabilities, constraints, model/provider/version identifiers, resource requirements, licensing/usage terms and optional cost metadata. Workflow planning must validate capability requirements before dispatch.

- Deterministic mock adapters are the CI default.
- Open-source/self-hosted adapters are preferred where their quality and operational requirements fit.
- Optional hosted providers require explicit configuration and, where applicable, explicit tenant opt-in.
- Never silently fall back to a paid provider or incur charges.
- Do not require GPU/model servers in the API container.
- Heavy generation may run on a separately provisioned worker pool; no assumption is made that remote GPU capacity is free.
- Persist provider/model/version and sanitized execution metadata with every artifact.

### 5. Durable orchestration, provenance and review

A production run is a durable, tenant-scoped state machine. It records the approved input versions, story/research references, plan versions, stage attempts, worker leases, artifacts, quality findings, approvals, cancellations and terminal outcomes.

All stage handlers must be idempotent or use explicit idempotency keys at side-effect boundaries. Retries, cancellation, lease expiry, worker restart and recovery must be tested. Workers derive tenant context from persisted authorized run records, never from untrusted model output.

Every factual claim retains links to its evidence and research provenance through story, scene and rendered artifact metadata. Generated media records retain identity references, prompt/plan versions, provider/model versions, licensing/attribution metadata and derivation lineage.

Human review is required before a video can enter the publishable state. YouTube upload and automatic scheduling remain behind channel-bound consent and explicit owner enablement. No worker may bypass approval.

### 6. Quality assurance and targeted regeneration

Quality gates are modular and typed. They may include:

- Audio integrity, clipping, loudness, silence and speech intelligibility checks.
- Subtitle timing and caption alignment.
- Lip-sync and dialogue timing.
- Visual identity and continuity checks.
- Black-frame, freeze, corruption, aspect ratio, duration, codec and render-integrity checks.
- Factual evidence coverage, claim verification and source attribution.
- Asset rights, license and policy checks.

A finding identifies the failed requirement, evidence, affected stage/asset/time range and recommended action. Targeted regeneration must preserve unaffected approved outputs and maintain lineage. Human review is required when a check is inconclusive, repeated regeneration fails, or a policy/rights decision cannot be automated reliably.

## Required domain boundaries

- `identity`: tenant-scoped voice/character profiles, assets, versions and consent lifecycle.
- `direction`: typed AI Director plans, scenes, shots and performance/cinematography instructions.
- `media`: provider-neutral media capability contracts and generated asset metadata.
- `continuity`: identity/performance/lip-sync checks and structured findings.
- `production`: durable run orchestration, stages, attempts, leases, artifacts, approvals and targeted regeneration.
- `quality`: reusable audio, visual, factual, rights and render quality gates.

These are logical boundaries within the modular monolith initially. Extract a worker or service only when independent scaling, isolation or deployment lifecycle justifies it.

## Security, privacy and tenant isolation

- Tenant scope is mandatory for every identity, asset, production run, artifact, finding and approval.
- Never expose raw voice recordings, embeddings, OAuth tokens or provider credentials to logs or unrelated model context.
- Encrypt sensitive source recordings and identity artifacts at rest and in transit; restrict access and audit access to them.
- Consent is explicit, purpose-bound, versioned and revocable. No non-consensual voice cloning or impersonation.
- Treat prompts, references, retrieved content and model outputs as untrusted data.
- Model output cannot grant permissions, choose unapproved paid providers or bypass workflow state transitions.
- Provider adapters must enforce timeouts, size/resource limits, tenant authorization and safe secret handling.
- Retention, export and deletion behavior must be defined for source and derived media, including provider deletion limitations.

## Consequences

### Positive

- Voice and character identity become reusable first-class product data rather than ad hoc prompt text.
- Creative direction is explicit, reviewable and reproducible.
- Media providers can be replaced without changing workflow/domain contracts.
- Continuity failures can be detected and repaired at targeted scope.
- Tenant isolation, consent, provenance and human approval remain end-to-end properties.

### Trade-offs

- Identity asset security, consent lifecycle and deletion require careful implementation.
- Automated continuity metrics are probabilistic and require human review paths.
- Multi-modal workers add storage, compute, queueing and licensing complexity.
- Open-source models may have significant hardware, latency and quality constraints.
- Versioned plans and artifact lineage add persistence and migration complexity.

## Implementation sequence

1. Complete the durable story-production workflow: research context, story generation, atomic claim verification, persisted artifacts and tenant-scoped stage transitions.
2. Add the production-run and artifact foundation with idempotency, leases, retries, cancellation and recovery.
3. Add Identity Registry contracts and persistence, starting with character profiles/assets and consent-aware voice profiles without enabling cloning by default.
4. Add typed AI Director plan schemas for scenes, shots, performance and cinematography.
5. Add media capability ports and deterministic mock workers; then add open-source adapters behind explicit configuration.
6. Add continuity and quality findings with targeted regeneration and human review.
7. Integrate audio/video rendering, captions, lip-sync and publishing readiness.
8. Keep upload behind explicit human approval and channel-bound consent.

Each increment must include tenant-isolation tests, migration validation, deterministic tests, provider-mock tests, operational failure tests and documentation. This ADR does not authorize production deployment, paid inference, voice cloning without consent, or automatic publishing.
