import io
import json
import wave

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.media.tts import TTSProviderError
from app.media.tts_registry import TTSProviderRegistry


def _settings():
    return Settings(
        _env_file=None,
        media_tts_provider="elevenlabs",
        media_tts_base_url="https://api.elevenlabs.io",
        media_tts_api_key=SecretStr("mock-elevenlabs-key"),
        media_tts_model="eleven_multilingual_v2",
        media_tts_voice="test-voice",
        media_tts_hosting_mode="hosted",
        media_tts_rights_basis="licensed",
        media_tts_license_id="elevenlabs-commercial-terms",
        media_tts_license_uri="https://elevenlabs.io/terms-of-use",
        media_tts_source_uri="https://elevenlabs.io/app/voice-library",
        media_tts_attribution="ElevenLabs voice generated under configured account terms",
        media_tts_languages=["en-US"],
        media_tts_voices=["test-voice"],
        media_tts_sample_rates_hz=[24_000],
    )


@pytest.mark.anyio
async def test_elevenlabs_native_adapter_satisfies_tts_contract_with_mock_http():
    pcm = b"\x00\x00" * 24_000
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path == "/v1/models":
            return httpx.Response(200, json={"models": []})
        if request.method == "POST" and request.url.path == "/v1/text-to-speech/test-voice":
            return httpx.Response(
                200,
                headers={"content-type": "audio/pcm", "request-id": "mock-request-123"},
                content=pcm,
            )
        return httpx.Response(404)

    registry = TTSProviderRegistry(_settings(), transport=httpx.MockTransport(handler))
    provider = registry.create("elevenlabs")
    try:
        assert provider.capability_manifest.provider_id == "elevenlabs"
        assert provider.capability_manifest.hosting_mode == "hosted"
        assert provider.capability_manifest.supports_word_timings is False
        assert await provider.health_check()
        audio = await provider.synthesize(
            text="A short test narration.",
            voice_id="test-voice",
            language="en-US",
            speed=1.1,
            audio_format="wav",
        )
    finally:
        await provider.aclose()

    assert audio.provider_id == "elevenlabs"
    assert audio.mime_type == "audio/wav"
    assert audio.sample_rate_hz == 24_000
    assert audio.channels == 1
    assert audio.duration_ms == 1_000
    assert audio.request_id == "mock-request-123"
    assert len(requests) == 2
    synth_request = requests[1]
    assert synth_request.headers["xi-api-key"] == "mock-elevenlabs-key"
    assert synth_request.url.params["output_format"] == "pcm_24000"
    body = json.loads(synth_request.content)
    assert body["model_id"] == "eleven_multilingual_v2"
    assert body["text"] == "A short test narration."
    assert body["voice_settings"]["speed"] == 1.1
    with wave.open(io.BytesIO(audio.content), "rb") as wav:
        assert wav.getframerate() == 24_000
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 24_000


@pytest.mark.anyio
async def test_elevenlabs_native_adapter_rejects_invalid_speed_and_pcm():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "audio/pcm"},
            content=b"\x00",
        )

    registry = TTSProviderRegistry(_settings(), transport=httpx.MockTransport(handler))
    provider = registry.create("elevenlabs")
    try:
        with pytest.raises(ValueError, match="speed"):
            await provider.synthesize(
                text="A short test narration.",
                voice_id="test-voice",
                language="en-US",
                speed=1.5,
            )
        with pytest.raises(TTSProviderError, match="malformed PCM"):
            await provider.synthesize(
                text="A short test narration.",
                voice_id="test-voice",
                language="en-US",
            )
    finally:
        await provider.aclose()
