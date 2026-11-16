import pytest

from app.core.config import Settings
from app.media.tts_registry import (
    TTSProviderConfigurationError,
    TTSProviderRegistry,
    TTSSelection,
    resolve_tts_selection,
)


def test_tts_selection_precedence_is_run_then_channel_then_environment():
    default = TTSSelection("openai_compatible", "env-voice", 1.0, "wav")
    profile = {"tts_provider": "kokoro", "tts_voice": "channel-voice", "tts_speed": 1.1}
    override = {"tts_voice": "run-voice", "tts_speed": 0.9}

    selected = resolve_tts_selection(
        environment_default=default,
        channel_profile=profile,
        run_override=override,
    )

    assert selected == TTSSelection("kokoro", "run-voice", 0.9, "wav")


def test_tts_selection_uses_environment_when_profile_and_run_are_empty():
    default = TTSSelection("mock", "tone", 1.0, "wav")
    assert resolve_tts_selection(environment_default=default) == default


@pytest.mark.parametrize("speed", [0.49, 2.01])
def test_tts_selection_rejects_out_of_range_speed(speed):
    with pytest.raises(TTSProviderConfigurationError, match="speed"):
        resolve_tts_selection(
            environment_default=TTSSelection("mock", "tone", 1.0),
            run_override={"tts_speed": speed},
        )


def test_tts_selection_rejects_unsupported_format():
    with pytest.raises(TTSProviderConfigurationError, match="WAV"):
        resolve_tts_selection(
            environment_default=TTSSelection("mock", "tone"),
            run_override={"tts_audio_format": "mp3"},
        )


def test_registry_creates_mock_provider_without_external_credentials():
    registry = TTSProviderRegistry(Settings(_env_file=None, media_tts_provider="mock"))
    provider = registry.create("mock")
    assert provider is not None


def test_registry_does_not_silently_fallback_for_unknown_provider():
    registry = TTSProviderRegistry(Settings(_env_file=None))
    with pytest.raises(TTSProviderConfigurationError, match="Unsupported TTS provider"):
        registry.create("unsupported_vendor")


def test_registry_does_not_allow_mock_provider_in_production():
    settings = Settings(_env_file=None, media_tts_provider="mock")
    settings.app_env = "production"
    registry = TTSProviderRegistry(settings)
    with pytest.raises(TTSProviderConfigurationError, match="not allowed in production"):
        registry.create("mock")

@pytest.mark.anyio
async def test_mock_provider_exposes_capabilities_and_health():
    registry = TTSProviderRegistry(Settings(_env_file=None, media_tts_provider="mock"))
    provider = registry.create("mock")
    assert provider.capability_manifest.provider_id == "mock"
    assert provider.capability_manifest.hosting_mode == "in_process"
    assert await provider.health_check()
    await provider.aclose()


@pytest.mark.anyio
async def test_openai_compatible_provider_exposes_manifest_and_health():
    settings = Settings(
        _env_file=None,
        media_tts_provider="openai_compatible",
        media_tts_base_url="https://tts.example.test/v1",
        media_tts_model="test-model",
        media_tts_license_id="Legacy-License",
        media_tts_model_license_id="Apache-2.0",
        media_tts_voice_license_id="CC-BY-4.0",
        media_tts_license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        media_tts_attribution="Test model attribution",
        media_tts_languages=["en-US", "fr-FR"],
        media_tts_voices=["af_heart", "af_bella"],
        media_tts_sample_rates_hz=[22_050, 24_000],
        media_tts_hosting_mode="self_hosted",
    )
    registry = TTSProviderRegistry(settings)
    provider = registry.create("openai_compatible")
    # The endpoint is not contacted by construction; adapters expose an explicit
    # health check so deployments can verify readiness before enabling workers.
    assert provider.capability_manifest.provider_id == "openai_compatible"
    assert provider.capability_manifest.model_license == "Apache-2.0"
    assert provider.capability_manifest.voice_license == "CC-BY-4.0"
    assert provider.capability_manifest.languages == ("en-US", "fr-FR")
    assert provider.capability_manifest.voices == ("af_heart", "af_bella")
    assert provider.capability_manifest.sample_rates_hz == (22_050, 24_000)
    assert provider.capability_manifest.supports_streaming is False
    assert provider.capability_manifest.supports_word_timings is False
    assert provider.capability_manifest.hosting_mode == "self_hosted"
    await provider.aclose()


def test_registry_can_register_a_custom_adapter_without_business_logic_changes():
    settings = Settings(_env_file=None, media_tts_provider="mock")
    registry = TTSProviderRegistry(settings)
    registry.register("custom_test", lambda: registry.create("mock"))
    assert registry.create("custom_test").capability_manifest.provider_id == "mock"

def test_channel_profile_accepts_optional_tts_preferences():
    from app.schemas.youtube_channel_settings import YouTubeChannelProfileRequest

    profile = YouTubeChannelProfileRequest(
        niche="science",
        target_audience="curious adults",
        language="en-US",
        tone="clear",
        tts_provider=" kokoro ",
        tts_voice=" af_heart ",
        tts_speed=1.1,
        tts_audio_format="wav",
    )
    assert profile.tts_provider == "kokoro"
    assert profile.tts_voice == "af_heart"
    assert profile.tts_speed == 1.1


def test_production_run_accepts_explicit_tts_overrides():
    from uuid import uuid4

    from app.schemas.production import ProductionRunCreateRequest

    payload = ProductionRunCreateRequest(
        research_run_id=uuid4(),
        youtube_channel_id=uuid4(),
        tts_provider_override="mock",
        tts_voice_override="mock-tone",
        tts_speed_override=0.9,
        tts_audio_format_override="wav",
    )
    assert payload.tts_provider_override == "mock"
    assert payload.tts_speed_override == 0.9

@pytest.mark.anyio
async def test_same_synthesis_contract_runs_with_mock_and_openai_compatible_adapters():
    import io
    import wave

    import httpx

    wav_buffer = io.BytesIO()
    with wave.open(wav_buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(bytes(32_000))
    wav = wav_buffer.getvalue()

    def handler(request):
        if request.method == "GET" and request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": []})
        if request.method == "POST" and request.url.path.endswith("/audio/speech"):
            return httpx.Response(200, headers={"content-type": "audio/wav"}, content=wav)
        return httpx.Response(404)

    settings = Settings(
        _env_file=None,
        media_tts_provider="openai_compatible",
        media_tts_base_url="https://tts.example.test/v1",
        media_tts_model="test-model",
        media_tts_voice="test-voice",
    )
    registry = TTSProviderRegistry(settings, transport=httpx.MockTransport(handler))

    mock_provider = registry.create("mock")
    mock_audio = await mock_provider.synthesize(
        text="The same narration text.", voice_id="mock-tone", language="en"
    )
    await mock_provider.aclose()

    configured_provider = registry.create("openai_compatible")
    assert await configured_provider.health_check()
    configured_audio = await configured_provider.synthesize(
        text="The same narration text.", voice_id="test-voice", language="en"
    )
    await configured_provider.aclose()

    assert mock_audio.mime_type == configured_audio.mime_type == "audio/wav"
    assert mock_audio.provider_id == "mock_tone"
    assert configured_audio.provider_id == "openai_compatible"
    assert configured_audio.duration_ms == 1000

@pytest.mark.anyio
async def test_registry_preflights_environment_default_provider():
    import httpx

    settings = Settings(
        _env_file=None,
        media_tts_provider="openai_compatible",
        media_tts_base_url="https://tts.example.test/v1",
        media_tts_model="test-model",
        media_tts_voice="test-voice",
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"data": []})
    )
    registry = TTSProviderRegistry(settings, transport=transport)
    await registry.validate_default()


def test_elevenlabs_selection_enforces_vendor_speed_range():
    with pytest.raises(TTSProviderConfigurationError, match="ElevenLabs TTS speed"):
        resolve_tts_selection(
            environment_default=TTSSelection("elevenlabs", "voice", 1.0, "wav"),
            run_override={"tts_speed": 1.5},
        )
