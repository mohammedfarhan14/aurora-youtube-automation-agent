"""OpenAI-compatible and deterministic mock text-to-speech adapters."""

import io
import math
import struct
import wave
from urllib.parse import urlsplit

import httpx

from app.media.providers import AudioAsset, TTSCapabilityManifest


class TTSProviderError(RuntimeError):
    """A configured narration provider failed or returned invalid audio."""


def _normalize_streaming_wav(content: bytes) -> bytes:
    """Normalize RIFF/data length sentinels used by streaming WAV responses.

    Some OpenAI-compatible TTS servers stream WAV audio with 0xFFFFFFFF in
    the RIFF and data chunk size fields. Python's stdlib wave reader interprets
    the data sentinel as an enormous frame count. The response is already
    fully buffered under the configured byte limit, so replace those sentinel
    lengths with the actual bounded payload length before parsing.
    """
    if len(content) < 12 or content[:4] != b"RIFF" or content[8:12] != b"WAVE":
        return content

    normalized = bytearray(content)
    if struct.unpack_from("<I", normalized, 4)[0] == 0xFFFFFFFF:
        struct.pack_into("<I", normalized, 4, len(normalized) - 8)

    offset = 12
    while offset + 8 <= len(normalized):
        chunk_id = bytes(normalized[offset : offset + 4])
        chunk_size = struct.unpack_from("<I", normalized, offset + 4)[0]
        chunk_data_start = offset + 8

        if chunk_size == 0xFFFFFFFF:
            if chunk_id != b"data":
                raise TTSProviderError("TTS provider returned an unsupported WAV chunk sentinel.")
            chunk_size = len(normalized) - chunk_data_start
            if chunk_size <= 0:
                raise TTSProviderError("TTS provider returned an empty streaming WAV data chunk.")
            struct.pack_into("<I", normalized, offset + 4, chunk_size)

        if chunk_size > len(normalized) - chunk_data_start:
            raise TTSProviderError("TTS provider returned a truncated WAV chunk.")

        chunk_end = chunk_data_start + chunk_size
        padded_end = chunk_end + (chunk_size & 1)
        if padded_end > len(normalized):
            if chunk_end == len(normalized):
                break
            raise TTSProviderError("TTS provider returned a truncated WAV chunk.")
        offset = padded_end

    return bytes(normalized)


class OpenAICompatibleTTSProvider:
    """TTS adapter for an explicitly configured OpenAI-compatible endpoint.

    The endpoint is trusted deployment configuration, never model-generated input.
    No paid-provider fallback is performed.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout_seconds: float = 90.0,
        max_response_bytes: int = 25_000_000,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy_url: str | None = None,
        provider_id: str = "openai_compatible",
        model_license: str | None = None,
        voice_license: str | None = None,
        license_uri: str | None = None,
        attribution: str | None = None,
        cost_per_1000_characters_usd: float | None = None,
        hosting_mode: str = "self_hosted",
        languages: tuple[str, ...] = (),
        voices: tuple[str, ...] = (),
        sample_rates_hz: tuple[int, ...] = (8_000, 16_000, 22_050, 24_000, 44_100, 48_000),
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("TTS base URL must be a valid HTTP(S) origin/path.")
        if not model.strip() or len(model) > 200:
            raise ValueError("TTS model identifier is invalid.")
        if timeout_seconds <= 0 or max_response_bytes <= 0:
            raise ValueError("TTS timeout and response-size limit must be positive.")

        headers = {}
        if api_key and api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"
        self._client = httpx.AsyncClient(
            base_url=f"{base_url.rstrip('/')}/",
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            proxy=proxy_url,
        )
        self._model = model
        self._provider_id = provider_id
        self._max_response_bytes = max_response_bytes
        self._manifest = TTSCapabilityManifest(
            provider_id=provider_id,
            languages=languages or ("provider-defined",),
            voices=voices or ("provider-defined",),
            sample_rates_hz=sample_rates_hz or (24_000,),
            supports_streaming=False,
            supports_word_timings=False,
            model_license=model_license,
            voice_license=voice_license,
            license_uri=license_uri,
            attribution=attribution,
            cost_per_1000_characters_usd=cost_per_1000_characters_usd,
            hosting_mode=hosting_mode,
        )

    @property
    def capability_manifest(self) -> TTSCapabilityManifest:
        return self._manifest

    async def health_check(self) -> bool:
        try:
            response = await self._client.get("models")
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
        if not text.strip() or len(text) > 10_000:
            raise ValueError("Narration text must contain 1 to 10000 characters.")
        if not voice_id.strip() or len(voice_id) > 100:
            raise ValueError("TTS voice identifier is invalid.")
        if not language.strip() or len(language) > 35:
            raise ValueError("TTS language tag is invalid.")
        if not 0.5 <= speed <= 2.0:
            raise ValueError("TTS speed must be between 0.5 and 2.0.")
        if audio_format.lower() != "wav":
            raise ValueError("Only WAV output is currently supported by the media pipeline.")
        manifest = self.capability_manifest
        if manifest.languages != ("provider-defined",) and language not in manifest.languages:
            raise TTSProviderError(
                f"TTS language '{language}' is not listed in the configured capability manifest."
            )
        if manifest.voices != ("provider-defined",) and voice_id not in manifest.voices:
            raise TTSProviderError(
                f"TTS voice '{voice_id}' is not listed in the configured capability manifest."
            )

        try:
            async with self._client.stream(
                "POST",
                "audio/speech",
                json={
                    "model": self._model,
                    "input": text,
                    "voice": voice_id,
                    "speed": speed,
                    "response_format": audio_format.lower(),
                },
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0]
                if content_type not in {"audio/wav", "audio/x-wav", "application/octet-stream"}:
                    raise TTSProviderError("TTS provider returned an unsupported audio MIME type.")
                chunks = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise TTSProviderError("TTS provider response exceeded the size limit.")
                    chunks.append(chunk)
                content = b"".join(chunks)
                request_id = response.headers.get("x-request-id")
        except httpx.HTTPStatusError as exc:
            raise TTSProviderError(
                f"TTS provider returned HTTP {exc.response.status_code}."
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSProviderError("TTS provider request failed.") from exc

        content = _normalize_streaming_wav(content)
        try:
            with wave.open(io.BytesIO(content), "rb") as audio:
                channels = audio.getnchannels()
                sample_rate = audio.getframerate()
                sample_width = audio.getsampwidth()
                frame_count = audio.getnframes()
                if channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
                    raise TTSProviderError("TTS provider returned unsupported WAV parameters.")
                if (
                    not 8_000 <= sample_rate <= 96_000
                    or sample_rate not in manifest.sample_rates_hz
                    or frame_count <= 0
                ):
                    raise TTSProviderError(
                        "TTS provider returned a sample rate outside its capability manifest."
                    )
                duration_ms = round(frame_count * 1000 / sample_rate)
        except (wave.Error, EOFError) as exc:
            raise TTSProviderError("TTS provider returned a malformed WAV file.") from exc

        if not 250 <= duration_ms <= 90_000:
            raise TTSProviderError("TTS audio duration is outside the supported range.")

        return AudioAsset(
            content=content,
            mime_type="audio/wav",
            duration_ms=duration_ms,
            sample_rate_hz=sample_rate,
            channels=channels,
            provider_id=self._provider_id,
            model_id=self._model,
            voice_id=voice_id,
            request_id=request_id,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class MockToneTTSProvider:
    """Deterministic synthetic tone provider for tests; it does not produce speech."""

    @property
    def capability_manifest(self) -> TTSCapabilityManifest:
        return TTSCapabilityManifest(
            provider_id="mock",
            languages=("und",),
            voices=("mock-tone",),
            sample_rates_hz=(16_000,),
            supports_streaming=False,
            supports_word_timings=False,
            model_license="generated_in_application",
            voice_license="generated_in_application",
            license_uri=None,
            attribution="Deterministic synthetic test tone; not speech",
            cost_per_1000_characters_usd=0.0,
            hosting_mode="in_process",
        )

    async def health_check(self) -> bool:
        return True

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str = "mock-tone",
        language: str = "und",
        speed: float = 1.0,
        audio_format: str = "wav",
    ) -> AudioAsset:
        if audio_format.lower() != "wav":
            raise ValueError("Mock TTS only supports WAV output.")
        if not text.strip():
            raise ValueError("Narration text cannot be empty.")
        duration_seconds = max(3, min(45, round(len(text.split()) / 2.2)))
        sample_rate = 16_000
        frames = int(duration_seconds * sample_rate)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(sample_rate)
            samples = bytearray()
            for index in range(frames):
                envelope = min(1.0, index / 1600, (frames - index) / 1600)
                value = int(700 * envelope * math.sin(2 * math.pi * 440 * index / sample_rate))
                samples.extend(struct.pack("<h", value))
            audio.writeframes(samples)

        return AudioAsset(
            content=buffer.getvalue(),
            mime_type="audio/wav",
            duration_ms=duration_seconds * 1000,
            sample_rate_hz=sample_rate,
            channels=1,
            provider_id="mock_tone",
            model_id="deterministic-sine-v1",
            voice_id=voice_id,
            request_id=None,
        )

    async def aclose(self) -> None:
        return None
