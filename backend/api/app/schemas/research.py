from datetime import date, datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)


class ResearchRunCreateRequest(BaseModel):
    topic: str = Field(min_length=1, max_length=300)

    @field_validator("topic")
    @classmethod
    def normalize_topic(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Topic must not be empty.")
        return normalized


class ResearchCleanupResponse(BaseModel):
    deleted_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    skipped_statuses: dict[str, int] = Field(default_factory=dict)


class ResearchRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    requested_by_user_id: UUID
    topic: str
    youtube_channel_id: UUID | None
    channel_profile_id: UUID | None
    channel_profile_version: int | None
    scheduled_local_date: date | None
    scheduled_for: datetime | None
    status: str
    failure_code: str | None
    failure_message: str | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime

class ResearchSourceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    research_run_id: UUID
    canonical_url: str
    title: str
    publisher: str | None
    source_type: str
    published_at: datetime | None
    retrieved_at: datetime
    content_hash: str | None
    publisher_domain: str | None
    source_class: str
    independence_group_key: str | None
    independence_basis: str
    classification_version: str
    rights_metadata: RightsMetadata = Field(default_factory=RightsMetadata)
    created_at: datetime


class EvidenceItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    research_source_id: UUID
    excerpt: str
    content_hash: str
    locator: str | None


class ResearchSourceRightsReviewRequest(BaseModel):
    decision: Literal["cleared", "restricted"]
    scope: RightsScope = RightsScope.UNKNOWN
    license_ids: tuple[str, ...] = Field(default=(), max_length=10)
    license_urls: tuple[str, ...] = Field(default=(), max_length=10)
    rights_holder: str | None = Field(default=None, max_length=500)
    creators: tuple[str, ...] = Field(default=(), max_length=50)
    source_revision_url: str | None = Field(default=None, max_length=2048)
    attribution_text: str | None = Field(default=None, max_length=2000)
    reuse_restrictions: tuple[ReuseRestriction, ...] = Field(default=(), max_length=20)
    review_notes: str = Field(min_length=1, max_length=2000)
    evidence_urls: tuple[str, ...] = Field(min_length=1, max_length=10)

    @field_validator("review_notes")
    @classmethod
    def normalize_review_notes(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Review notes must not be empty.")
        return normalized

    @field_validator("evidence_urls")
    @classmethod
    def validate_evidence_urls(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            parts = urlsplit(value)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise ValueError(
                    "Rights review evidence URLs must be absolute HTTPS URLs."
                )
        if len(set(values)) != len(values):
            raise ValueError("Rights review evidence URLs must be unique.")
        return values

    @model_validator(mode="after")
    def validate_decision_metadata(self) -> "ResearchSourceRightsReviewRequest":
        status = (
            RightsStatus.CLEARED
            if self.decision == "cleared"
            else RightsStatus.RESTRICTED
        )
        RightsMetadata(
            status=status,
            scope=self.scope,
            license_ids=self.license_ids,
            license_urls=self.license_urls,
            rights_holder=self.rights_holder,
            creators=self.creators,
            source_revision_url=self.source_revision_url,
            attribution_text=self.attribution_text,
            reuse_restrictions=self.reuse_restrictions,
        )
        if self.decision == "cleared":
            if self.scope == RightsScope.UNKNOWN:
                raise ValueError("Cleared rights require an explicit reuse scope.")
            if not self.attribution_text:
                raise ValueError("Cleared rights require attribution text.")
        if not set(self.license_urls).issubset(set(self.evidence_urls)):
            raise ValueError(
                "Rights review evidence must include every declared license URL."
            )
        if (
            self.source_revision_url
            and self.source_revision_url not in self.evidence_urls
        ):
            raise ValueError(
                "Rights review evidence must include the exact reviewed source revision."
            )
        return self


class ResearchSourceRightsReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    organization_id: UUID
    research_run_id: UUID
    research_source_id: UUID
    rights_profile_id: UUID
    reviewer_user_id: UUID
    decision: str
    review_notes: str
    evidence_urls: list[str]
    previous_rights_metadata: RightsMetadata
    resulting_rights_metadata: RightsMetadata
    created_at: datetime


class ResearchSourceRightsReviewResult(BaseModel):
    review: ResearchSourceRightsReviewResponse
    source: ResearchSourceResponse
    requires_new_story_run: bool = True
