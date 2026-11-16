import hashlib
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from app.media.providers import (
    AudioAsset,
    MediaQualityReport,
    TTSWordTiming,
    VideoAsset,
    VisualAsset,
)
from app.rights.contracts import (
    ReuseRestriction,
    RightsMetadata,
    RightsScope,
    RightsStatus,
)
from app.services.media_artifacts import MediaArtifactResult
from app.workers import media_stage_handlers


def make_story_with_source_rights(
    *,
    status=RightsStatus.CLEARED,
    restrictions=(ReuseRestriction.ATTRIBUTION,),
):
    source_id = uuid4()
    license_ids = ("CC0-1.0",) if status == RightsStatus.CLEARED else ()
    license_urls = (
        ("https://creativecommons.org/publicdomain/zero/1.0/",)
        if status == RightsStatus.CLEARED
        else ()
    )
    rights = RightsMetadata(
        status=status,
        scope=RightsScope.SOURCE_TEXT,
        license_ids=license_ids,
        license_urls=license_urls,
        rights_holder="Example Research",
        creators=("Example Research",),
        source_revision_url="https://example.com/revision/1",
        attribution_text="Example Research, CC0 1.0",
        reuse_restrictions=restrictions,
    )
    source = SimpleNamespace(
        source_id=source_id,
        canonical_url="https://example.com/research/source",
        title="Example research source",
        publisher="Example Research",
        rights_metadata=rights,
    )
    scene = SimpleNamespace(
        visual_direction="A neutral scientific illustration",
        narration_segment="A verified fact about the topic.",
        duration_seconds=10,
    )
    story = SimpleNamespace(
        title="Test story",
        hook="A concise hook.",
        narration="A verified fact about the topic.",
        target_duration_seconds=30,
        scenes=(scene,),
        factual_claims=(),
        source_references=(source,),
    )
    return story


def make_png(color: tuple[int, int, int]) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (540, 960), color).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeStorage:
    def __init__(self, objects):
        self.objects = objects

    async def get(self, storage_key):
        return self.objects[storage_key]


class FakeTTS:
    async def synthesize(self, *, text, voice_id, language, speed=1.0, audio_format="wav"):
        return AudioAsset(
            content=b"RIFF-audio",
            mime_type="audio/wav",
            duration_ms=28_000,
            sample_rate_hz=16_000,
            channels=1,
            provider_id="test-tts",
            model_id="test-model",
            voice_id=voice_id,
            request_id="tts-req-1",
        )

    async def aclose(self):
        return None


class FakeVisualProvider:
    async def generate(
        self,
        *,
        visual_direction,
        narration_segment,
        scene_index,
        visual_style="",
        lighting="cinematic",
    ):
        return VisualAsset(
            content=b"\x89PNG\r\n\x1a\n" + bytes([scene_index]),
            mime_type="image/png",
            width=540,
            height=960,
            provider_id="fake-visual",
            rights_basis="generated_in_application",
            contains_synthetic_media=False,
        )


class FakeWikimediaVisualProvider:
    async def generate(
        self,
        *,
        visual_direction,
        narration_segment,
        scene_index,
        visual_style="",
        lighting="cinematic",
    ):
        return VisualAsset(
            content=bytes.fromhex("ffd8ff") + b"commons-jpeg",
            mime_type="image/jpeg",
            width=640,
            height=360,
            provider_id="wikimedia_commons",
            rights_basis="cc_by",
            attribution="Jane Doe — File — Source — CC BY 4.0",
            source_uri="https://commons.wikimedia.org/wiki/File:Example.jpg",
            license_id="CC BY 4.0",
            license_uri="https://creativecommons.org/licenses/by/4.0/",
            contains_synthetic_media=False,
        )


class FakeRenderer:
    def __init__(self, video=None, report=None):
        self.video = video
        self.report = report
        self.request = None
        self.inspected = None

    async def render(self, request):
        self.request = request
        return self.video

    async def inspect(self, content):
        self.inspected = content
        return self.report


def make_artifact(
    *,
    artifact_kind,
    content,
    metadata=None,
    mime_type="application/octet-stream",
    duration_ms=None,
    width=None,
    height=None,
):
    return MediaArtifactResult(
        id=uuid4(),
        organization_id=uuid4(),
        production_run_id=uuid4(),
        production_stage_id=uuid4(),
        artifact_kind=artifact_kind,
        storage_backend="local",
        storage_key=f"tenant/{uuid4()}.bin",
        mime_type=mime_type,
        sha256=hashlib.sha256(content).hexdigest(),
        byte_size=len(content),
        duration_ms=duration_ms,
        width=width,
        height=height,
        metadata_json=metadata or {},
    )


@pytest.mark.anyio
async def test_narration_stage_persists_provider_and_rights_provenance(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    run = type(
        "Run",
        (),
        {"channel_profile_snapshot": {"language": "en-US"}},
    )()
    story = type("Story", (), {"narration": "A concise verified narration."})()
    stored = make_artifact(
        artifact_kind="audio",
        content=b"RIFF-audio",
        mime_type="audio/wav",
        duration_ms=28_000,
    )
    captured = {}

    async def load_run_and_story(*args, **kwargs):
        return run, story

    async def store(**kwargs):
        captured["store"] = kwargs
        return stored

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_run_and_story", load_run_and_story)
    monkeypatch.setattr(media_stage_handlers, "store_media_artifact", store)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_narration_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=1,
        provider=FakeTTS(),
        voice_id="voice-en",
        rights_basis="licensed",
        license_id="Apache-2.0",
        license_uri="https://www.apache.org/licenses/LICENSE-2.0",
        source_uri="https://example.com/kokoro-model-and-voice",
        attribution="Model and voice pack",
        storage=FakeStorage({}),
        session_factory=None,
    )

    assert captured["store"]["metadata"]["language"] == "en-US"
    assert captured["store"]["metadata"]["license_id"] == "Apache-2.0"
    assert captured["store"]["metadata"]["license_uri"] == (
        "https://www.apache.org/licenses/LICENSE-2.0"
    )
    assert captured["store"]["metadata"]["source_uri"] == (
        "https://example.com/kokoro-model-and-voice"
    )
    assert captured["store"]["metadata"]["attribution"] == "Model and voice pack"
    assert captured["manifest"]["audio_artifact_id"] == str(stored.id)


@pytest.mark.anyio
async def test_visual_generation_stage_persists_ordered_scene_assets(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    stored = []
    captured = {}

    async def load_manifest(*args, **kwargs):
        return {
            "scenes": [
                {
                    "scene_index": 0,
                    "visual_direction": "First scene",
                    "narration_segment": "First narration.",
                    "duration_seconds": 15,
                },
                {
                    "scene_index": 1,
                    "visual_direction": "Second scene",
                    "narration_segment": "Second narration.",
                    "duration_seconds": 15,
                },
            ]
        }

    async def store(**kwargs):
        stored.append(kwargs)
        return make_artifact(
            artifact_kind="image",
            content=kwargs["content"],
            mime_type="image/png",
            width=kwargs["width"],
            height=kwargs["height"],
        )

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "store_media_artifact", store)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_visual_generation_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=1,
        provider=FakeVisualProvider(),
        storage=FakeStorage({}),
        session_factory=None,
    )

    assert len(stored) == 2
    assert [item["artifact_slot"] for item in stored] == ["scene-000", "scene-001"]
    assert all(item["metadata"]["rights_basis"] == "generated_in_application" for item in stored)
    assert [item["scene_index"] for item in captured["manifest"]["scenes"]] == [0, 1]


@pytest.mark.anyio
async def test_visual_generation_stage_persists_jpeg_license_and_provider_metadata(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    stored = []
    captured = {}

    async def load_manifest(*args, **kwargs):
        return {
            "scenes": [
                {
                    "scene_index": 0,
                    "visual_direction": "A historical building",
                    "narration_segment": "A short architectural history.",
                    "duration_seconds": 30,
                }
            ]
        }

    async def store(**kwargs):
        stored.append(kwargs)
        return make_artifact(
            artifact_kind="image",
            content=kwargs["content"],
            mime_type=kwargs["mime_type"],
            width=kwargs["width"],
            height=kwargs["height"],
            metadata=kwargs["metadata"],
        )

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "store_media_artifact", store)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_visual_generation_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=1,
        provider=FakeWikimediaVisualProvider(),
        storage=FakeStorage({}),
        session_factory=None,
    )

    assert stored[0]["extension"] == "jpg"
    assert stored[0]["mime_type"] == "image/jpeg"
    assert stored[0]["metadata"]["license_id"] == "CC BY 4.0"
    assert stored[0]["metadata"]["license_uri"] == "https://creativecommons.org/licenses/by/4.0/"
    assert stored[0]["metadata"]["source_uri"].startswith("https://commons.wikimedia.org/")
    assert captured["manifest"]["provider_ids"] == ["wikimedia_commons"]
    assert captured["manifest"]["scenes"][0]["provider_id"] == "wikimedia_commons"
    assert captured["kwargs"]["provider_id"] == "wikimedia_commons"


@pytest.mark.anyio
async def test_rendering_stage_integrity_checks_inputs_and_persists_video_and_captions(
    monkeypatch,
):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    audio_content = b"RIFF-audio"
    image_one = make_png((24, 48, 72))
    image_two = make_png((72, 48, 24))
    audio_record = make_artifact(
        artifact_kind="audio",
        content=audio_content,
        mime_type="audio/wav",
        duration_ms=28_000,
        metadata={
            "sample_rate_hz": 16_000,
            "channels": 1,
            "provider_id": "test-tts",
            "model_id": "test-model",
            "voice_id": "voice-en",
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "license_uri": "https://www.apache.org/licenses/LICENSE-2.0",
            "source_uri": "https://example.com/kokoro-model-and-voice",
            "attribution": "Model and voice pack",
        },
    )
    visual_records = [
        make_artifact(
            artifact_kind="image",
            content=image_one,
            mime_type="image/png",
            width=540,
            height=960,
            metadata={
                "provider_id": "wikimedia_commons",
                "rights_basis": "cc_by",
                "attribution": "Jane Doe — File — Source — CC BY 4.0",
                "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "license_id": "CC BY 4.0",
                "license_uri": "https://creativecommons.org/licenses/by/4.0/",
                "contains_synthetic_media": False,
            },
        ),
        make_artifact(
            artifact_kind="image",
            content=image_two,
            mime_type="image/png",
            width=540,
            height=960,
            metadata={
                "provider_id": "fake-visual",
                "rights_basis": "generated_in_application",
                "contains_synthetic_media": False,
            },
        ),
    ]
    records = {
        audio_record.id: audio_record,
        visual_records[0].id: visual_records[0],
        visual_records[1].id: visual_records[1],
    }
    objects = {
        audio_record.storage_key: audio_content,
        visual_records[0].storage_key: image_one,
        visual_records[1].storage_key: image_two,
    }
    story = make_story_with_source_rights()
    source_rights = media_stage_handlers._source_rights_snapshot(story)
    manifests = {
        "ai_direction": {
            "captions_vtt": "WEBVTT\n\n00:00:00.000 --> 00:00:30.000\nNarration\n",
            "scenes": [
                {
                    "scene_index": 0,
                    "duration_seconds": 15,
                    "narration_segment": "A concise opening scene.",
                },
                {
                    "scene_index": 1,
                    "duration_seconds": 15,
                    "narration_segment": "A concise concluding scene.",
                },
            ],
            "scene_durations_seconds": [15, 15],
            "target_duration_seconds": 30,
            "source_rights": source_rights,
        },
        "narration": {"audio_artifact_id": str(audio_record.id)},
        "visual_generation": {
            "scenes": [
                {"scene_index": 1, "artifact_id": str(visual_records[1].id)},
                {"scene_index": 0, "artifact_id": str(visual_records[0].id)},
            ]
        },
    }
    video = VideoAsset(
        content=b"mp4-video",
        mime_type="video/mp4",
        duration_ms=30_000,
        width=1080,
        height=1920,
        video_codec="h264",
        audio_codec="aac",
        provider_id="ffmpeg",
    )
    renderer = FakeRenderer(video=video)
    stored_outputs = []
    captured = {}

    async def load_manifest(session_factory, *, production_run_id, stage_key):
        return manifests[stage_key]

    async def load_artifact(session_factory, **kwargs):
        return records[kwargs["artifact_id"]]

    async def store(**kwargs):
        stored_outputs.append(kwargs)
        return make_artifact(
            artifact_kind=kwargs["artifact_kind"],
            content=kwargs["content"],
            mime_type=kwargs["mime_type"],
            duration_ms=kwargs.get("duration_ms"),
            width=kwargs.get("width"),
            height=kwargs.get("height"),
        )

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "_load_media_artifact", load_artifact)
    monkeypatch.setattr(media_stage_handlers, "store_media_artifact", store)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_rendering_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        attempt_count=1,
        renderer=renderer,
        storage=FakeStorage(objects),
        session_factory=None,
    )

    assert renderer.request is not None
    assert len(renderer.request.visual_assets) == 2
    assert renderer.request.visual_assets[0].content == image_one
    assert renderer.request.scene_durations_seconds == (15, 15)
    assert len(stored_outputs) == 3
    assert [item["artifact_kind"] for item in stored_outputs] == [
        "video",
        "captions",
        "thumbnail",
    ]
    assert stored_outputs[2]["mime_type"] == "image/jpeg"
    assert stored_outputs[2]["width"] == 1080
    assert stored_outputs[2]["height"] == 1920
    assert captured["manifest"]["thumbnail_artifact_id"]
    assert captured["manifest"]["duration_ms"] == 30_000
    video_metadata = stored_outputs[0]["metadata"]
    assert video_metadata["attribution_credits"][0]["license_id"] == "CC BY 4.0"
    assert video_metadata["attribution_credits"][0]["source_uri"].startswith(
        "https://commons.wikimedia.org/"
    )
    assert video_metadata["attribution_credits"][1]["license_uri"] == (
        "https://www.apache.org/licenses/LICENSE-2.0"
    )
    assert video_metadata["attribution_credits"][1]["source_uri"] == (
        "https://example.com/kokoro-model-and-voice"
    )
    assert video_metadata["attribution_credits"][2]["source_id"] == str(
        story.source_references[0].source_id
    )
    assert video_metadata["attribution_credits"][2]["rights_status"] == "cleared"
    assert video_metadata["source_rights"] == source_rights
    assert video_metadata["audio_rights"]["license_uri"] == (
        "https://www.apache.org/licenses/LICENSE-2.0"
    )
    assert video_metadata["audio_rights"]["source_uri"] == (
        "https://example.com/kokoro-model-and-voice"
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "status",
    [
        RightsStatus.UNKNOWN,
        RightsStatus.RESEARCH_ONLY,
        RightsStatus.REVIEW_REQUIRED,
        RightsStatus.RESTRICTED,
    ],
)
async def test_ai_direction_stage_blocks_unreviewed_source_rights(monkeypatch, status):
    story = make_story_with_source_rights(
        status=status,
        restrictions=(ReuseRestriction.REVIEW_PAGE_HISTORY,),
    )
    captured = {}

    async def load_story(session_factory, **kwargs):
        return None, story

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        captured["kwargs"] = kwargs
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_run_and_story", load_story)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_ai_direction_stage(
        organization_id=uuid4(),
        production_run_id=uuid4(),
        production_stage_id=uuid4(),
        lease_token=uuid4(),
        session_factory=None,
    )

    manifest = captured["manifest"]
    assert manifest["source_rights"][0]["status"] == status.value
    assert manifest["publication_blocked"] is True
    assert any(
        flag["reason"] == "source_rights_not_cleared"
        for flag in manifest["source_rights_flags"]
    )
    assert captured["kwargs"]["prompt_version"] == "media-direction.v3"
    assert manifest["visual_settings"] == {
        "style": "",
        "lighting": "cinematic",
        "motion": "gentle_drift",
        "aspect_ratio": "9:16",
    }


def test_source_rights_gate_blocks_share_alike_without_output_license():
    story = make_story_with_source_rights(
        restrictions=(ReuseRestriction.SHARE_ALIKE,),
    )
    source_rights = media_stage_handlers._source_rights_snapshot(story)

    flags = media_stage_handlers._source_rights_clearance_flags(source_rights)

    assert any(
        flag["reason"] == "source_reuse_restriction_unmet"
        and flag["reuse_restrictions"] == ["share_alike"]
        for flag in flags
    )


@pytest.mark.anyio
async def test_quality_check_blocks_approval_for_missing_tts_license_metadata(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    story = make_story_with_source_rights()
    source_rights = media_stage_handlers._source_rights_snapshot(story)
    source_credits = media_stage_handlers._source_rights_attribution_credits(source_rights)
    video_content = b"mp4-video"
    captions_content = b"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\nCaption\n"
    video_record = make_artifact(
        artifact_kind="video",
        content=video_content,
        mime_type="video/mp4",
        duration_ms=30_000,
        width=1080,
        height=1920,
        metadata={
            "attribution_credits": [
                {
                    "artifact_id": str(uuid4()),
                    "license_id": "CC BY 4.0",
                    "license_uri": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Jane Doe — Example image — CC BY 4.0",
                }
            ]
        },
    )
    caption_record = make_artifact(
        artifact_kind="captions",
        content=captions_content,
        mime_type="text/vtt",
        duration_ms=30_000,
        metadata={"rights_basis": "generated_in_application"},
    )
    audio_record = make_artifact(
        artifact_kind="audio",
        content=b"RIFF-audio",
        mime_type="audio/wav",
        duration_ms=30_000,
        metadata={"rights_basis": "licensed", "license_id": "Apache-2.0"},
    )
    visual_record = make_artifact(
        artifact_kind="image",
        content=b"png-scene",
        mime_type="image/png",
        width=540,
        height=960,
        metadata={
            "provider_id": "wikimedia_commons",
            "rights_basis": "cc_by",
            "license_id": "CC BY 4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "attribution": "Jane Doe — Example image — CC BY 4.0",
        },
    )
    video_record.metadata_json["attribution_credits"] = [
        {
            "artifact_id": str(visual_record.id),
            "provider_id": visual_record.metadata_json["provider_id"],
            "rights_basis": visual_record.metadata_json["rights_basis"],
            "source_uri": visual_record.metadata_json["source_uri"],
            "license_id": visual_record.metadata_json["license_id"],
            "license_uri": visual_record.metadata_json["license_uri"],
            "attribution": visual_record.metadata_json["attribution"],
        },
        {
            "artifact_id": str(audio_record.id),
            "provider_id": audio_record.metadata_json.get("provider_id"),
            "rights_basis": audio_record.metadata_json.get("rights_basis"),
            "source_uri": audio_record.metadata_json.get("source_uri"),
            "license_id": audio_record.metadata_json.get("license_id"),
            "license_uri": audio_record.metadata_json.get("license_uri"),
            "attribution": audio_record.metadata_json.get("attribution"),
        },
        *source_credits,
    ]
    video_record.metadata_json["source_rights"] = source_rights
    video_record.metadata_json["rights_manifest_version"] = "1.0.0"
    records = {
        video_record.id: video_record,
        caption_record.id: caption_record,
        audio_record.id: audio_record,
        visual_record.id: visual_record,
    }
    tampered_source_rights = [dict(source_rights[0])]
    tampered_source_rights[0]["canonical_url"] = "https://example.com/tampered-source"
    manifests = {
        "ai_direction": {
            "claim_flags": [],
            "publication_blocked": False,
            "target_duration_seconds": 30,
            "source_rights": tampered_source_rights,
        },
        "rendering": {
            "video_artifact_id": str(video_record.id),
            "captions_artifact_id": str(caption_record.id),
        },
        "narration": {"audio_artifact_id": str(audio_record.id)},
        "visual_generation": {
            "scenes": [{"artifact_id": str(visual_record.id)}],
        },
    }
    renderer = FakeRenderer(
        report=MediaQualityReport(
            is_valid=True,
            duration_ms=30_000,
            width=1080,
            height=1920,
            video_codec="h264",
            audio_codec="aac",
            has_audio=True,
            has_captions=True,
            integrated_loudness_lufs=-14.0,
            true_peak_dbfs=-1.0,
            black_frame_segments=(),
        )
    )
    captured = {}

    async def load_manifest(session_factory, *, production_run_id, stage_key):
        return manifests[stage_key]

    async def load_artifact(session_factory, **kwargs):
        return records[kwargs["artifact_id"]]

    async def load_story(session_factory, **kwargs):
        return None, story

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "_load_run_and_story", load_story)
    monkeypatch.setattr(media_stage_handlers, "_load_media_artifact", load_artifact)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_quality_check_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        renderer=renderer,
        storage=FakeStorage(
            {
                video_record.storage_key: video_content,
                caption_record.storage_key: captions_content,
            }
        ),
        session_factory=None,
    )

    assert renderer.inspected == video_content
    assert captured["manifest"]["publication_blocked"] is True
    assert captured["manifest"]["rights_flags"][0]["artifact_id"] == str(audio_record.id)
    assert any(
        flag["reason"] == "source_rights_manifest_mismatch"
        for flag in captured["manifest"]["rights_flags"]
    )
    assert captured["manifest"]["attribution_credits"][0]["license_id"] == "CC BY 4.0"
    assert captured["manifest"]["attribution_credits"][0]["attribution"] == (
        "Jane Doe — Example image — CC BY 4.0"
    )
    assert captured["manifest"]["attribution_credits"][1]["source_id"] == str(
        story.source_references[0].source_id
    )
    quality = captured["manifest"]["quality_report"]
    assert quality["integrated_loudness_lufs"] == -14.0
    assert quality["true_peak_dbfs"] == -1.0
    assert quality["black_frame_segments"] == []


@pytest.mark.anyio
async def test_quality_check_accepts_fully_rights_cleared_video_with_provenance(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    story = make_story_with_source_rights()
    source_rights = media_stage_handlers._source_rights_snapshot(story)
    source_credits = media_stage_handlers._source_rights_attribution_credits(source_rights)
    video_content = b"mp4-video"
    captions_content = b"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\nCaption\n"
    video_record = make_artifact(
        artifact_kind="video",
        content=video_content,
        mime_type="video/mp4",
        duration_ms=30_000,
        width=1080,
        height=1920,
        metadata={
            "contains_synthetic_media": False,
            "attribution_credits": [
                {
                    "artifact_id": str(uuid4()),
                    "license_id": "CC BY 4.0",
                    "license_uri": "https://creativecommons.org/licenses/by/4.0/",
                    "attribution": "Jane Doe — Example image — CC BY 4.0",
                }
            ]
        },
    )
    caption_record = make_artifact(
        artifact_kind="captions",
        content=captions_content,
        mime_type="text/vtt",
        duration_ms=30_000,
        metadata={"rights_basis": "generated_in_application"},
    )
    audio_record = make_artifact(
        artifact_kind="audio",
        content=b"RIFF-audio",
        mime_type="audio/wav",
        duration_ms=30_000,
        metadata={
            "provider_id": "kokoro",
            "model_id": "kokoro-82m",
            "voice_id": "af_heart",
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "license_uri": "https://www.apache.org/licenses/LICENSE-2.0/",
            "source_uri": "https://example.com/voice-license",
            "attribution": "Kokoro 82M, voice af_heart, Apache-2.0",
        },
    )
    visual_record = make_artifact(
        artifact_kind="image",
        content=b"png-scene",
        mime_type="image/png",
        width=540,
        height=960,
        metadata={
            "provider_id": "wikimedia_commons",
            "contains_synthetic_media": False,
            "rights_basis": "cc_by",
            "license_id": "CC BY 4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "attribution": "Jane Doe — Example image — CC BY 4.0",
        },
    )
    video_record.metadata_json["attribution_credits"] = [
        {
            "artifact_id": str(visual_record.id),
            "provider_id": visual_record.metadata_json["provider_id"],
            "rights_basis": visual_record.metadata_json["rights_basis"],
            "source_uri": visual_record.metadata_json["source_uri"],
            "license_id": visual_record.metadata_json["license_id"],
            "license_uri": visual_record.metadata_json["license_uri"],
            "attribution": visual_record.metadata_json["attribution"],
        },
        {
            "artifact_id": str(audio_record.id),
            "provider_id": audio_record.metadata_json["provider_id"],
            "rights_basis": audio_record.metadata_json["rights_basis"],
            "source_uri": audio_record.metadata_json["source_uri"],
            "license_id": audio_record.metadata_json["license_id"],
            "license_uri": audio_record.metadata_json["license_uri"],
            "attribution": audio_record.metadata_json["attribution"],
        },
        *source_credits,
    ]
    video_record.metadata_json["source_rights"] = source_rights
    video_record.metadata_json["rights_manifest_version"] = "1.0.0"
    records = {
        video_record.id: video_record,
        caption_record.id: caption_record,
        audio_record.id: audio_record,
        visual_record.id: visual_record,
    }
    manifests = {
        "ai_direction": {
            "claim_flags": [],
            "publication_blocked": False,
            "target_duration_seconds": 30,
            "source_rights": source_rights,
        },
        "rendering": {
            "contains_synthetic_media": False,
            "video_artifact_id": str(video_record.id),
            "captions_artifact_id": str(caption_record.id),
        },
        "narration": {"audio_artifact_id": str(audio_record.id)},
        "visual_generation": {
            "scenes": [{"artifact_id": str(visual_record.id)}],
        },
    }
    renderer = FakeRenderer(
        report=MediaQualityReport(
            is_valid=True,
            duration_ms=30_000,
            width=1080,
            height=1920,
            video_codec="h264",
            audio_codec="aac",
            has_audio=True,
            has_captions=True,
            integrated_loudness_lufs=-14.0,
            true_peak_dbfs=-1.0,
            black_frame_segments=(),
        )
    )
    captured = {}

    async def load_manifest(session_factory, *, production_run_id, stage_key):
        return manifests[stage_key]

    async def load_artifact(session_factory, **kwargs):
        return records[kwargs["artifact_id"]]

    async def load_story(session_factory, **kwargs):
        return None, story

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "_load_run_and_story", load_story)
    monkeypatch.setattr(media_stage_handlers, "_load_media_artifact", load_artifact)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_quality_check_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        renderer=renderer,
        storage=FakeStorage(
            {
                video_record.storage_key: video_content,
                caption_record.storage_key: captions_content,
            }
        ),
        session_factory=None,
    )

    assert renderer.inspected == video_content
    assert captured["manifest"]["publication_blocked"] is False
    assert captured["manifest"]["rights_flags"] == []
    assert captured["manifest"]["attribution_credits"][0]["license_id"] == "CC BY 4.0"
    assert captured["manifest"]["attribution_credits"][0]["attribution"] == (
        "Jane Doe — Example image — CC BY 4.0"
    )
    assert captured["manifest"]["attribution_credits"][2]["source_id"] == str(
        story.source_references[0].source_id
    )
    assert any(
        credit.get("license_id") == "Apache-2.0"
        for credit in captured["manifest"]["attribution_credits"]
    )
    quality = captured["manifest"]["quality_report"]
    assert quality["integrated_loudness_lufs"] == -14.0
    assert quality["true_peak_dbfs"] == -1.0
    assert quality["black_frame_segments"] == []




@pytest.mark.anyio
async def test_quality_check_rejects_missing_rendered_attribution_credits(monkeypatch):
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    story = make_story_with_source_rights()
    source_rights = media_stage_handlers._source_rights_snapshot(story)
    video_content = b"mp4-video"
    captions_content = b"WEBVTT\n\n00:00:00.000 --> 00:00:30.000\nCaption\n"

    audio_record = make_artifact(
        artifact_kind="audio",
        content=b"RIFF-audio",
        mime_type="audio/wav",
        duration_ms=30_000,
        metadata={
            "provider_id": "test-tts",
            "rights_basis": "licensed",
            "license_id": "Apache-2.0",
            "license_uri": "https://www.apache.org/licenses/LICENSE-2.0",
            "source_uri": "https://example.com/voice",
            "attribution": "Test TTS model and voice",
        },
    )
    visual_record = make_artifact(
        artifact_kind="image",
        content=b"png-scene",
        mime_type="image/png",
        width=540,
        height=960,
        metadata={
            "provider_id": "wikimedia_commons",
            "rights_basis": "cc_by",
            "license_id": "CC BY 4.0",
            "license_uri": "https://creativecommons.org/licenses/by/4.0/",
            "source_uri": "https://commons.wikimedia.org/wiki/File:Example.jpg",
            "attribution": "Jane Doe — Example image — CC BY 4.0",
        },
    )
    video_record = make_artifact(
        artifact_kind="video",
        content=video_content,
        mime_type="video/mp4",
        duration_ms=30_000,
        width=1080,
        height=1920,
        metadata={
            "attribution_credits": [],
            "source_rights": source_rights,
            "rights_manifest_version": "1.0.0",
        },
    )
    caption_record = make_artifact(
        artifact_kind="captions",
        content=captions_content,
        mime_type="text/vtt",
        duration_ms=30_000,
        metadata={"rights_basis": "generated_in_application"},
    )
    records = {
        video_record.id: video_record,
        caption_record.id: caption_record,
        audio_record.id: audio_record,
        visual_record.id: visual_record,
    }
    manifests = {
        "ai_direction": {
            "claim_flags": [],
            "publication_blocked": False,
            "target_duration_seconds": 30,
            "source_rights": source_rights,
        },
        "rendering": {
            "video_artifact_id": str(video_record.id),
            "captions_artifact_id": str(caption_record.id),
        },
        "narration": {"audio_artifact_id": str(audio_record.id)},
        "visual_generation": {
            "scenes": [{"artifact_id": str(visual_record.id)}],
        },
    }
    renderer = FakeRenderer(
        report=MediaQualityReport(
            is_valid=True,
            duration_ms=30_000,
            width=1080,
            height=1920,
            video_codec="h264",
            audio_codec="aac",
            has_audio=True,
            has_captions=True,
            integrated_loudness_lufs=-14.0,
            true_peak_dbfs=-1.0,
            black_frame_segments=(),
        )
    )
    captured = {}

    async def load_manifest(session_factory, *, production_run_id, stage_key):
        return manifests[stage_key]

    async def load_artifact(session_factory, **kwargs):
        return records[kwargs["artifact_id"]]

    async def load_story(session_factory, **kwargs):
        return None, story

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "_load_run_and_story", load_story)
    monkeypatch.setattr(media_stage_handlers, "_load_media_artifact", load_artifact)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_quality_check_stage(
        organization_id=organization_id,
        production_run_id=production_run_id,
        production_stage_id=production_stage_id,
        lease_token=lease_token,
        renderer=renderer,
        storage=FakeStorage(
            {
                video_record.storage_key: video_content,
                caption_record.storage_key: captions_content,
            }
        ),
        session_factory=None,
    )

    result = captured["manifest"]
    assert result["publication_blocked"] is True
    assert any(
        flag["reason"] == "attribution_manifest_mismatch"
        for flag in result["rights_flags"]
    )
    assert [credit["artifact_id"] for credit in result["attribution_credits"][:2]] == [
        str(visual_record.id),
        str(audio_record.id),
    ]
    assert result["attribution_credits"][0]["license_id"] == "CC BY 4.0"
    assert result["attribution_credits"][1]["license_id"] == "Apache-2.0"
    assert result["attribution_credits"][2]["source_id"] == str(
        story.source_references[0].source_id
    )



def test_captions_vtt_groups_estimated_words_into_readable_phrases():
    scenes = [
        {"duration_seconds": 2, "narration_segment": "A bright star appears."},
        {"duration_seconds": 2, "narration_segment": "The night sky glows."},
    ]

    captions = media_stage_handlers._captions_vtt(scenes)
    blocks = [block for block in captions.split("\n\n") if "-->" in block]

    assert [block.splitlines()[1] for block in blocks] == [
        "A bright star appears.",
        "The night sky glows.",
    ]


def test_captions_vtt_uses_provider_word_alignment_timestamps():
    scenes = [{"duration_seconds": 2, "narration_segment": "Hello bright world."}]
    timings = (
        TTSWordTiming(start_ms=0, end_ms=350, text="Hello"),
        TTSWordTiming(start_ms=350, end_ms=900, text="bright"),
        TTSWordTiming(start_ms=900, end_ms=1_800, text="world."),
    )

    captions = media_stage_handlers._captions_vtt(scenes, word_timings=timings)

    assert "00:00:00.000 --> 00:00:02.000\nHello bright world." in captions


def test_captions_vtt_maps_provider_timings_to_ffmpeg_rendered_clock():
    """A short source narration is slowed to the target; captions follow it."""
    scenes = [{"duration_seconds": 3, "narration_segment": "Hello bright world."}]
    timings = (
        TTSWordTiming(start_ms=0, end_ms=400, text="Hello"),
        TTSWordTiming(start_ms=400, end_ms=800, text="bright"),
        TTSWordTiming(start_ms=800, end_ms=2_000, text="world."),
    )

    captions = media_stage_handlers._captions_vtt(
        scenes,
        word_timings=timings,
        audio_duration_ms=2_000,
        target_duration_ms=3_000,
    )

    # FFmpeg uses atempo=0.666667, so every source timestamp is multiplied
    # by 1.5 on the rendered audio clock. The cue must end at the source
    # speech end (3s), without a progressive caption delay.
    assert "00:00:00.000 --> 00:00:03.000\nHello bright world." in captions


def test_captions_vtt_keeps_trailing_source_silence_as_an_invisible_tail():
    scenes = [{"duration_seconds": 3, "narration_segment": "Hello world."}]
    timings = (
        TTSWordTiming(start_ms=0, end_ms=500, text="Hello"),
        TTSWordTiming(start_ms=500, end_ms=1_000, text="world."),
    )

    captions = media_stage_handlers._captions_vtt(
        scenes,
        word_timings=timings,
        audio_duration_ms=2_000,
        target_duration_ms=3_000,
    )

    assert "00:00:00.000 --> 00:00:01.500\nHello world." in captions
    assert "00:00:01.500 --> 00:00:03.000\n\u200b" in captions


def test_captions_vtt_without_alignment_uses_rendered_target_not_source_audio():
    scenes = [
        {"duration_seconds": 15, "narration_segment": "First scene narration."},
        {"duration_seconds": 15, "narration_segment": "Second scene narration."},
    ]

    captions = media_stage_handlers._captions_vtt(
        scenes,
        audio_duration_ms=24_000,
        target_duration_ms=30_000,
    )

    # The renderer slows the 24 s WAV to 30 s. The second scene must still
    # begin at the visual cut, not at 12 s on the unscaled source clock.
    assert "00:00:15.000 -->" in captions
    assert "--> 00:00:30.000" in captions


def test_captions_vtt_rejects_alignment_that_does_not_match_narration():
    scenes = [{"duration_seconds": 2, "narration_segment": "Hello bright world."}]
    timings = (
        TTSWordTiming(start_ms=0, end_ms=350, text="Hello"),
        TTSWordTiming(start_ms=350, end_ms=900, text="dark"),
        TTSWordTiming(start_ms=900, end_ms=1_800, text="world."),
    )

    with pytest.raises(ValueError, match="do not match narration"):
        media_stage_handlers._captions_vtt(scenes, word_timings=timings)


def test_background_music_rights_gate_accepts_cleared_commercial_youtube_license():
    metadata = {
        "rights_status": "cleared",
        "commercial_use": True,
        "derivative_works": True,
        "usage_scope": ["youtube", "commercial"],
        "content_sha256": "a" * 64,
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://music.example.test/track",
        "attribution_text": "Composer — Track — CC BY 4.0",
    }

    assert media_stage_handlers._background_music_rights_clearance_flag(
        artifact_id=uuid4(),
        metadata=metadata,
    ) is None


def test_background_music_rights_gate_rejects_personal_only_or_uncleared_track():
    metadata = {
        "rights_status": "cleared",
        "commercial_use": True,
        "derivative_works": True,
        "usage_scope": ["personal"],
        "content_sha256": "b" * 64,
        "license_id": "CC-BY-4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_url": "https://music.example.test/track",
        "attribution_text": "Composer — Track — CC BY 4.0",
    }

    flag = media_stage_handlers._background_music_rights_clearance_flag(
        artifact_id=uuid4(),
        metadata=metadata,
    )

    assert flag is not None
    assert flag["reason"] == "background_music_rights_not_cleared"


class FakeSyntheticVisualProvider:
    async def generate(
        self,
        *,
        visual_direction,
        narration_segment,
        scene_index,
        visual_style="",
        lighting="cinematic",
    ):
        return VisualAsset(
            content=b"synthetic-visual",
            mime_type="image/png",
            width=540,
            height=960,
            provider_id="mock-ai-visual",
            rights_basis="generated_in_application",
            contains_synthetic_media=True,
        )


@pytest.mark.anyio
async def test_visual_generation_stage_persists_synthetic_media_declaration(monkeypatch):
    stored = []
    captured = {}

    async def load_manifest(*args, **kwargs):
        return {
            "scenes": [
                {
                    "scene_index": 0,
                    "visual_direction": "A realistic generated scene",
                    "narration_segment": "A synthetic visual example.",
                    "duration_seconds": 30,
                }
            ]
        }

    async def store(**kwargs):
        stored.append(kwargs)
        return make_artifact(
            artifact_kind="image",
            content=kwargs["content"],
            mime_type=kwargs["mime_type"],
            width=kwargs["width"],
            height=kwargs["height"],
        )

    async def complete(stage_id, token, *, output_manifest, **kwargs):
        captured["manifest"] = output_manifest
        return True

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)
    monkeypatch.setattr(media_stage_handlers, "store_media_artifact", store)
    monkeypatch.setattr(media_stage_handlers, "complete_production_stage", complete)

    await media_stage_handlers.execute_visual_generation_stage(
        organization_id=uuid4(),
        production_run_id=uuid4(),
        production_stage_id=uuid4(),
        lease_token=uuid4(),
        attempt_count=1,
        provider=FakeSyntheticVisualProvider(),
        storage=FakeStorage({}),
        session_factory=None,
    )

    assert stored[0]["metadata"]["contains_synthetic_media"] is True
    assert captured["manifest"]["scenes"][0]["contains_synthetic_media"] is True


class FakeUndeclaredVisualProvider:
    async def generate(
        self,
        *,
        visual_direction,
        narration_segment,
        scene_index,
        visual_style="",
        lighting="cinematic",
    ):
        return VisualAsset(
            content=b"undeclared-visual",
            mime_type="image/png",
            width=540,
            height=960,
            provider_id="fake-undeclared",
            rights_basis="generated_in_application",
        )


@pytest.mark.anyio
async def test_visual_generation_stage_rejects_missing_synthetic_media_declaration(monkeypatch):
    async def load_manifest(*args, **kwargs):
        return {
            "scenes": [
                {
                    "scene_index": 0,
                    "visual_direction": "An unclassified scene",
                    "narration_segment": "A test narration.",
                    "duration_seconds": 30,
                }
            ]
        }

    monkeypatch.setattr(media_stage_handlers, "_load_stage_manifest", load_manifest)

    with pytest.raises(ValueError, match="must declare its synthetic-media status"):
        await media_stage_handlers.execute_visual_generation_stage(
            organization_id=uuid4(),
            production_run_id=uuid4(),
            production_stage_id=uuid4(),
            lease_token=uuid4(),
            attempt_count=1,
            provider=FakeUndeclaredVisualProvider(),
            storage=FakeStorage({}),
            session_factory=None,
        )
