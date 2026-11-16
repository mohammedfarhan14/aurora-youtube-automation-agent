from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OrganizationStudioSettings(BaseModel):
    """Tenant-owned automation policy and creative defaults (never credentials)."""

    model_config = ConfigDict(extra="forbid")

    research_enabled: bool = True
    video_generation_enabled: bool = True
    automatic_publishing_enabled: bool = False
    target_duration_seconds: Literal[15, 30, 45] = 30
    qwen_thinking_mode: Literal["off", "on"] = "off"
    story_max_output_tokens: int | None = Field(default=None, ge=512, le=4096)
    story_timeout_seconds: int = Field(default=120, ge=30, le=600)
    language: str = Field(default="English", min_length=1, max_length=80)
    tone: str = Field(default="Clear, engaging, documentary", min_length=1, max_length=120)
    audience: str = Field(default="Curious general audience", min_length=1, max_length=300)
    additional_rules: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=20,
    )

    @field_validator("language", "tone", "audience", mode="before")
    @classmethod
    def normalize_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("additional_rules", mode="before")
    @classmethod
    def normalize_rules(cls, value: object) -> object:
        if not isinstance(value, list):
            return value
        rules = [item.strip() if isinstance(item, str) else item for item in value]
        return list(dict.fromkeys(item for item in rules if item))


class OrganizationStudioSettingsResponse(BaseModel):
    settings: OrganizationStudioSettings
    updated_at: datetime
    updated_by_user_id: UUID | None
    can_edit: bool

    model_config = ConfigDict(from_attributes=True)
