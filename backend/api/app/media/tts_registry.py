"""Provider-neutral TTS registry and deterministic configuration resolution."""

from collections.abc import Callable
from dataclasses import dataclass

import httpx

from app.core.config import Settings
from app.media.providers import TTSProvider
from app.media.tts import MockToneTTSProvider, OpenAICompatibleTTSProvider
from app.media.tts_elevenlabs import ElevenLabsTTSProvider


class TTSProviderConfigurationError(ValueError):
    """The selected narration provider is unsupported or misconfigured."""


@dataclass(frozen=True, slots=True)
class TTSSelection:
    provider_id: str
    voice_id: str
    speed: float = 1.0
    audio_format: str = "wav"


def _candidate(source: object, key: str) -> object | None:
    if not isinstance(source, dict):
        return None
    value = source.get(key)
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def resolve_tts_selection(
    *,
    environment_default: TTSSelection,
    channel_profile: dict | None = None,
    run_override: dict | None = None,
) -> TTSSelection:
    """Resolve each setting independently: run override > profile > environment."""
    profile = channel_profile if isinstance(channel_profile, dict) else {}
    override = run_override if isinstance(run_override, dict) else {}

    def resolve(key: str, default: object) -> object:
        run_value = _candidate(override, key)
        if run_value is not None:
            return run_value
        profile_value = _candidate(profile, key)
        return profile_value if profile_value is not None else default

    provider_id = str(resolve("tts_provider", environment_default.provider_id)).strip().lower()
    voice_id = str(resolve("tts_voice", environment_default.voice_id)).strip()
    speed = float(resolve("tts_speed", environment_default.speed))
    audio_format = str(
        resolve("tts_audio_format", environment_default.audio_format)
    ).strip().lower()

    if not provider_id or not voice_id:
        raise TTSProviderConfigurationError("TTS provider and voice must be configured.")
    if not 0.5 <= speed <= 2.0:
        raise TTSProviderConfigurationError("TTS speed must be between 0.5 and 2.0.")
    if provider_id == "elevenlabs" and not 0.7 <= speed <= 1.2:
        raise TTSProviderConfigurationError(
            "ElevenLabs TTS speed must be between 0.7 and 1.2."
        )
    if audio_format != "wav":
        raise TTSProviderConfigurationError("Only WAV output is currently supported.")
    return TTSSelection(
        provider_id=provider_id,
        voice_id=voice_id,
        speed=speed,
        audio_format=audio_format,
    )


class TTSProviderRegistry:
    """Construct adapters from explicit settings; never silently substitutes providers."""

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport
        self._factories: dict[str, Callable[[str], TTSProvider]] = {
            "mock": lambda _provider_id: self._mock(),
            "openai_compatible": self._openai_compatible,
            "elevenlabs": self._elevenlabs,
            # These aliases require the OpenAI-compatible /audio/speech contract.
            # Native vendor APIs must be implemented as separate adapters.
            "kokoro": self._openai_compatible,
            "qwen3_tts": self._openai_compatible,
            "openai": self._openai_compatible,
            "custom": self._openai_compatible,
        }

    @property
    def environment_default(self) -> TTSSelection:
        return TTSSelection(
            provider_id=self.settings.media_tts_provider,
            voice_id=self.settings.media_tts_voice or "default",
            speed=self.settings.media_tts_speed,
            audio_format=self.settings.media_tts_format,
        )

    def register(self, provider_id: str, factory: Callable[[], TTSProvider]) -> None:
        key = provider_id.strip().lower()
        if not key or key in self._factories:
            raise TTSProviderConfigurationError(
                "TTS provider identifier is empty or already registered."
            )
        self._factories[key] = lambda _provider_id: factory()

    def create(self, provider_id: str) -> TTSProvider:
        key = provider_id.strip().lower()
        factory = self._factories.get(key)
        if factory is None:
            raise TTSProviderConfigurationError(
                f"Unsupported TTS provider '{provider_id}'. "
                "Register an adapter before selecting it."
            )
        return factory(key)

    def _mock(self) -> TTSProvider:
        if self.settings.app_env.strip().lower() == "production":
            raise TTSProviderConfigurationError(
                "The mock TTS provider is not allowed in production."
            )
        return MockToneTTSProvider()

    def _openai_compatible(self, provider_id: str) -> TTSProvider:
        required = {
            "MEDIA_TTS_BASE_URL": self.settings.media_tts_base_url,
            "MEDIA_TTS_MODEL": self.settings.media_tts_model,
        }
        missing = [
            key for key, value in required.items() if not value or not value.strip()
        ]
        if missing:
            raise TTSProviderConfigurationError(
                "Selected TTS provider requires: " + ", ".join(missing)
            )
        return OpenAICompatibleTTSProvider(
            base_url=self.settings.media_tts_base_url or "",
            model=self.settings.media_tts_model or "",
            api_key=(
                self.settings.media_tts_api_key.get_secret_value()
                if self.settings.media_tts_api_key is not None
                else None
            ),
            timeout_seconds=self.settings.media_tts_timeout_seconds,
            max_response_bytes=self.settings.media_tts_max_response_bytes,
            transport=self.transport,
            proxy_url=self.settings.media_tts_proxy_url,
            provider_id=provider_id,
            model_license=(
                self.settings.media_tts_model_license_id
                or self.settings.media_tts_license_id
            ),
            voice_license=(
                self.settings.media_tts_voice_license_id
                or self.settings.media_tts_license_id
            ),
            license_uri=self.settings.media_tts_license_uri,
            attribution=self.settings.media_tts_attribution,
            cost_per_1000_characters_usd=(
                self.settings.media_tts_cost_per_1000_characters_usd
            ),
            hosting_mode=self.settings.media_tts_hosting_mode,
            languages=tuple(self.settings.media_tts_languages),
            voices=tuple(self.settings.media_tts_voices),
            sample_rates_hz=tuple(self.settings.media_tts_sample_rates_hz),
        )

    def _elevenlabs(self, provider_id: str) -> TTSProvider:
        required = {
            "MEDIA_TTS_BASE_URL": self.settings.media_tts_base_url,
            "MEDIA_TTS_MODEL": self.settings.media_tts_model,
            "MEDIA_TTS_VOICE": self.settings.media_tts_voice,
        }
        missing = [
            key for key, value in required.items() if not value or not value.strip()
        ]
        if missing:
            raise TTSProviderConfigurationError(
                "Selected ElevenLabs TTS provider requires: " + ", ".join(missing)
            )
        if (
            self.settings.media_tts_api_key is None
            or not self.settings.media_tts_api_key.get_secret_value().strip()
        ):
            raise TTSProviderConfigurationError(
                "Selected ElevenLabs TTS provider requires MEDIA_TTS_API_KEY."
            )
        if self.settings.media_tts_hosting_mode != "hosted":
            raise TTSProviderConfigurationError(
                "ElevenLabs TTS must be configured with hosted infrastructure metadata."
            )
        return ElevenLabsTTSProvider(
            base_url=self.settings.media_tts_base_url or "",
            api_key=self.settings.media_tts_api_key.get_secret_value(),
            model=self.settings.media_tts_model or "",
            timeout_seconds=self.settings.media_tts_timeout_seconds,
            max_response_bytes=self.settings.media_tts_max_response_bytes,
            transport=self.transport,
            model_license=(
                self.settings.media_tts_model_license_id
                or self.settings.media_tts_license_id
            ),
            voice_license=(
                self.settings.media_tts_voice_license_id
                or self.settings.media_tts_license_id
            ),
            license_uri=self.settings.media_tts_license_uri,
            attribution=self.settings.media_tts_attribution,
            cost_per_1000_characters_usd=(
                self.settings.media_tts_cost_per_1000_characters_usd
            ),
            languages=tuple(self.settings.media_tts_languages),
            voices=tuple(self.settings.media_tts_voices),
            sample_rates_hz=tuple(
                sorted(set(self.settings.media_tts_sample_rates_hz) | {24_000})
            ),
            provider_id=provider_id,
        )

    async def validate_default(self) -> None:
        """Fail worker startup if the environment-default provider is unavailable."""
        provider = self.create(self.environment_default.provider_id)
        try:
            if not await provider.health_check():
                raise TTSProviderConfigurationError(
                    f"Configured TTS provider '{self.environment_default.provider_id}' "
                    "failed its startup health check."
                )
        finally:
            await provider.aclose()

    async def aclose(self) -> None:
        """Registry owns no long-lived provider resources."""
        return None
