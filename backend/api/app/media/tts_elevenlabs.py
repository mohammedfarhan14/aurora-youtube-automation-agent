"""Native ElevenLabs REST adapter; converts PCM S16LE responses to WAV."""

import io
import re
import wave
from urllib.parse import quote, urlsplit

import httpx

from app.media.providers import AudioAsset, TTSCapabilityManifest
from app.media.tts import TTSProviderError


class ElevenLabsTTSProvider:
    """ElevenLabs adapter using its native xi-api-key and text-to-speech API."""

    SAMPLE_RATE_HZ = 24_000
    OUTPUT_FORMAT = "pcm_24000"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 90.0,
        max_response_bytes: int = 25_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
        model_license: str | None = None,
        voice_license: str | None = None,
        license_uri: str | None = None,
        attribution: str | None = None,
        cost_per_1000_characters_usd: float | None = None,
        languages: tuple[str, ...] = (),
        voices: tuple[str, ...] = (),
        sample_rates_hz: tuple[int, ...] = (24_000,),
        provider_id: str = "elevenlabs",
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("ElevenLabs base URL must be an HTTPS origin.")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("ElevenLabs API key is required.")
        if not model.strip() or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
            raise ValueError("ElevenLabs model identifier is invalid.")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("ElevenLabs timeout and response-size limit must be positive.")
        if self.SAMPLE_RATE_HZ not in sample_rates_hz:
            raise ValueError(
                "ElevenLabs PCM sample rate must be declared in its capability manifest."
            )

        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers={"xi-api-key": api_key.strip()},
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._model = model
        self._max_response_bytes = max_response_bytes
        self._manifest = TTSCapabilityManifest(
            provider_id=provider_id,
            languages=languages or ("provider-defined",),
            voices=voices or ("provider-defined",),
            sample_rates_hz=(self.SAMPLE_RATE_HZ,),
            supports_streaming=False,
            supports_word_timings=False,
            model_license=model_license,
            voice_license=voice_license,
            license_uri=license_uri,
            attribution=attribution,
            cost_per_1000_characters_usd=cost_per_1000_characters_usd,
            hosting_mode="hosted",
        )

    @property
    def capability_manifest(self) -> TTSCapabilityManifest:
        return self._manifest

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("v1/models")
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        audio_format: str = "wav",
    ) -> AudioAsset:
        if not isinstance(text, str) or not text.strip() or len(text) > 10_000:
            raise ValueError("Narration text must contain 1 to 10000 characters.")
        if not isinstance(voice_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id):
            raise ValueError("ElevenLabs voice identifier is invalid.")
        if not isinstance(language, str) or not language.strip() or len(language) > 35:
            raise ValueError("TTS language tag is invalid.")
        if not 0.7 <= speed <= 1.2:
            raise ValueError("ElevenLabs speed must be between 0.7 and 1.2.")
        if audio_format.lower() != "wav":
            raise ValueError("Only WAV output is supported by the media pipeline.")
        manifest = self.capability_manifest
        if manifest.languages != ("provider-defined",) and language not in manifest.languages:
            raise TTSProviderError(
                f"ElevenLabs language '{language}' is not in the configured capability manifest."
            )
        if manifest.voices != ("provider-defined",) and voice_id not in manifest.voices:
            raise TTSProviderError(
                f"ElevenLabs voice '{voice_id}' is not in the configured capability manifest."
            )

        voice_settings = {}
        if not self._model.startswith(("eleven_v3", "eleven_v4")):
            voice_settings["speed"] = speed
        elif speed != 1.0:
            raise ValueError("The selected ElevenLabs model does not support speed overrides.")

        try:
            async with self._client.stream(
                "POST",
                f"v1/text-to-speech/{quote(voice_id, safe='')}",
                params={"output_format": self.OUTPUT_FORMAT},
                json={
                    "text": text,
                    "model_id": self._model,
                    "voice_settings": voice_settings,
                },
            ) as response:
                response.raise_for_status()
                content_type = (
                    response.headers.get("content-type", "").split(";", 1)[0].lower()
                )
                if content_type not in {"audio/pcm", "application/octet-stream"}:
                    raise TTSProviderError(
                        "ElevenLabs returned an unsupported PCM content type."
                    )
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise TTSProviderError("ElevenLabs PCM response exceeded the size limit.")
                    chunks.append(chunk)
                pcm = b"".join(chunks)
                request_id = (
                    response.headers.get("request-id")
                    or response.headers.get("xi-request-id")
                )
        except httpx.HTTPStatusError as exc:
            raise TTSProviderError(
                f"ElevenLabs returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSProviderError("ElevenLabs TTS request failed.") from exc

        if not pcm or len(pcm) % 2:
            raise TTSProviderError("ElevenLabs returned empty or malformed PCM audio.")

        duration_ms = round(len(pcm) * 1000 / (self.SAMPLE_RATE_HZ * 2))
        if not 250 <= duration_ms <= 90_000:
            raise TTSProviderError("ElevenLabs audio duration is outside the supported range.")

        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(self.SAMPLE_RATE_HZ)
            wav.writeframes(pcm)

        return AudioAsset(
            content=output.getvalue(),
            mime_type="audio/wav",
            duration_ms=duration_ms,
            sample_rate_hz=self.SAMPLE_RATE_HZ,
            channels=1,
            provider_id=self.capability_manifest.provider_id,
            model_id=self._model,
            voice_id=voice_id,
            request_id=request_id,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
