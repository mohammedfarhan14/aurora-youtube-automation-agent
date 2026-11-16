from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.story.contracts import StoryArtifact


class ProductionRunCreateRequest(BaseModel):
    research_run_id: UUID
    youtube_channel_id: UUID
    tts_provider_override: str | None = Field(default=None, min_length=1, max_length=80)
    tts_voice_override: str | None = Field(default=None, min_length=1, max_length=100)
    tts_speed_override: float | None = Field(default=None, ge=0.5, le=2.0)
    tts_audio_format_override: Literal["wav"] | None = None


class ProductionReadinessCheck(BaseModel):
    key: str
    status: Literal["ready", "attention", "blocked"]
    title: str
    detail: str
    action: str | None = None


class ProductionReadinessResponse(BaseModel):
    checks: list[ProductionReadinessCheck]
    summary: Literal["ready", "attention", "blocked"]
    # Safe, non-secret runtime capabilities used to avoid offering voices that
    # the configured local TTS service cannot synthesize.
    tts_provider: str | None = None
    tts_voice_options: list[str] = Field(default_factory=list)


class ProductionRunStageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    stage_key: str
    status: str
    attempt_count: int
    next_attempt_at: datetime
    input_manifest: dict
    output_manifest: dict
    provider_id: str | None
    model_id: str | None
    prompt_version: str | None
    request_id: str | None
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None


class ProductionRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    research_run_id: UUID
    requested_by_user_id: UUID
    youtube_channel_id: UUID | None
    channel_profile_id: UUID | None
    channel_profile_version: int | None
    channel_profile_snapshot: dict | None
    scheduled_local_date: date | None
    scheduled_for: datetime | None
    idempotency_key: str
    status: str
    attempt_count: int
    cancel_requested_at: datetime | None
    approved_by_user_id: UUID | None
    approved_at: datetime | None
    approval_warning_override: dict | None
    rejected_by_user_id: UUID | None
    rejected_at: datetime | None
    rejection_reason: str | None
    approval_mode: Literal["none", "human", "automatic_consent"]
    approval_consent_event_id: UUID | None
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    stages: list[ProductionRunStageResponse] = Field(default_factory=list)


class ProductionRunCancelResponse(BaseModel):
    id: UUID
    status: str
    cancel_requested_at: datetime | None


class ProductionRunRejectRequest(BaseModel):
    reason: str = Field(default="Rejected by editor.", min_length=1, max_length=1000)


class ProductionCleanupResponse(BaseModel):
    deleted_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    skipped_statuses: dict[str, int] = Field(default_factory=dict)


class ProductionRunApprovalRequest(BaseModel):
    allow_editorial_warnings: bool = False
    warning_override_reason: str | None = Field(default=None, min_length=10, max_length=1000)


class StoryEvidenceExcerptResponse(BaseModel):
    id: UUID
    excerpt: str
    locator: str | None


class StorySourceEvidenceResponse(BaseModel):
    id: UUID
    canonical_url: str
    title: str
    publisher: str | None
    source_type: str
    evidence: list[StoryEvidenceExcerptResponse] = Field(default_factory=list)


class StoryArtifactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    production_run_id: UUID
    artifact_type: Literal["story_draft", "story_verified"]
    schema_version: str
    version: int
    content_hash: str
    created_at: datetime
    payload: StoryArtifact
    evidence_context: list[StorySourceEvidenceResponse] = Field(default_factory=list)
