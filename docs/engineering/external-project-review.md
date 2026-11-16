# External project review: YouTube Automation Agent

Reviewed on 13 November 2026: the original [darkzOGx/youtube-automation-agent](https://github.com/darkzOGx/youtube-automation-agent), including its [feature guide](https://github.com/darkzOGx/youtube-automation-agent/blob/master/docs/features.md) and [MIT license](https://github.com/darkzOGx/youtube-automation-agent/blob/master/LICENSE). This document tracks feature-level reproduction in Aurora; no source code was copied.

## What Aurora already has

Aurora already covers evidence-linked research and factual claim checks, durable staged production with retries, media QA and rights provenance, per-channel schedules, human review, explicit publishing consent, resumable YouTube uploads, and a provider-neutral architecture. Replacing Aurora with the reference Node.js application would discard its multi-tenant controls, durable production records, rights gates, and established Python/FastAPI integrations.

## Feature comparison and adaptation

| Reference capability | Aurora status | Recommended next step |
| --- | --- | --- |
| Multiple LLM providers | Named OpenAI, OpenRouter, DeepSeek, Hugging Face, Gemini, and Groq chat endpoints use Aurora's normalized provider adapter; Anthropic Claude uses its native Messages API adapter; custom OpenAI-compatible local endpoints remain available. | Keep provider selection operator-controlled, secrets out of tenant input, and account for vendor-specific model IDs, costs, and feature differences. |
| Production readiness | Workspace configuration preflight exists; it does not make live probes | Add bounded probes for TTS, renderer, channel authorization, and queued-upload metadata. Keep paid image/video probes separate and opt-in. |
| Resumable generation | Durable stages, leases, retries, and cancellation exist | Improve operator visibility and safe resume/reconcile controls around existing stages. |
| Evidence desk and review | Research evidence, source-rights review, and claim assessment exist in separate work areas | Bring claim evidence and media attribution into one final-video review surface. |
| YouTube Analytics | Read-only daily metrics are fetched for an exact tenant-owned channel and upserted as tenant-bound daily snapshots. Complete seven-day windows produce a persisted human review cue; an approved cue is passed only to future story generation for that channel as optional creative context. Revenue is excluded; empty or incomplete reports remain unavailable, not zero. | Add cross-channel baselines and controlled learning experiments; retain normal story review and publishing consent. |
| Outcome/ROI and controlled experiments | Not implemented | Add after cross-channel baselines and quota/cost guards are available; retain explicit review before applying any experiment. |
| Shorts repurposing | Not implemented; Aurora's primary output is already short-form | Evaluate whether scene-based alternate cuts help the daily-video goal; retain scene provenance, rights, and separate approval. |
| Comment insights and reply drafts | Not implemented | Consider topic/question summaries and reply drafts; posting replies needs explicit approval and spam controls. |
| Scene repair and spend limits | Durable media stages exist; fine-grained repair and provider spend budgets remain outstanding | Add per-scene regeneration and explicit cost caps after provider configuration and usage accounting. |

## Suggested order

1. Prove the rights-cleared daily production and YouTube upload path on a controlled test channel. Keep human approval and channel-bound upload consent explicit.
2. Join evidence, rights, and technical QA into one review surface, and add non-publishing verified readiness probes.
3. Add cross-channel baselines and controlled learning experiments after enough channel history exists.
4. Evaluate alternate Shorts, comment insights, experiments, and scene-aware retention after privacy, quota, and operator controls are designed.

The reference feature guide describes its behavior; it does not prove Aurora's credentials or real-channel publishing are configured. MIT permits code reuse if the copyright and permission notice remains in copies or substantial portions. Aurora's current work reproduces selected behavior in its own architecture without copying source files.

## 3Blue1Brown / ManimGL reference

The official [3b1b/manim repository](https://github.com/3b1b/manim) is an MIT-licensed programmatic animation engine for explanatory mathematics. Its strongest fit is a specialist explainer-video renderer: clean vector diagrams, equations, transformations and camera movement. It is not a drop-in replacement for Aurora's short-form video pipeline or a general AI video model. ManimGL also brings rendering and LaTeX/native dependency weight, so it should be an optional isolated worker/profile rather than a dependency installed in the core API or every developer environment.

The referenced seven YouTube Shorts could not be fetched by the available web reader, so their specific shot, pacing, graphic and audio patterns have not been verified. Avoid promising an exact style match until those references can be inspected. A sensible implementation direction is a selectable short-form visual treatment system (cinematic b-roll, kinetic typography, clean infographic, and math/explainer animation), with ManimGL considered only for the last treatment. Generate from Aurora-owned scene specifications and safe templates; do not execute model-written Python or copy distinctive creator assets.

### Candidate creative integrations

- **Higgsfield API:** an opt-in, budget-capped hosted image/video generation adapter. Use only model-specific documented schemas, persist remote request IDs for recovery/cancellation, validate downloaded media, and never issue generation requests during readiness checks unless a paid probe is separately enabled.
- **MCP:** an optional adapter over Aurora's already authorized typed tools. External MCP tool descriptions must not grant permissions; server identity, transport, secrets, tenant ownership, operator approval, timeouts and audit records remain Aurora-controlled.
- **Media skills:** add versioned, typed skills for shot planning, narration direction, captions, scene repair/remix, thumbnail composition and final MP4 QA, each backed only by bounded application tools and rights checks.
