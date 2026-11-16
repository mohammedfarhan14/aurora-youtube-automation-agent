"""Versioned, evidence-linked story and claim contracts.

These models validate artifact structure and provenance references only. They do
not treat model confidence as evidence or independently establish factual truth.
"""

from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.rights.contracts import RightsMetadata


def minimum_narration_words(target_duration_seconds: int) -> int:
    """Require narration to plausibly fill the target duration at natural speech pace."""
    # Kokoro's measured local narration pace is about 128 WPM. A 115 WPM floor
    # allows natural pauses while still producing enough speech to fill a
    # 30-second short without forcing the small local LLM to pad its script.
    return (target_duration_seconds * 115 + 59) // 60


class StrictContract(BaseModel):
    """Base for immutable, strict contracts used at workflow boundaries."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class EvidenceRelation(StrEnum):
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"


class ClaimVerificationStatus(StrEnum):
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    CONFLICTING = "conflicting"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    UNVERIFIABLE = "unverifiable"


class StoryBeat(StrictContract):
    """One ordered narrative beat in a short-form video."""

    heading: str = Field(min_length=1, max_length=120)
    summary: str = Field(min_length=1, max_length=500)


class StoryScene(StrictContract):
    """One visual scene with a bounded duration."""

    visual_direction: str = Field(min_length=1, max_length=1000)
    narration_segment: str = Field(min_length=1, max_length=500)
    duration_seconds: int = Field(gt=0, le=30)


class SourceReference(StrictContract):
    """A source and zero or more evidence excerpts considered from it."""

    source_id: UUID
    evidence_ids: tuple[UUID, ...] = Field(default=(), max_length=50)
    canonical_url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    publisher: str | None = Field(default=None, max_length=250)
    rights_metadata: RightsMetadata = Field(default_factory=RightsMetadata)

    @field_validator("canonical_url")
    @classmethod
    def validate_canonical_url(cls, value: str | None) -> str | None:
        if value is None:
            return value
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ValueError("Source canonical URLs must be absolute HTTP(S) URLs.")
        return value

    @model_validator(mode="after")
    def validate_unique_evidence_ids(self) -> "SourceReference":
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("Source evidence IDs must be unique.")
        return self


class ClaimEvidence(StrictContract):
    """An explicit assessment of one excerpt against one atomic claim."""

    source_id: UUID
    evidence_id: UUID
    relation: EvidenceRelation


class FactualClaim(StrictContract):
    """Atomic factual assertion with traceable source and evidence references."""

    claim: str = Field(min_length=1, max_length=1000)
    verification_status: ClaimVerificationStatus
    source_ids: tuple[UUID, ...] = Field(default=(), max_length=20)
    evidence: tuple[ClaimEvidence, ...] = Field(default=(), max_length=50)
    independent_support_group_count: int = Field(default=0, ge=0, le=20)
    independent_contradiction_group_count: int = Field(default=0, ge=0, le=20)
    model_confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_status_evidence_consistency(self) -> "FactualClaim":
        has_sources = bool(self.source_ids)
        has_evidence = bool(self.evidence)
        has_support = any(item.relation == EvidenceRelation.SUPPORTS for item in self.evidence)
        has_contradiction = any(
            item.relation == EvidenceRelation.CONTRADICTS for item in self.evidence
        )

        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("Claim source IDs must be unique.")
        if len({item.evidence_id for item in self.evidence}) != len(self.evidence):
            raise ValueError("Claim evidence IDs must be unique.")

        if self.verification_status == ClaimVerificationStatus.SUPPORTED:
            if not has_sources or not has_support or has_contradiction:
                raise ValueError("Supported claims need supporting evidence and no contradictions.")
        elif self.verification_status == ClaimVerificationStatus.CONTRADICTED:
            if not has_sources or not has_contradiction or has_support:
                raise ValueError(
                    "Contradicted claims need contradicting evidence and no supporting evidence."
                )
        elif self.verification_status == ClaimVerificationStatus.CONFLICTING:
            # Keep accepting historical artifacts where a single contradicting
            # excerpt was labelled conflicting before CONTRADICTED was introduced.
            if not has_sources or not has_contradiction:
                raise ValueError("Conflicting claims need at least one contradicting excerpt.")
        elif self.verification_status == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE:
            if not has_sources or has_evidence:
                raise ValueError(
                    "Insufficient-evidence claims need source references but no assessed excerpts."
                )
        elif self.verification_status == ClaimVerificationStatus.UNVERIFIABLE:
            if has_sources or has_evidence:
                raise ValueError("Unverifiable claims cannot claim source or evidence references.")

        return self


class StoryArtifact(StrictContract):
    """Versioned script package for one approximately 30-second video."""

    schema_version: Literal["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0"] = "1.3.0"
    title: str = Field(min_length=1, max_length=120)
    hook: str = Field(min_length=1, max_length=240)
    beats: tuple[StoryBeat, ...] = Field(min_length=2, max_length=5)
    narration: str = Field(min_length=1, max_length=2000)
    visual_direction: str = Field(min_length=1, max_length=2000)
    scenes: tuple[StoryScene, ...] = Field(min_length=2, max_length=6)
    target_duration_seconds: int = Field(default=30, ge=15, le=45)
    word_budget: int = Field(default=70, ge=35, le=110)
    source_references: tuple[SourceReference, ...] = Field(default=(), max_length=50)
    factual_claims: tuple[FactualClaim, ...] = Field(default=(), max_length=30)

    @model_validator(mode="after")
    def validate_artifact_consistency(self) -> "StoryArtifact":
        if sum(scene.duration_seconds for scene in self.scenes) != self.target_duration_seconds:
            raise ValueError("Scene durations must sum to the target duration.")

        narration_words = len(self.narration.split())
        if narration_words > self.word_budget:
            raise ValueError("Narration exceeds the declared word budget.")

        if self.schema_version == "1.4.0" and narration_words < minimum_narration_words(
            self.target_duration_seconds
        ):
            raise ValueError(
                "Narration is too short for its target video duration and word budget."
            )

        if self.schema_version in {"1.1.0", "1.2.0", "1.3.0", "1.4.0"}:
            normalized_narration = " ".join(self.narration.split())
            normalized_scene_narration = " ".join(
                " ".join(scene.narration_segment.split()) for scene in self.scenes
            )
            if normalized_scene_narration != normalized_narration:
                raise ValueError(
                    "Scene narration segments must reproduce the complete narration "
                    "verbatim and in order."
                )

        source_evidence = {
            reference.source_id: set(reference.evidence_ids) for reference in self.source_references
        }
        if len(source_evidence) != len(self.source_references):
            raise ValueError("Source references must have unique source IDs.")

        for claim in self.factual_claims:
            if not set(claim.source_ids).issubset(source_evidence):
                raise ValueError("Claim references a source outside this story artifact.")
            for assessment in claim.evidence:
                if assessment.source_id not in source_evidence:
                    raise ValueError("Claim evidence references an unknown source.")
                if assessment.source_id not in claim.source_ids:
                    raise ValueError("Claim evidence source must be listed on the claim.")
                if assessment.evidence_id not in source_evidence[assessment.source_id]:
                    raise ValueError("Claim evidence ID is not attached to its referenced source.")

        return self
