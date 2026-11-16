from datetime import date, datetime, time
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class YouTubeChannelProfileRequest(BaseModel):
    niche: str = Field(min_length=1, max_length=120)
    topic_boundaries: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=30
    )
    target_audience: str = Field(min_length=1, max_length=300)
    language: str = Field(min_length=1, max_length=80)
    tone: str = Field(min_length=1, max_length=80)
    format: Literal[
        "educational",
        "documentary",
        "news",
        "storytelling",
        "commentary",
        "entertainment",
    ] = "educational"
    prohibited_topics: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        default_factory=list, max_length=30
    )
    additional_constraints: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list, max_length=30
    )
    tts_provider: Literal[
        "openai_compatible",
        "kokoro",
        "qwen3_tts",
        "openai",
        "custom",
        "elevenlabs",
    ] | None = None
    tts_voice: str | None = Field(default=None, min_length=1, max_length=100)
    tts_speed: float | None = Field(default=None, ge=0.5, le=2.0)
    tts_audio_format: Literal["wav"] | None = None
    visual_style: str = Field(default="", max_length=500)
    visual_lighting: Literal["natural", "cinematic", "studio", "dramatic"] = "cinematic"
    visual_motion: Literal["static", "gentle_drift"] = "gentle_drift"
    video_aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    caption_style: Literal["classic", "bold_outline"] = "bold_outline"
    caption_position: Literal["lower_safe", "center"] = "lower_safe"
    caption_size: Literal["small", "medium", "large"] = "medium"

    @field_validator(
        "niche", "target_audience", "language", "tone", mode="before"
    )
    @classmethod
    def trim_required_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("tts_provider", "tts_voice", mode="before")
    @classmethod
    def trim_optional_tts_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("visual_style", mode="before")
    @classmethod
    def trim_visual_style(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "topic_boundaries", "prohibited_topics", "additional_constraints", mode="before"
    )
    @classmethod
    def trim_list_values(cls, value: object) -> object:
        if isinstance(value, list):
            return [
                item.strip() if isinstance(item, str) else item
                for item in value
                if not isinstance(item, str) or item.strip()
            ]
        return value


class YouTubeChannelProfileResponse(BaseModel):
    id: UUID
    organization_id: UUID
    youtube_channel_id: UUID
    version: int
    profile: dict
    created_by_user_id: UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class YouTubeChannelScheduleRequest(BaseModel):
    enabled: bool
    timezone: str = Field(min_length=1, max_length=64)
    posting_time: time
    catch_up_policy: Literal["skip", "run_once"] = "skip"


class YouTubeChannelScheduleResponse(BaseModel):
    id: UUID
    organization_id: UUID
    youtube_channel_id: UUID
    enabled: bool
    timezone: str
    posting_time: time
    catch_up_policy: str
    next_run_at: datetime | None
    last_scheduled_local_date: date | None
    updated_by_user_id: UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
