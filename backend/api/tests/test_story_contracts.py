from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.story.contracts import (
    ClaimEvidence,
    ClaimVerificationStatus,
    EvidenceRelation,
    FactualClaim,
    SourceReference,
    StoryArtifact,
    StoryBeat,
    StoryScene,
    minimum_narration_words,
)


def make_artifact() -> StoryArtifact:
    source_id = uuid4()
    evidence_id = uuid4()
    return StoryArtifact(
        title="How solar cells turn light into electricity",
        hook="What if sunlight could power your home?",
        beats=(
            StoryBeat(heading="The effect", summary="Light can release charge carriers."),
            StoryBeat(heading="The result", summary="A cell directs charge into a circuit."),
        ),
        narration="Sunlight carries energy. In a solar cell, light can free electrons. "
        "The cell's internal structure guides their movement, creating an electric current.",
        visual_direction="Use a simple animated cross-section of a photovoltaic cell.",
        scenes=(
            StoryScene(
                visual_direction="Sunlight reaches a rooftop panel.",
                narration_segment="Sunlight carries energy. In a solar cell,",
                duration_seconds=10,
            ),
            StoryScene(
                visual_direction="Show charge carriers moving through a cell.",
                narration_segment=(
                    "light can free electrons. The cell's internal structure guides their "
                    "movement, creating an electric current."
                ),
                duration_seconds=20,
            ),
        ),
        target_duration_seconds=30,
        word_budget=70,
        source_references=(SourceReference(source_id=source_id, evidence_ids=(evidence_id,)),),
        factual_claims=(
            FactualClaim(
                claim="A solar cell can convert light into electrical energy.",
                verification_status=ClaimVerificationStatus.SUPPORTED,
                source_ids=(source_id,),
                evidence=(
                    ClaimEvidence(
                        source_id=source_id,
                        evidence_id=evidence_id,
                        relation=EvidenceRelation.SUPPORTS,
                    ),
                ),
                model_confidence=0.92,
            ),
        ),
    )


def test_story_artifact_accepts_traceable_supported_claim():
    artifact = make_artifact()

    assert artifact.schema_version == "1.3.0"
    assert artifact.target_duration_seconds == 30
    assert artifact.factual_claims[0].verification_status == ClaimVerificationStatus.SUPPORTED


def test_story_v1_4_minimum_words_matches_renderer_duration_floor():
    payload = make_artifact().model_dump()
    narration_words = [f"word{index}." for index in range(68)]
    payload["schema_version"] = "1.4.0"
    payload["narration"] = " ".join(narration_words)
    payload["scenes"] = (
        StoryScene(
            visual_direction="Opening visual.",
            narration_segment=" ".join(narration_words[:34]),
            duration_seconds=15,
        ),
        StoryScene(
            visual_direction="Closing visual.",
            narration_segment=" ".join(narration_words[34:]),
            duration_seconds=15,
        ),
    )

    artifact = StoryArtifact.model_validate(payload)

    assert len(artifact.narration.split()) == 68


def test_minimum_story_lengths_scale_with_target_duration():
    assert minimum_narration_words(15) == 29
    assert minimum_narration_words(30) == 58
    assert minimum_narration_words(45) == 87


def test_story_artifact_preserves_legacy_v1_0_payload_compatibility():
    payload = make_artifact().model_dump()
    payload["schema_version"] = "1.0.0"
    payload["scenes"] = (
        StoryScene(
            visual_direction="First scene.",
            narration_segment="Legacy abbreviated caption.",
            duration_seconds=10,
        ),
        StoryScene(
            visual_direction="Second scene.",
            narration_segment="Legacy paraphrased caption.",
            duration_seconds=20,
        ),
    )

    artifact = StoryArtifact.model_validate(payload)

    assert artifact.schema_version == "1.0.0"


def test_story_rejects_scene_segments_that_do_not_match_complete_narration():
    payload = make_artifact().model_dump()
    payload["scenes"] = (
        StoryScene(
            visual_direction="First scene.",
            narration_segment="A paraphrased opening.",
            duration_seconds=10,
        ),
        StoryScene(
            visual_direction="Second scene.",
            narration_segment="An unrelated ending.",
            duration_seconds=20,
        ),
    )

    with pytest.raises(ValidationError, match="reproduce the complete narration"):
        StoryArtifact.model_validate(payload)


def test_story_accepts_scene_segments_with_normalized_whitespace():
    payload = make_artifact().model_dump()
    payload["scenes"] = (
        StoryScene(
            visual_direction="First scene.",
            narration_segment="Sunlight carries energy.   In a solar cell,",
            duration_seconds=10,
        ),
        StoryScene(
            visual_direction="Second scene.",
            narration_segment=(
                "light can free electrons. The cell's internal structure guides their "
                "movement, creating an electric current."
            ),
            duration_seconds=20,
        ),
    )

    StoryArtifact.model_validate(payload)


def test_story_rejects_scene_durations_that_do_not_sum_to_target():
    payload = make_artifact().model_dump()
    payload["scenes"] = (
        StoryScene(
            visual_direction="First scene.",
            narration_segment="First.",
            duration_seconds=10,
        ),
        StoryScene(
            visual_direction="Second scene.",
            narration_segment="Second.",
            duration_seconds=10,
        ),
    )

    with pytest.raises(ValidationError, match="Scene durations"):
        StoryArtifact.model_validate(payload)


def test_story_rejects_narration_over_word_budget():
    payload = make_artifact().model_dump()
    payload["narration"] = " ".join(["word"] * 71)

    with pytest.raises(ValidationError, match="word budget"):
        StoryArtifact.model_validate(payload)


def test_supported_claim_requires_supporting_evidence():
    source_id = uuid4()

    with pytest.raises(ValidationError, match="supporting evidence"):
        FactualClaim(
            claim="A factual assertion.",
            verification_status=ClaimVerificationStatus.SUPPORTED,
            source_ids=(source_id,),
        )


def test_conflicting_claim_requires_a_contradicting_excerpt():
    source_id = uuid4()

    with pytest.raises(ValidationError, match="contradicting excerpt"):
        FactualClaim(
            claim="A factual assertion.",
            verification_status=ClaimVerificationStatus.CONFLICTING,
            source_ids=(source_id,),
        )


def test_insufficient_evidence_is_distinct_from_unverifiable():
    source_id = uuid4()

    insufficient = FactualClaim(
        claim="A factual assertion.",
        verification_status=ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
        source_ids=(source_id,),
    )
    unverifiable = FactualClaim(
        claim="A factual assertion.",
        verification_status=ClaimVerificationStatus.UNVERIFIABLE,
    )

    assert insufficient.source_ids == (source_id,)
    assert unverifiable.source_ids == ()


def test_unverifiable_claim_cannot_attach_source_ids():
    with pytest.raises(ValidationError, match="cannot claim source"):
        FactualClaim(
            claim="A factual assertion.",
            verification_status=ClaimVerificationStatus.UNVERIFIABLE,
            source_ids=(uuid4(),),
        )


def test_artifact_rejects_claim_source_not_in_story_references():
    payload = make_artifact().model_dump()
    payload["factual_claims"][0]["source_ids"] = (uuid4(),)

    with pytest.raises(ValidationError, match="outside this story artifact"):
        StoryArtifact.model_validate(payload)


def test_artifact_rejects_evidence_id_not_attached_to_claim_source():
    payload = make_artifact().model_dump()
    payload["factual_claims"][0]["evidence"][0]["evidence_id"] = uuid4()

    with pytest.raises(ValidationError, match="not attached to its referenced source"):
        StoryArtifact.model_validate(payload)


def test_contracts_reject_unknown_fields_and_invalid_confidence():
    payload = make_artifact().model_dump()
    payload["untrusted_tool_call"] = "shell"

    with pytest.raises(ValidationError):
        StoryArtifact.model_validate(payload)

    with pytest.raises(ValidationError):
        FactualClaim(
            claim="A factual assertion.",
            verification_status=ClaimVerificationStatus.UNVERIFIABLE,
            model_confidence=1.1,
        )


def test_insufficient_evidence_can_reference_a_source_without_excerpts():
    source_id = uuid4()
    artifact = make_artifact().model_dump()
    artifact["source_references"] = (SourceReference(source_id=source_id),)
    artifact["factual_claims"] = (
        FactualClaim(
            claim="A factual assertion.",
            verification_status=ClaimVerificationStatus.INSUFFICIENT_EVIDENCE,
            source_ids=(source_id,),
        ),
    )

    validated = StoryArtifact.model_validate(artifact)

    assert validated.source_references[0].evidence_ids == ()
    assert (
        validated.factual_claims[0].verification_status
        == ClaimVerificationStatus.INSUFFICIENT_EVIDENCE
    )


def test_source_reference_rejects_duplicate_evidence_ids():
    evidence_id = uuid4()

    with pytest.raises(ValidationError, match="must be unique"):
        SourceReference(
            source_id=uuid4(),
            evidence_ids=(evidence_id, evidence_id),
        )
