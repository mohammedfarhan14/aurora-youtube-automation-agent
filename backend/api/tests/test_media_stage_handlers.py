from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)
from app.story.contracts import (
    ClaimVerificationStatus,
    FactualClaim,
    SourceReference,
    StoryArtifact,
    StoryBeat,
    StoryScene,
)
from app.workers import media_stage_handlers


class FakeSession:
    def __init__(self, run):
        self.run = run

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def scalar(self, statement):
        return self.run


def make_story(*, unresolved_claim=False):
    claims = (
        (
            FactualClaim(
                claim="A sample claim without supporting evidence.",
                verification_status=ClaimVerificationStatus.UNVERIFIABLE,
            ),
        )
        if unresolved_claim
        else ()
    )
    return StoryArtifact(
        title="A short science story",
        hook="A surprising discovery",
        beats=(
            StoryBeat(heading="The setup", summary="Introduce the discovery."),
            StoryBeat(heading="The result", summary="Explain what it means."),
        ),
        narration=(
            "A new experiment reveals an unexpected pattern. "
            "The result changes how researchers understand the system."
        ),
        visual_direction="Abstract science visuals with a clear focal point.",
        scenes=(
            StoryScene(
                visual_direction="A glowing laboratory at night",
                narration_segment="A new experiment reveals an unexpected pattern.",
                duration_seconds=15,
            ),
            StoryScene(
                visual_direction="A bright scientific visualization",
                narration_segment="The result changes how researchers understand the system.",
                duration_seconds=15,
            ),
        ),
        factual_claims=claims,
        source_references=(
            SourceReference(
                source_id=uuid4(),
                canonical_url="https://example.com/research/source",
                title="Example research source",
                publisher="Example Research",
                rights_metadata=RightsMetadata(
                    status=RightsStatus.CLEARED,
                    scope=RightsScope.SOURCE_TEXT,
                    license_ids=("CC0-1.0",),
                    license_urls=(
                        "https://creativecommons.org/publicdomain/zero/1.0/",
                    ),
                    rights_holder="Example Research",
                    creators=("Example Research",),
                    source_revision_url="https://example.com/research/source/revision/1",
                    attribution_text="Example Research, CC0 1.0",
                    reuse_restrictions=(ReuseRestriction.ATTRIBUTION,),
                ),
            ),
        ),
    )


@pytest.mark.anyio
async def test_ai_direction_stage_builds_captions_and_blocks_unverified_claims(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    run = SimpleNamespace(
        id=production_run_id,
        organization_id=organization_id,
        channel_profile_snapshot={"language": "en"},
    )
    story = make_story(unresolved_claim=True)
    captured = {}

    async def load_story(*args, **kwargs):
        return story

    async def complete_stage(stage_id, token, *, output_manifest, **kwargs):
        captured["stage_id"] = stage_id
        captured["lease_token"] = token
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "load_latest_story_artifact", load_story)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete_stage)

    await media_stage_handlers.execute_ai_direction_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        session_factory=lambda: FakeSession(run),
    )

    manifest = captured["manifest"]
    assert captured["stage_id"] == production_stage_id
    assert captured["lease_token"] == lease_token
    assert manifest["target_duration_seconds"] == 30
    assert manifest["scene_durations_seconds"] == [15, 15]
    assert manifest["captions_vtt"].startswith("WEBVTT")
    assert "00:00:00.000 -->" in manifest["captions_vtt"]
    assert "00:00:15.000 -->" in manifest["captions_vtt"]
    assert "--> 00:00:30.000" in manifest["captions_vtt"]
    assert manifest["publication_blocked"] is True
    assert manifest["claim_flags"][0]["verification_status"] == "unverifiable"


@pytest.mark.anyio
async def test_ai_direction_stage_allows_story_without_unresolved_claims(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    run = SimpleNamespace(
        id=production_run_id,
        organization_id=organization_id,
        channel_profile_snapshot={},
    )
    captured = {}

    async def load_story(*args, **kwargs):
        return make_story()

    async def complete_stage(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "load_latest_story_artifact", load_story)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete_stage)

    await media_stage_handlers.execute_ai_direction_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        session_factory=lambda: FakeSession(run),
    )

    assert captured["manifest"]["claim_flags"] == []
    assert captured["manifest"]["publication_blocked"] is False

def test_captions_vtt_limits_cue_words_and_escapes_markup():
    scenes = [
        {
            "duration_seconds": 30,
            "narration_segment": " ".join(["research"] * 100),
        },
        {
            "duration_seconds": 3,
            "narration_segment": "A <script>unsafe</script> & useful result.",
        },
    ]

    vtt = media_stage_handlers._captions_vtt(scenes)
    cue_blocks = [
        block.splitlines()
        for block in vtt.split(chr(10) * 2)[1:]
        if block.strip()
    ]

    assert len(cue_blocks) < 105
    assert all(1 <= len(cue[1].split()) <= 5 for cue in cue_blocks)
    assert "&lt;script&gt;unsafe&lt;/script&gt;" in vtt
    assert "&amp;" in vtt
    assert "<script>" not in vtt


def test_captions_vtt_rejects_excessive_caption_density():
    scenes = [
        {
            "duration_seconds": 10,
            "narration_segment": " ".join(["word"] * 100),
        }
    ]

    with pytest.raises(ValueError, match="too dense"):
        media_stage_handlers._captions_vtt(scenes)


def test_media_rights_clearance_requires_license_and_attribution_when_applicable():
    artifact_id = uuid4()

    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={"rights_basis": "generated_in_application"},
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={"rights_basis": "generated_in_application"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={"rights_basis": "licensed"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "license_uri": "https://www.apache.org/licenses/LICENSE-2.0",
            "source_uri": "https://example.com/model-and-voice",
            "attribution": "Model and voice attribution",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "attribution": "Model and voice attribution",
        },
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "cc_by",
            "license_id": "CC-BY-4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://example.com/voice-asset",
        },
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "license_uri": "http://example.com/license",
            "source_uri": "https://example.com/model-and-voice",
            "attribution": "Model and voice attribution",
        },
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={"rights_basis": "public_domain"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "public_domain",
            "source_uri": "https://example.com/public-domain-voice",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "public_domain",
            "source_uri": "http://example.com/public-domain-voice",
        },
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={"rights_basis": "cc0"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "cc0",
            "license_id": "CC0 1.0",
            "license_uri": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source_uri": "https://example.com/cc0-voice",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="audio",
        metadata={
            "rights_basis": "cc_by",
            "license_id": "CC-BY-4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://example.com/voice-asset",
            "attribution": "Creator attribution",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={
            "rights_basis": "cc_by",
            "license_id": "CC BY 4.0",
            "attribution": "Creator attribution",
        },
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={
            "rights_basis": "cc_by",
            "license_id": "CC BY 4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "attribution": "Creator attribution",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={"rights_basis": "cc0"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={
            "rights_basis": "cc0",
            "license_id": "CC0 1.0",
            "license_uri": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
        },
    ) is None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={"rights_basis": "public_domain"},
    ) is not None
    assert media_stage_handlers._rights_clearance_flag(
        artifact_id=artifact_id,
        artifact_kind="image",
        metadata={
            "rights_basis": "public_domain",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
        },
    ) is None


def test_caption_builder_splits_long_scenes_into_readable_monotonic_cues():
    words = [f"word{index}" for index in range(66)]
    scenes = [
        {
            "duration_seconds": 15,
            "narration_segment": " ".join(words[:33]),
        },
        {
            "duration_seconds": 15,
            "narration_segment": " ".join(words[33:]),
        },
    ]

    captions = media_stage_handlers._captions_vtt(scenes)
    blocks = [block for block in captions.strip().split("\n\n")[1:] if block]
    cues = []
    for block in blocks:
        lines = block.splitlines()
        start, end = lines[0].split(" --> ")
        cues.append((start, end, " ".join(lines[1:])))

    assert len(cues) < len(words)
    assert cues[0][0] == "00:00:00.000"
    assert any(start == "00:00:15.000" for start, _, _ in cues)
    assert cues[-1][1] == "00:00:30.000"
    assert all(1 <= len(text.split()) <= 5 for _, _, text in cues)
    assert " ".join(text for _, _, text in cues).split() == words

    previous_end = "00:00:00.000"
    for start, end, _ in cues:
        assert start == previous_end
        assert end > start
        previous_end = end



def test_caption_sidecar_validator_accepts_complete_monotonic_timeline():
    captions = (
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:15.000\n"
        "A short opening sentence.\n\n"
        "00:00:15.000 --> 00:00:30.000\n"
        "A concise concluding sentence.\n"
    )

    media_stage_handlers._validate_caption_sidecar(captions, duration_ms=30_000)


@pytest.mark.parametrize(
    ("captions", "message"),
    [
        (
            "WEBVTT\n\n00:00:00.000 --> 00:00:16.000\nFirst cue.\n\n"
            "00:00:15.000 --> 00:00:30.000\nSecond cue.\n",
            "overlapping",
        ),
        (
            "WEBVTT\n\n00:00:00.000 --> 00:00:31.000\nLate cue.\n",
            "beyond",
        ),
        (
            "WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n"
            "one two three four five six seven eight\n",
            "seven words",
        ),
        (
            "WEBVTT\n\n00:00:00.000 --> 00:00:29.000\n"
            "Timeline ends early.\n",
            "does not cover",
        ),
        (
            "WEBVTT\n\n00:00:00.000 --> 00:00:00.000\n"
            "Zero duration.\n",
            "overlapping or empty",
        ),
        (
            "WEBVTT\n\n00:00:00.000 --> 00:61:00.000\n"
            "Invalid timestamp.\n",
            "invalid timestamp",
        ),
    ],
)
def test_caption_sidecar_validator_rejects_invalid_timing_and_content(captions, message):
    with pytest.raises(media_stage_handlers.MediaQualityGateError, match=message):
        media_stage_handlers._validate_caption_sidecar(captions, duration_ms=30_000)


def test_caption_sidecar_validator_rejects_missing_or_empty_cues():
    with pytest.raises(media_stage_handlers.MediaQualityGateError, match="at least one cue"):
        media_stage_handlers._validate_caption_sidecar("WEBVTT\n", duration_ms=30_000)

    with pytest.raises(media_stage_handlers.MediaQualityGateError, match="empty"):
        media_stage_handlers._validate_caption_sidecar(
            "WEBVTT\n\n00:00:00.000 --> 00:00:30.000\n   \n",
            duration_ms=30_000,
        )
