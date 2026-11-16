import io
import struct
import wave

import httpx
import pytest

from app.media.html_slides import _scene_html
from app.media.providers import VisualAsset
from app.media.tts import MockToneTTSProvider, OpenAICompatibleTTSProvider, TTSProviderError
from app.media.visuals import LocalAbstractVisualProvider, WikimediaFallbackVisualProvider
from app.media.wikimedia import WikimediaVisualProviderError


def make_wav(duration_seconds=1):
    sample_rate = 16_000
    content = io.BytesIO()
    with wave.open(content, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x00\x00" * sample_rate * duration_seconds)
    return content.getvalue()


@pytest.mark.anyio
async def test_openai_compatible_tts_provider_parses_wav_and_request_provenance():
    wav = make_wav()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav", "x-request-id": "tts-request-123"},
            content=wav,
        )

    provider = OpenAICompatibleTTSProvider(
        base_url="https://tts.example.test/v1",
        model="kokoro-local",
        api_key="test-secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await provider.synthesize(
            text="A short narration.",
            voice_id="af_heart",
            language="en-US",
        )
    finally:
        await provider.aclose()

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/audio/speech"
    assert requests[0].headers["authorization"] == "Bearer test-secret"
    assert result.content == wav
    assert result.mime_type == "audio/wav"
    assert result.duration_ms == 1000
    assert result.sample_rate_hz == 16_000
    assert result.channels == 1
    assert result.provider_id == "openai_compatible"
    assert result.request_id == "tts-request-123"


@pytest.mark.anyio
async def test_openai_compatible_tts_provider_normalizes_streaming_wav_size_sentinels():
    streaming_wav = bytearray(make_wav())
    struct.pack_into("<I", streaming_wav, 4, 0xFFFFFFFF)
    struct.pack_into("<I", streaming_wav, 40, 0xFFFFFFFF)
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=bytes(streaming_wav),
        )

    provider = OpenAICompatibleTTSProvider(
        base_url="http://127.0.0.1:8880/v1",
        model="kokoro",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await provider.synthesize(
            text="A short narration.",
            voice_id="af_heart",
            language="en-US",
        )
    finally:
        await provider.aclose()

    assert len(requests) == 1
    assert result.duration_ms == 1000
    assert result.sample_rate_hz == 16_000
    assert result.channels == 1
    with wave.open(io.BytesIO(result.content), "rb") as audio:
        assert audio.getnframes() == 16_000


@pytest.mark.anyio
async def test_openai_compatible_tts_provider_rejects_oversized_response():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "audio/wav"},
            content=make_wav(),
        )

    provider = OpenAICompatibleTTSProvider(
        base_url="http://127.0.0.1:8880/v1",
        model="mock",
        max_response_bytes=100,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TTSProviderError, match="size limit"):
            await provider.synthesize(
                text="A short narration.",
                voice_id="mock",
                language="en",
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_openai_compatible_tts_provider_rejects_invalid_audio_mime_type():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"audio": "not-audio"},
        )

    provider = OpenAICompatibleTTSProvider(
        base_url="http://127.0.0.1:8880/v1",
        model="mock",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(TTSProviderError, match="MIME type"):
            await provider.synthesize(
                text="A short narration.",
                voice_id="mock",
                language="en",
            )
    finally:
        await provider.aclose()


@pytest.mark.anyio
async def test_mock_tts_provider_returns_explicitly_synthetic_audio():
    provider = MockToneTTSProvider()
    result = await provider.synthesize(
        text="This is a deterministic test narration.",
        voice_id="mock-tone",
        language="en",
    )

    assert result.provider_id == "mock_tone"
    assert result.model_id == "deterministic-sine-v1"
    assert result.duration_ms >= 3000
    with wave.open(io.BytesIO(result.content), "rb") as audio:
        assert audio.getframerate() == result.sample_rate_hz
        assert audio.getnchannels() == 1


@pytest.mark.anyio
async def test_local_abstract_visual_provider_is_deterministic_and_portrait():
    provider = LocalAbstractVisualProvider(width=180, height=320)
    args = {
        "visual_direction": "A glowing solar panel at sunrise",
        "narration_segment": "Solar cells convert light into electricity.",
        "scene_index": 0,
    }

    first = await provider.generate(**args)
    second = await provider.generate(**args)
    other_scene = await provider.generate(**{**args, "scene_index": 1})

    assert first.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert struct.unpack(">II", first.content[16:24]) == (180, 320)
    assert first.content == second.content
    assert first.content != other_scene.content
    assert first.rights_basis == "generated_in_application"
    assert first.attribution is None


def test_html_scene_plate_leaves_narration_to_the_caption_track():
    narration = "The Moon illusion makes the Moon appear larger near the horizon."
    visual = VisualAsset(
        content=b"test-image",
        mime_type="image/png",
        width=1,
        height=1,
        provider_id="test",
        rights_basis="test",
    )

    page = _scene_html(
        visual=visual,
        scene_index=0,
        width=720,
        height=1280,
        lighting="cinematic",
    )

    assert "SCENE 01" not in page
    assert "THE STORY" not in page
    assert narration not in page
    assert "makes the Moon appear" not in page


@pytest.mark.anyio
async def test_wikimedia_visual_provider_uses_original_local_art_when_no_asset_is_eligible():
    class NoEligibleCommonsAsset:
        async def generate(self, **kwargs):
            raise WikimediaVisualProviderError("No license-eligible image.")

    provider = WikimediaFallbackVisualProvider(
        NoEligibleCommonsAsset(),
        LocalAbstractVisualProvider(width=180, height=320),
    )
    visual = await provider.generate(
        visual_direction="A moon above a horizon",
        narration_segment="The Moon looks larger near the horizon.",
        scene_index=0,
        visual_style="cinematic",
        lighting="dramatic",
    )

    assert visual.provider_id == "local_abstract_visual_v1"
    assert visual.rights_basis == "generated_in_application"
    assert visual.contains_synthetic_media is False

@pytest.mark.anyio
async def test_openai_compatible_tts_provider_health_check_and_manifest():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": []})

    provider = OpenAICompatibleTTSProvider(
        base_url="https://tts.example.test/v1",
        model="local-model",
        provider_id="kokoro",
        model_license="Apache-2.0",
        voice_license="Apache-2.0",
        license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        attribution="Kokoro model attribution",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await provider.health_check()
        manifest = provider.capability_manifest
        assert manifest.provider_id == "kokoro"
        assert manifest.model_license == "Apache-2.0"
        assert manifest.supports_word_timings is False
    finally:
        await provider.aclose()
