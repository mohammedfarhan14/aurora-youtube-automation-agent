"""Validated rights metadata carried with source and media provenance."""

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RightsStatus(StrEnum):
    UNKNOWN = "unknown"
    RESEARCH_ONLY = "research_only"
    REVIEW_REQUIRED = "review_required"
    CLEARED = "cleared"
    RESTRICTED = "restricted"


class RightsScope(StrEnum):
    UNKNOWN = "unknown"
    SOURCE_TEXT = "source_text"
    ABSTRACT = "abstract"
    METADATA = "metadata"
    MEDIA_ASSET = "media_asset"


class ReuseRestriction(StrEnum):
    ATTRIBUTION = "attribution"
    SHARE_ALIKE = "share_alike"
    NON_COMMERCIAL = "non_commercial"
    NO_DERIVATIVES = "no_derivatives"
    INDICATE_MODIFICATIONS = "indicate_modifications"
    NO_VERBATIM_REUSE_WITHOUT_LICENSE = "no_verbatim_reuse_without_license"
    REVIEW_PAGE_HISTORY = "review_page_history"
    UNKNOWN = "unknown"


class RightsMetadata(BaseModel):
    """Immutable rights snapshot; unknown rights are explicit, never inferred."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RightsStatus = RightsStatus.UNKNOWN
    scope: RightsScope = RightsScope.UNKNOWN
    license_ids: tuple[str, ...] = Field(default=(), max_length=10)
    license_urls: tuple[str, ...] = Field(default=(), max_length=10)
    rights_holder: str | None = Field(default=None, max_length=500)
    creators: tuple[str, ...] = Field(default=(), max_length=50)
    source_revision_url: str | None = Field(default=None, max_length=2048)
    attribution_text: str | None = Field(default=None, max_length=2000)
    reuse_restrictions: tuple[ReuseRestriction, ...] = Field(default=(), max_length=20)

    @field_validator("license_ids", "creators")
    @classmethod
    def validate_non_empty_items(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("Rights metadata list values must not be empty.")
        if len(normalized) != len(set(normalized)):
            raise ValueError("Rights metadata list values must be unique.")
        return normalized

    @field_validator("license_urls", "source_revision_url")
    @classmethod
    def validate_https_urls(
        cls, value: tuple[str, ...] | str | None
    ) -> tuple[str, ...] | str | None:
        values = value if isinstance(value, tuple) else (() if value is None else (value,))
        for item in values:
            if not isinstance(item, str) or len(item) > 2048:
                raise ValueError("Rights metadata URL is invalid.")
            parts = urlsplit(item)
            if (
                parts.scheme != "https"
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.fragment
            ):
                raise ValueError(
                    "Rights metadata URLs must be absolute HTTPS URLs without "
                    "credentials or fragments."
                )
        return value

    @model_validator(mode="after")
    def validate_cleared_rights(self) -> "RightsMetadata":
        if self.status == RightsStatus.CLEARED and (
            not self.license_ids or not self.license_urls
        ):
            raise ValueError("Cleared rights require at least one license identifier and URL.")
        if len(self.license_ids) != len(self.license_urls):
            raise ValueError("Each license identifier must have a corresponding license URL.")
        return self
