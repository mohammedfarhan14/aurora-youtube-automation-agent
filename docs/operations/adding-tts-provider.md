# Adding a TTS provider

Aurora Studio routes narration through `TTSProviderRegistry` and the shared
`TTSProvider` contract. Story generation, captioning and video rendering must
not contain vendor-specific branches.

## Contract

Every adapter implements:

- `synthesize(text, voice_id, language, speed, audio_format)` and returns an
  `AudioAsset` containing validated audio bytes, duration, sample rate,
  channel count, provider/model/voice identifiers, request ID and optional
  word timings.
- `capability_manifest`, including languages, voices, sample rates, streaming
  and word-timing support, model/voice license, attribution, cost and hosting.
- `health_check()` and `aclose()`.

## Resolution order

The narration stage resolves each setting independently:

1. Production-run override supplied when creating the run.
2. Immutable channel editorial profile snapshot.
3. Environment default (`MEDIA_TTS_PROVIDER`, `MEDIA_TTS_VOICE`,
   `MEDIA_TTS_SPEED`, `MEDIA_TTS_FORMAT`).

A run override is persisted inside the immutable production-run profile
snapshot. Reusing an idempotency key with a different TTS override is rejected.

## Add an adapter

1. Add one module under `app/media/tts_providers/` (or extend an existing
   adapter module).
2. Implement `TTSProvider`; keep HTTP/auth/response parsing inside the adapter.
3. Add one explicit factory entry in `TTSProviderRegistry`.
4. Publish an honest capability manifest. Use unknown/unspecified values when
   the provider does not expose a capability; do not invent supported voices,
   language coverage or licensing.
5. Add adapter contract tests for valid audio, malformed/oversized responses,
   timeouts, health checks, capability manifest and cleanup.
6. Run the same synthesis contract tests with the deterministic mock and the
   new adapter.
7. Document required settings, voice/model license, attribution and cost.

The current OpenAI-compatible adapter is also registered under `kokoro`,
`openai` and `custom` identifiers when the configured endpoint implements
the same `/audio/speech` contract. Languages, voices, sample rates, hosting
mode, licenses, attribution and cost are operator-declared in configuration;
the adapter enforces the declared language, voice and sample-rate values.
This adapter currently supports buffered WAV only and reports no streaming or
word-timing support. These aliases do not implement native ElevenLabs, Piper
or Azure APIs.

## Safety and fallback

- Unknown or misconfigured providers fail with an actionable error.
- Provider failure fails the narration stage and uses the durable stage retry
  and alert path.
- No automatic fallback is performed.
- Paid fallback is disabled. A future fallback requires explicit allowlisting
  and explicit operator configuration.
- The mock tone provider is non-production only and does not produce speech.
- Verify model and voice licenses separately. Provider API terms alone do not
  establish commercial reuse rights.
- Never put API keys in channel profiles or per-run overrides. Credentials
  remain in deployment secrets/environment configuration.
