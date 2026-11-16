"""Provider-neutral media-generation contracts."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TTSWordTiming:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class TTSCapabilityManifest:
    provider_id: str
    languages: tuple[str, ...]
    voices: tuple[str, ...]
    sample_rates_hz: tuple[int, ...]
    supports_streaming: bool
    supports_word_timings: bool
    model_license: str | None
    voice_license: str | None
    license_uri: str | None
    attribution: str | None
    cost_per_1000_characters_usd: float | None
    hosting_mode: str


@dataclass(frozen=True, slots=True)
class AudioAsset:
    content: bytes
    mime_type: str
    duration_ms: int
    sample_rate_hz: int
    channels: int
    provider_id: str
    model_id: str
    voice_id: str
    request_id: str | None = None
    word_timings: tuple[TTSWordTiming, ...] = ()


class WhisperXAlignmentProvider(Protocol):
    """Injection seam for a Whisper/WhisperX forced-alignment implementation.

    Implementations return absolute audio-timeline word timings. The core
    application does not assume a hosted endpoint or bundle an alignment model.
    """

    async def align(
        self,
        *,
        audio: AudioAsset,
        text: str,
        language: str,
    ) -> tuple[TTSWordTiming, ...]:
        ...


@dataclass(frozen=True, slots=True)
class VisualAsset:
    content: bytes
    mime_type: str
    width: int
    height: int
    provider_id: str
    rights_basis: str
    attribution: str | None = None
    source_uri: str | None = None
    license_id: str | None = None
    license_uri: str | None = None
    contains_synthetic_media: bool | None = None


@dataclass(frozen=True, slots=True)
class BackgroundMusicAsset:
    content: bytes
    mime_type: str
    duration_ms: int
    sample_rate_hz: int
    channels: int
    metadata: dict[str, object]


class BackgroundMusicProvider(Protocol):
    async def select_track(
        self,
        *,
        organization_id: UUID,
        production_run_id: UUID,
    ) -> BackgroundMusicAsset | None:
        """Return a rights-cleared track for this tenant/run, or no track."""


@dataclass(frozen=True, slots=True)
class RenderRequest:
    visual_assets: tuple[VisualAsset, ...]
    audio_asset: AudioAsset
    captions_vtt: str
    scene_durations_seconds: tuple[int, ...]
    background_music_asset: BackgroundMusicAsset | None = None
    target_duration_seconds: int = 30
    width: int = 1080
    height: int = 1920
    frame_rate: int = 30
    motion_style: str | None = None
    caption_style: str = "bold_outline"
    caption_position: str = "lower_safe"
    caption_size: str = "medium"


@dataclass(frozen=True, slots=True)
class MediaQualityReport:
    is_valid: bool
    duration_ms: int
    width: int
    height: int
    video_codec: str
    audio_codec: str
    has_audio: bool
    has_captions: bool = False
    issues: tuple[str, ...] = ()
    integrated_loudness_lufs: float | None = None
    true_peak_dbfs: float | None = None
    black_frame_segments: tuple[tuple[float, float], ...] = ()


@dataclass(frozen=True, slots=True)
class VideoAsset:
    content: bytes
    mime_type: str
    duration_ms: int
    width: int
    height: int
    video_codec: str
    audio_codec: str
    provider_id: str
    metadata: dict[str, object] | None = None


class TTSProvider(Protocol):
    @property
    def capability_manifest(self) -> TTSCapabilityManifest:
        """Describe supported voices, languages, timing and licensing."""

    async def synthesize(
        self,
        *,
        text: str,
        voice_id: str,
        language: str,
        speed: float = 1.0,
        audio_format: str = "wav",
    ) -> AudioAsset:
        """Convert narration text into validated audio and optional word timings."""

    async def health_check(self) -> bool:
        """Return whether the configured provider endpoint is reachable and ready."""

    async def aclose(self) -> None:
        """Release provider resources."""


class VideoRenderer(Protocol):
    async def render(self, request: RenderRequest) -> VideoAsset:
        """Render a portrait video from rights-cleared scene assets and narration."""

    async def inspect(self, content: bytes) -> MediaQualityReport:
        """Inspect the rendered video and report deterministic technical checks."""


class VisualProvider(Protocol):
    async def generate(
        self,
        *,
        visual_direction: str,
        narration_segment: str,
        scene_index: int,
        visual_style: str = "",
        lighting: str = "cinematic",
    ) -> VisualAsset:
        """Create one scene visual with explicit rights provenance."""
