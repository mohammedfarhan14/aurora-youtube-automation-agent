# Native ElevenLabs TTS adapter

Aurora Studio's `elevenlabs` provider is a native adapter, not an OpenAI-compatible `/audio/speech` endpoint. It calls `POST /v1/text-to-speech/{voice_id}` with the vendor's `xi-api-key` header, `model_id`, `voice_settings.speed`, and `output_format=pcm_24000`. The raw PCM S16LE response is wrapped in a standard mono 24 kHz WAV using Python's standard library so the existing FFmpeg media contract remains unchanged.

The adapter reports hosted infrastructure, no streaming, and no word timings. It does not use the ElevenLabs SDK or add a runtime dependency. All HTTP contract tests use `httpx.MockTransport`.

For a production worker, configure:
- `MEDIA_TTS_PROVIDER=elevenlabs`
- `MEDIA_TTS_BASE_URL=https://api.elevenlabs.io`
- `MEDIA_TTS_API_KEY` (stored as a secret; sent only as `xi-api-key`)
- `MEDIA_TTS_MODEL` and a valid `MEDIA_TTS_VOICE`
- `MEDIA_TTS_HOSTING_MODE=hosted`
- Accurate model/voice license identifiers, license URI and attribution consistent with the selected account, voice and commercial usage rights.

The provider uses PCM 24 kHz, so the account/model must permit that output format. Production also requires real API credentials, a valid voice ID, account credits/concurrency capacity, and a reviewed license/voice-rights basis. No real credentials or paid API calls are used in tests.
