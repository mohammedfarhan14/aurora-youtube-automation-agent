# ADR-0003: Provider-independent AI runtime and versioned skills

- **Status:** Accepted
- **Date:** 2026-11-11
- **Decision owners:** Aurora Studio maintainers

## Context

Aurora Studio must produce one evidence-backed 30-second video per connected YouTube channel per day. The product needs research, story writing, factual verification, narration, visual planning/generation, editing, rendering, quality assurance and publishing preparation.

The project prioritizes open-source technology and portable self-hosting. Some users may nevertheless prefer frontier LLMs for selected tasks. The architecture must support both without coupling domain logic, persisted artifacts or workflows to a single vendor.

The product also needs reusable production skills and controlled tool integrations. Prompt strings embedded in endpoints are not a maintainable or safely governable skills system.

## Decision

### 1. Separate four concepts

- **Model provider:** performs inference. Examples include local Ollama, self-hosted vLLM and optional frontier APIs.
- **Skill:** a versioned capability definition with purpose, instructions, typed input/output schemas, required model capabilities, declared tool permissions, guardrails and evaluation fixtures.
- **Tool/integration:** a bounded action behind a typed interface, such as research retrieval, source parsing, image generation, TTS, media inspection, FFmpeg rendering or YouTube operations.
- **Workflow/agent:** durable orchestration of skills and tools, including dependencies, approvals, retries, cancellation, idempotency and audit events.

Do not conflate a skill with an LLM, a tool or a workflow.

### 2. Open-source-first inference

- Deterministic mock providers are the CI and unit-test default.
- Ollama-compatible local inference is the first local development target.
- Self-hosted inference such as vLLM is a replaceable deployment option, not a mandatory dependency.
- Keep inference APIs provider-neutral and support OpenAI-compatible endpoints where practical.
- Do not require a GPU or model server in the API/control-plane container.

### 3. Optional frontier providers

Support optional adapters for frontier providers (initial candidates: OpenAI, Anthropic and Google) and compatible endpoints.

- Provider choice is explicit per tenant and can be configured per skill/workflow stage.
- Support BYOK with encrypted-at-rest credentials and strict secret redaction.
- Persist provider/model identifiers and execution metadata, never raw secrets.
- Enforce per-tenant budgets, rate limits, timeout/token limits and usage visibility.
- Never silently fall back from a local or selected provider to a paid frontier provider.
- A provider outage must fail or follow a tenant-approved fallback policy.

### 4. Skills are versioned and permissioned

Each skill definition must have an immutable version identifier, strict input/output schemas, an explicit tool allowlist, model capability requirements, guardrails and deterministic evaluation fixtures. Validate every input/output and tool argument at runtime.

Model-generated text must never grant itself additional permissions. No arbitrary shell execution, unrestricted filesystem access or unrestricted network access is exposed to skills. Tools authorize tenant context and permissions server-side, apply time/resource budgets, and emit sanitized audit records.

Treat retrieved web pages, documents, media metadata, captions and model outputs as untrusted data. Prompt injection in such content must not modify tool policy or workflow instructions.

### 5. Tool interoperability

Internal typed tool contracts are the source of truth. MCP can be added as an optional adapter for compatible external tool servers; the core product must not require MCP or trust external tool descriptions as authorization policy.

### 6. Initial production skill catalog

- Topic discovery and editorial planning.
- Multi-source research and source-quality assessment.
- Story structure, hook and 30-second script writing.
- Atomic claim extraction and evidence-grounded verification.
- Script timing and pronunciation preparation.
- Scene breakdown and shot/visual prompt planning.
- Asset rights, license and attribution review.
- Narration and audio QA.
- Captions/subtitles and alignment.
- Edit decision planning and FFmpeg render specification.
- Render QA (duration, aspect ratio, codec, audio, subtitle sync, black frames and file integrity).
- YouTube metadata/thumbnail preparation and publishing readiness.

Actual upload and automatic publishing remain governed by the separate YouTube publishing workflow and explicit channel-owner consent.

## Consequences

### Positive

- Local/open-source deployments remain first-class.
- Frontier models can be adopted selectively without rewriting business logic.
- Skills can be tested, versioned, reused and improved independently.
- Tool access is constrained, auditable and tenant-aware.
- Provider/model changes do not invalidate domain artifacts or workflow state.

### Trade-offs

- Provider adapters, credential handling, model capability metadata and usage accounting add engineering work.
- Skill schemas, version compatibility and evaluation datasets require ongoing maintenance.
- Self-hosted inference has hardware, latency and operational costs even when the model software is open source.

## Implementation plan

- Issue #49: provider-neutral inference and evidence-backed story/script/claim verification.
- Issue #51: versioned skills, tool contracts, evaluations and permissioned execution.
- Issue #50: narration, visual assets, media storage, rendering and render QA.
- Issue #35: controlled YouTube upload, scheduling and explicit opt-in auto-publishing.

## Security and release gates

No skill may access a tool that is not declared and authorized. No tenant secret may appear in logs or model-visible context. No automatic paid-provider fallback is allowed. No factual script may pass review without evidence traceability. No media may pass publishing QA without rights/attribution checks. No automatic upload may occur without explicit channel-bound publishing consent and owner enablement.
