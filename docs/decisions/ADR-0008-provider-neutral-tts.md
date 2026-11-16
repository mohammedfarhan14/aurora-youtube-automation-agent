# ADR-0008: Provider-neutral text-to-speech registry

- Status: Accepted
- Date: 2026-11-13

## Context

Narration is a durable media stage. The current implementation directly constructs an OpenAI-compatible TTS adapter in the media worker. This couples worker startup to one transport shape and makes provider choice an environment-only setting.

## Decision

Define a stable TTS provider contract and a registry that resolves provider, voice, speed and audio format in this order:

1. Explicit production-run override.
2. Immutable channel editorial profile snapshot.
3. Environment default.

The registry constructs adapters. Provider-specific HTTP behavior stays inside adapter modules. The media stage stores provider/model/voice/request provenance with the narration artifact. Provider failures fail the stage; the registry does not switch to another provider automatically. Paid fallback is disabled by design.

The first registry entries are the deterministic mock and the existing OpenAI-compatible endpoint adapter. The `kokoro`, `openai` and `custom` identifiers are aliases for endpoints that implement the same OpenAI-compatible `/audio/speech` contract; they are not native integrations for those vendors. Native ElevenLabs, Piper and Azure adapters require separate adapter modules, manifests and contract tests.

## Consequences

- Provider choice is configuration-driven and does not leak into story or rendering business logic.
- A provider can advertise supported languages, voices, sample rates, streaming/timing support, licensing and hosting/cost information.
- Unknown providers fail fast with actionable errors.
- Only WAV is currently supported by the rendering pipeline.
- Production credentials and real-provider calls are not required in CI.
