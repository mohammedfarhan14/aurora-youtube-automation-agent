from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class YouTubeAuthorizationResponse(BaseModel):
    authorization_url: str
    purpose: Literal["connect", "publish"]
    requested_scopes: list[str]


class YouTubeChannelResponse(BaseModel):
    id: UUID
    channel_id: str
    title: str
    custom_url: str | None
    thumbnail_url: str | None
    granted_scopes: list[str]
    can_publish: bool
    connected_at: datetime

    model_config = ConfigDict(from_attributes=True)


class YouTubeAnalyticsDay(BaseModel):
    day: date
    views: int
    minutes_watched: float
    average_view_duration_seconds: float
    average_view_percentage: float
    subscribers_gained: int
    subscribers_lost: int
    likes: int
    comments: int
    shares: int


class YouTubeAnalyticsReviewSummary(BaseModel):
    id: UUID
    status: Literal["pending", "approved", "dismissed"]
    reviewed_at: datetime | None
    reviewed_by_user_id: UUID | None

    model_config = ConfigDict(from_attributes=True)


class YouTubeAnalyticsReviewRequest(BaseModel):
    decision: Literal["approved", "dismissed"]


class YouTubeAnalyticsComparison(BaseModel):
    current_start_date: date
    current_end_date: date
    previous_start_date: date
    previous_end_date: date
    current_views: int
    previous_views: int
    current_watch_minutes: float
    previous_watch_minutes: float
    current_average_view_percentage: float
    previous_average_view_percentage: float
    views_change_percent: float | None
    watch_minutes_change_percent: float | None
    average_view_percentage_change_points: float
    suggestion: str
    review: YouTubeAnalyticsReviewSummary


class YouTubeAnalyticsResponse(BaseModel):
    youtube_channel_id: UUID
    channel_title: str
    start_date: date
    end_date: date
    latest_report_day: date | None = Field(
        default=None,
        description="Most recent day returned by YouTube; reports can lag the requested range.",
    )
    pending_days: int = Field(
        default=0,
        ge=0,
        description="Days through the requested end date that YouTube has not reported yet.",
    )
    verified: Literal[True] = True
    snapshot_count: int
    snapshot_saved_at: datetime | None
    comparison: YouTubeAnalyticsComparison | None
    rows: list[YouTubeAnalyticsDay]


class YouTubeChannelChoice(BaseModel):
    channel_id: str
    title: str
    custom_url: str | None = None
    thumbnail_url: str | None = None


class YouTubePendingSelectionResponse(BaseModel):
    attempt_id: UUID
    organization_id: UUID
    expires_at: datetime
    channels: list[YouTubeChannelChoice]


class YouTubeChannelSelectionRequest(BaseModel):
    channel_id: str = Field(min_length=1, max_length=128)


class YouTubePublishingSettingsRequest(BaseModel):
    auto_publish_enabled: bool
    privacy_status: Literal["private", "unlisted", "public"] | None = None


class YouTubePublishingSettingsResponse(BaseModel):
    organization_id: UUID
    youtube_channel_id: UUID
    connected_by_user_id: UUID | None
    can_publish: bool
    can_manage_auto_publish: bool
    auto_publish_enabled: bool
    auto_publish_privacy_status: Literal["private", "unlisted", "public"] = "private"
    automatic_execution_available: bool = Field(
        default=False,
        description="Whether the automatic publishing worker is enabled in this deployment.",
    )
    consent_granted_by_user_id: UUID | None
    consent_granted_at: datetime | None


class YouTubePublishJobCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=5000)
    tags: list[str] = Field(default_factory=list, max_length=30)
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    publish_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("YouTube title cannot be empty.")
        return normalized

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("tags")
    @classmethod
    def normalize_tags(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("YouTube tags cannot be empty.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("YouTube tags must be unique.")
        if sum(len(value) for value in normalized) + len(normalized) > 500:
            raise ValueError("YouTube tags exceed the 500-character combined limit.")
        return normalized


class YouTubePublishJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    production_run_id: UUID
    youtube_channel_id: UUID
    media_artifact_id: UUID
    requested_by_user_id: UUID
    approved_by_user_id: UUID | None
    approved_at: datetime
    approval_mode: Literal["human", "automatic_consent"]
    approval_consent_event_id: UUID | None
    status: str
    privacy_status: str
    publish_at: datetime | None
    title: str
    description: str
    tags: list[str]
    byte_size: int
    bytes_uploaded: int
    youtube_video_id: str | None
    youtube_processing_status: str | None
    attempt_count: int
    failure_code: str | None
    failure_message: str | None
    thumbnail_upload_warning: str | None
    published_at: datetime | None
    created_at: datetime
    updated_at: datetime


class YouTubePublishEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    publish_job_id: UUID
    actor_user_id: UUID | None
    event_type: str
    event_metadata: dict
    created_at: datetime


class YouTubePublishingConsentEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    youtube_channel_id: UUID
    actor_user_id: UUID | None
    action: Literal["enabled", "disabled"]
    reason: str
    granted_scopes_snapshot: str
    created_at: datetime
