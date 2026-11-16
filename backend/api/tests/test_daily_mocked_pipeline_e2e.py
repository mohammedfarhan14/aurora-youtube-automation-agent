"""End-to-end PostgreSQL integration test for a consent-gated daily short."""

import asyncio
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select

from app.database.engine import get_session_factory
from app.models.evidence_item import EvidenceItem
from app.models.media_artifact import MediaArtifact
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.story_artifact import StoryArtifactRecord
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.models.youtube_publishing import (
    YouTubePublishEvent,
    YouTubePublishingConsentEvent,
    YouTubePublishJob,
)
from app.services.production import PRODUCTION_STAGE_PLAN, create_production_run

NARRATION = (
    "Scientists studying planets noticed a tiny wobble in a star's light. "
    "Repeated observations revealed a steady pattern. The star moved because "
    "an unseen planet pulled on it. By measuring that motion, astronomers "
    "estimated the planet's orbit and mass. This method helps researchers "
    "discover worlds too faint to see directly. Measurements can reveal hidden "
    "objects across enormous distances. Independent checks help distinguish "
    "a real discovery from noise."
)
CC0_URI = "https://creativecommons.org/publicdomain/zero/1.0/"


class _ResearchProvider:
    def __init__(self):
        self.calls = 0

    async def research(self, topic):
        from app.research.contracts import EvidenceExcerpt, ResearchDocument

        self.calls += 1
        assert topic.strip()
        return (
            ResearchDocument(
                canonical_url="https://example.test/astronomy/planet-wobble",
                title="Synthetic astronomy evidence fixture",
                publisher="Aurora E2E Research Fixture",
                source_type="webpage",
                published_at=None,
                retrieved_at=datetime.now(UTC),
                content=NARRATION,
                evidence=(EvidenceExcerpt(excerpt=NARRATION, locator="Fixture excerpt"),),
            ),
        )


class _QueueLLM:
    def __init__(self, *responses):
        self.responses = list(responses)

    async def generate(self, request):
        from app.llm.contracts import LLMResponse, LLMUsage

        assert self.responses
        return LLMResponse(
            content=self.responses.pop(0),
            provider="mock",
            model=request.model,
            finish_reason="stop",
            usage=LLMUsage(input_tokens=100, output_tokens=80, total_tokens=180),
            request_id=f"daily-e2e-{len(self.responses)}",
        )


class _CC0MockTTS:
    @property
    def capability_manifest(self):
        from app.media.providers import TTSCapabilityManifest

        return TTSCapabilityManifest(
            provider_id="daily_e2e_cc0_mock",
            languages=("en",),
            voices=("daily-e2e-voice",),
            sample_rates_hz=(16_000,),
            supports_streaming=False,
            supports_word_timings=False,
            model_license="generated_in_application",
            voice_license="CC0-1.0",
            license_uri=CC0_URI,
            attribution="Synthetic local E2E narration audio, dedicated to CC0 1.0.",
            cost_per_1000_characters_usd=0.0,
            hosting_mode="in_process",
        )

    async def health_check(self):
        return True

    async def synthesize(self, *, text, voice_id, language, speed=1.0, audio_format="wav"):
        from app.media.providers import AudioAsset
        from app.media.tts import MockToneTTSProvider

        audio = await MockToneTTSProvider().synthesize(
            text=text, voice_id=voice_id, language=language, speed=speed, audio_format=audio_format
        )
        return AudioAsset(
            content=audio.content,
            mime_type=audio.mime_type,
            duration_ms=audio.duration_ms,
            sample_rate_hz=audio.sample_rate_hz,
            channels=audio.channels,
            provider_id="daily_e2e_cc0_mock",
            model_id="synthetic-tone-v1",
            voice_id=voice_id,
            request_id="daily-e2e-tts",
        )

    async def aclose(self):
        return None


class _FakeUpload:
    def __init__(self, publish_at, byte_size):
        self.publish_at = publish_at
        self.byte_size = byte_size
        self.initiations = 0
        self.uploads = 0
        self.processing = 0
        self.thumbnails = 0

        def token_response(request):
            assert str(request.url) == "https://oauth2.googleapis.com/token"
            assert b"grant_type=refresh_token" in request.content
            return httpx.Response(
                200,
                json={
                    "access_token": "ephemeral-daily-e2e-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "refresh_token": "rotated-daily-e2e-refresh",
                },
            )

        self.http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(token_response)
        )

    async def initiate_upload(self, **kwargs):
        self.initiations += 1
        assert kwargs["privacy_status"] == "private"
        assert kwargs["total_bytes"] == self.byte_size
        assert kwargs["publish_at"] == self.publish_at.isoformat().replace("+00:00", "Z")
        return (
            "https://www.googleapis.com/upload/youtube/v3/videos"
            "?upload_id=daily-e2e-mock-session"
        )

    async def upload_chunk(self, **kwargs):
        self.uploads += 1
        assert kwargs["start"] == 0
        assert len(kwargs["content"]) == self.byte_size
        from app.services.youtube_upload import UploadChunkResult

        return UploadChunkResult(
            bytes_received=self.byte_size,
            video_id="daily-e2e-mock-video",
        )

    async def query_upload_status(self, **kwargs):
        raise AssertionError("A new mocked upload must not require reconciliation.")

    async def get_video_processing_status(self, *, access_token, video_id):
        self.processing += 1
        assert access_token == "ephemeral-daily-e2e-token"
        assert video_id == "daily-e2e-mock-video"
        from app.services.youtube_upload import YouTubeVideoProcessingStatus

        return YouTubeVideoProcessingStatus(
            upload_status="processed",
            processing_status="succeeded",
            privacy_status="private",
            published_at=None,
        )

    async def upload_thumbnail(self, **kwargs):
        self.thumbnails += 1
        assert kwargs["video_id"] == "daily-e2e-mock-video"
        assert kwargs["mime_type"] == "image/jpeg"
        assert kwargs["processing_status"] == "succeeded"
        assert kwargs["content"].startswith(b"\xff\xd8")
        return {"kind": "youtube#thumbnailSetResponse"}

    async def update_video_privacy_status(self, **kwargs):
        raise AssertionError("A future scheduled video must remain private.")

    async def aclose(self):
        await self.http_client.aclose()


def test_cleanup_script_is_valid_bash_and_safe_by_default():
    repository_root = Path(__file__).resolve().parents[3]
    script = repository_root / "scripts" / "cleanup-branches.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    contents = script.read_text(encoding="utf-8")
    assert "EXECUTE=0" in contents
    assert "feat/creative-identity-registry" in contents
    assert "archive/branch-cleanup/" in contents


def test_daily_mocked_pipeline_is_traceable_idempotent_and_scheduled(tmp_path, monkeypatch):
    """Discover, research, verify, render, gate, enqueue and mock-upload one daily short."""
    from app.core.config import Settings
    from app.media.ffmpeg import FFmpegVideoRenderer
    from app.media.storage import LocalMediaStorage
    from app.media.visuals import LocalAbstractVisualProvider
    from app.research.topic_discovery import select_research_topic
    from app.services.youtube_crypto import encrypt_refresh_token
    from app.services.youtube_publishing import (
        claim_next_youtube_publish_job,
        create_youtube_publish_job,
    )
    from app.workers import media_stage_handlers
    from app.workers.research_worker import claim_research_batch, process_research_run
    from app.workers.story_stage_handlers import run_story_stage_once
    from app.workers.youtube_publish_worker import process_claimed_youtube_publish_job

    fixtures = __import__("test_production_worker")
    client = fixtures.client
    cleanup_user = fixtures.cleanup_user
    create_user_and_organization = fixtures.create_user_and_organization

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if not ffmpeg_path or not ffprobe_path:
        pytest.skip("FFmpeg and ffprobe are required for the daily pipeline integration test.")
    encoders = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    if "libx264" not in encoders:
        pytest.skip("The installed FFmpeg build does not include libx264.")

    user_id, token, organization_id = create_user_and_organization()
    user_uuid, org_uuid = UUID(user_id), UUID(organization_id)
    factory = get_session_factory()
    token_key = "42" * 32
    external_channel_id = f"UC-daily-e2e-{uuid4().hex[:16]}"
    profile = {
        "niche": "space science",
        "topic_boundaries": ["astronomy", "space exploration"],
        "target_audience": "curious adults",
        "language": "en",
        "tone": "engaging",
        "format": "educational",
        "prohibited_topics": [],
        "additional_constraints": [],
    }
    publish_at = datetime.now(UTC) + timedelta(hours=2)
    storage = None
    tts = None

    async def seed_channel():
        async with factory() as session:
            workspace = await session.get(Organization, org_uuid)
            assert workspace is not None
            workspace.studio_settings = {
                "research_enabled": True,
                "video_generation_enabled": True,
                "automatic_publishing_enabled": True,
                "target_duration_seconds": 30,
                "language": "English",
                "tone": "Clear, engaging, documentary",
                "audience": "Curious general audience",
                "additional_rules": [],
            }
            channel = YouTubeChannel(
                organization_id=org_uuid,
                channel_id=external_channel_id,
                title="Aurora Daily E2E Channel",
                encrypted_refresh_token=encrypt_refresh_token(
                    "synthetic-refresh-token", token_key, org_uuid, external_channel_id
                ),
                key_version=1,
                granted_scopes=(
                    "https://www.googleapis.com/auth/youtube.readonly "
                    "https://www.googleapis.com/auth/youtube.upload "
                    "https://www.googleapis.com/auth/youtube.force-ssl"
                ),
                connected_by_user_id=user_uuid,
            )
            session.add(channel)
            await session.flush()
            channel_profile = YouTubeChannelProfile(
                organization_id=org_uuid,
                youtube_channel_id=channel.id,
                version=1,
                profile=profile,
                created_by_user_id=user_uuid,
            )
            consent = YouTubePublishingConsentEvent(
                id=uuid4(),
                organization_id=org_uuid,
                youtube_channel_id=channel.id,
                actor_user_id=user_uuid,
                action="enabled",
                reason="explicit_channel_owner_consent",
                granted_scopes_snapshot=channel.granted_scopes,
            )
            session.add_all([channel_profile, consent])
            await session.flush()
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            channel.auto_publish_enabled = True
            channel.auto_publish_privacy_status = "public"
            channel.auto_publish_consent_granted_by_user_id = user_uuid
            channel.auto_publish_consent_granted_at = now
            channel.auto_publish_consent_event_id = consent.id
            await session.commit()
            return channel.id, channel_profile.id

    try:
        channel_id, profile_id = asyncio.run(seed_channel())
        topic = select_research_topic(profile)
        assert topic
        created = client.post(
            f"/api/v1/organizations/{organization_id}/research-runs",
            headers={"Authorization": f"Bearer {token}"},
            json={"topic": topic},
        )
        assert created.status_code == 201
        research_id = UUID(created.json()["id"])

        async def bind_research():
            async with factory() as session:
                run = await session.get(ResearchRun, research_id)
                run.youtube_channel_id = channel_id
                run.channel_profile_id = profile_id
                run.channel_profile_version = 1
                run.channel_profile_snapshot = profile
                run.scheduled_local_date = publish_at.date()
                run.scheduled_for = publish_at
                await session.commit()

        asyncio.run(bind_research())
        research_settings = Settings(
            _env_file=None,
            research_worker_lease_seconds=120,
            research_worker_heartbeat_interval_seconds=5,
            research_worker_provider_timeout_seconds=30,
        )
        provider = _ResearchProvider()

        async def research_once():
            claimed = await claim_research_batch(
                "daily-e2e-research", settings=research_settings, session_factory=factory
            )
            assert len(claimed) == 1 and claimed[0].id == research_id
            assert await process_research_run(
                claimed[0], provider, settings=research_settings, session_factory=factory
            )

        asyncio.run(research_once())
        assert provider.calls == 1

        async def source_and_evidence():
            async with factory() as session:
                source = await session.scalar(
                    select(ResearchSource).where(ResearchSource.research_run_id == research_id)
                )
                assert source is not None
                evidence = await session.scalar(
                    select(EvidenceItem).where(EvidenceItem.research_source_id == source.id)
                )
                assert evidence is not None
                return source.id, evidence.id

        source_id, evidence_id = asyncio.run(source_and_evidence())
        review = client.post(
            (
                f"/api/v1/organizations/{organization_id}/research-runs/{research_id}"
                f"/sources/{source_id}/rights-reviews"
            ),
            headers={"Authorization": f"Bearer {token}"},
            json={
                "decision": "cleared",
                "scope": "source_text",
                "license_ids": ["CC0-1.0"],
                "license_urls": [CC0_URI],
                "rights_holder": "Aurora synthetic E2E fixture",
                "creators": ["Aurora Studio E2E"],
                "attribution_text": "Synthetic research fixture, CC0 1.0.",
                "reuse_restrictions": ["attribution"],
                "review_notes": "Explicit owner review of synthetic test evidence.",
                "evidence_urls": [CC0_URI],
            },
        )
        assert review.status_code == 201
        assert review.json()["review"]["decision"] == "cleared"

        async def make_production_run():
            async with factory() as session:
                run = await create_production_run(
                    session,
                    organization_id=org_uuid,
                    requested_by_user_id=user_uuid,
                    research_run_id=research_id,
                    youtube_channel_id=channel_id,
                    idempotency_key=f"daily-e2e:{research_id}",
                )
                await session.commit()
                return run.id

        production_id = asyncio.run(make_production_run())
        words = NARRATION.split()
        assert len(words) == 66
        draft = json.dumps(
            {
                "title": "How astronomers find hidden planets",
                "hook": "A star can reveal a planet it cannot show us.",
                "beats": [
                    {
                        "heading": "The signal",
                        "summary": "Repeated observations reveal a stellar wobble.",
                    },
                    {
                        "heading": "The measurement",
                        "summary": "Astronomers infer a planet from stellar motion.",
                    },
                ],
                "narration": NARRATION,
                "visual_direction": "Original abstract space-science visuals.",
                "scenes": [
                    {
                        "visual_direction": "A distant star with a subtle repeating light pulse.",
                        "narration_segment": " ".join(words[:33]),
                        "duration_seconds": 15,
                    },
                    {
                        "visual_direction": "A planet orbiting a luminous star.",
                        "narration_segment": " ".join(words[33:]),
                        "duration_seconds": 15,
                    },
                ],
            }
        )
        claims = [
            "Repeated observations revealed a steady pattern in a star's light.",
            "Astronomers estimated the planet's orbit and mass by measuring the star's motion.",
            "Independent checks help distinguish a real discovery from noise.",
        ]
        extraction = json.dumps({"claims": [{"claim": claim} for claim in claims]})
        verification = json.dumps(
            {
                "assessments": [
                    {
                        "claim_index": i,
                        "evidence": [
                            {
                                "source_id": str(source_id),
                                "evidence_id": str(evidence_id),
                                "relation": "supports",
                            }
                        ],
                    }
                    for i in range(len(claims))
                ]
            }
        )
        llm = _QueueLLM(draft, extraction, verification)
        storage = LocalMediaStorage(tmp_path / "daily-e2e-media", max_object_bytes=20_000_000)
        tts = _CC0MockTTS()
        visual = LocalAbstractVisualProvider(width=360, height=640)
        renderer = FFmpegVideoRenderer(
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            video_encoder="libx264",
            video_preset="ultrafast",
            video_crf=30,
            timeout_seconds=120,
            max_output_bytes=20_000_000,
        )
        worker_settings = Settings(
            _env_file=None,
            youtube_publish_worker_enabled=True,
            youtube_client_id="daily-e2e-client",
            youtube_client_secret="daily-e2e-client-secret",
            youtube_egress_proxy_url="http://127.0.0.1:3128",
            integration_token_encryption_key=token_key,
            integration_token_active_key_version=1,
            youtube_publish_poll_interval_seconds=1,
        )
        monkeypatch.setattr("app.core.config.get_settings", lambda: worker_settings)

        async def produce_and_upload():
            assert await run_story_stage_once(
                "daily-e2e-story",
                provider=llm,
                model="daily-e2e-mock",
                verification_model="daily-e2e-mock",
                session_factory=factory,
            )
            assert await run_story_stage_once(
                "daily-e2e-verifier",
                provider=llm,
                model="daily-e2e-mock",
                verification_model="daily-e2e-mock",
                session_factory=factory,
            )
            assert not llm.responses

            for stage_key in media_stage_handlers.MEDIA_STAGE_KEYS:
                assert await media_stage_handlers.run_media_stage_once(
                    f"daily-e2e-{stage_key}",
                    tts_provider=tts,
                    tts_voice="daily-e2e-voice",
                    tts_rights_basis="cc0",
                    tts_license_id="CC0-1.0",
                    tts_license_uri=CC0_URI,
                    tts_source_uri="https://example.test/audio/daily-e2e",
                    tts_attribution="Synthetic local E2E narration audio, CC0 1.0.",
                    visual_provider=visual,
                    renderer=renderer,
                    storage=storage,
                    session_factory=factory,
                ), f"Media stage did not run: {stage_key}"

            async with factory() as session:
                run = await session.get(ProductionRun, production_id)
                assert run is not None
                assert run.status == "approved"
                assert run.approval_mode == "automatic_consent"
                assert run.scheduled_for == publish_at
                stages = list(
                    (await session.scalars(
                        select(ProductionStage).where(
                            ProductionStage.production_run_id == production_id
                        )
                    )).all()
                )
                assert {
                    stage.stage_key for stage in stages if stage.status == "completed"
                } == set(PRODUCTION_STAGE_PLAN)
                quality = next(s for s in stages if s.stage_key == "quality_check")
                assert quality.output_manifest["publication_blocked"] is False
                assert quality.output_manifest["claim_flags"] == []
                assert quality.output_manifest["rights_flags"] == []
                assert quality.output_manifest["quality_report"]["is_valid"] is True

                stories = list(
                    (await session.scalars(
                        select(StoryArtifactRecord).where(
                            StoryArtifactRecord.production_run_id == production_id
                        )
                    )).all()
                )
                assert {s.artifact_type for s in stories} == {"story_draft", "story_verified"}
                verified = next(s for s in stories if s.artifact_type == "story_verified")
                assert all(
                    claim["verification_status"] == "supported"
                    for claim in verified.payload["factual_claims"]
                )
                assert verified.payload["source_references"][0]["source_id"] == str(source_id)

                artifacts = list(
                    (await session.scalars(
                        select(MediaArtifact).where(
                            MediaArtifact.production_run_id == production_id
                        )
                    )).all()
                )
                kinds = {a.artifact_kind for a in artifacts}
                assert {"audio", "image", "video", "captions"}.issubset(kinds)
                for artifact in artifacts:
                    assert artifact.sha256 and len(artifact.sha256) == 64
                video = next(a for a in artifacts if a.artifact_kind == "video")
                assert video.mime_type == "video/mp4"
                assert abs((video.duration_ms or 0) - 30_000) <= 750
                video_bytes = await storage.get(video.storage_key)
                assert video_bytes[4:8] == b"ftyp"

                job = await session.scalar(
                    select(YouTubePublishJob).where(
                        YouTubePublishJob.production_run_id == production_id
                    )
                )
                assert job is not None and job.status == "queued"
                assert job.approval_mode == "automatic_consent"
                assert job.privacy_status == "private"
                assert job.publish_at == publish_at
                assert job.metadata_snapshot["auto_publish_privacy_status"] == "public"
                assert (
                    job.metadata_snapshot["production"]["production_run_id"]
                    == str(production_id)
                )
                assert job.metadata_snapshot["production"]["research_run_id"] == str(research_id)
                assert (
                    job.metadata_snapshot["editorial_profile"]["channel_profile_id"]
                    == str(profile_id)
                )
                assert job.metadata_snapshot["media_artifact"]["artifact_id"] == str(video.id)
                assert job.metadata_snapshot["media_artifact"]["sha256"] == video.sha256
                job_id = job.id

            # Reconstruct the exact original request from the immutable production
            # snapshot rather than the database-normalized publishing row.
            async with factory() as session:
                run = await session.get(ProductionRun, production_id)
                story = await session.scalar(
                    select(StoryArtifactRecord).where(
                        StoryArtifactRecord.production_run_id == production_id,
                        StoryArtifactRecord.artifact_type == "story_verified",
                    )
                )
                assert run is not None and story is not None
                profile_snapshot = run.channel_profile_snapshot
                replay_tags = [
                    profile_snapshot["niche"],
                    *profile_snapshot["topic_boundaries"],
                ]
                replay_publish_at = (
                    run.scheduled_for
                    if run.scheduled_for is not None
                    and run.approved_at is not None
                    and run.scheduled_for > run.approved_at
                    else None
                )
                replay = await create_youtube_publish_job(
                    session,
                    organization_id=org_uuid,
                    production_run_id=production_id,
                    requested_by_user_id=user_uuid,
                    idempotency_key=f"auto-consent:{production_id}",
                    title=story.payload["title"].strip(),
                    description=story.payload["hook"].strip(),
                    tags=replay_tags,
                    privacy_status="private" if replay_publish_at else "public",
                    publish_at=replay_publish_at,
                )
                await session.commit()
                assert replay.id == job_id

            async with factory() as session:
                count = await session.scalar(
                    select(func.count()).select_from(YouTubePublishJob).where(
                        YouTubePublishJob.production_run_id == production_id
                    )
                )
            assert count == 1
            claim = await claim_next_youtube_publish_job(
                factory, worker_id="daily-e2e-upload", lease_seconds=300
            )
            assert claim is not None and claim[0] == job_id
            upload = _FakeUpload(publish_at, len(video_bytes))
            try:
                await process_claimed_youtube_publish_job(
                    job_id=claim[0],
                    lease_token=claim[1],
                    worker_id="daily-e2e-upload",
                    session_factory=factory,
                    storage=storage,
                    upload_client=upload,
                    settings=worker_settings,
                )
            finally:
                await upload.aclose()
            assert (
                upload.initiations,
                upload.uploads,
                upload.processing,
                upload.thumbnails,
            ) == (1, 1, 1, 1)

            async with factory() as session:
                job = await session.get(YouTubePublishJob, job_id)
                assert job is not None
                assert job.status == "uploaded"
                assert job.youtube_video_id == "daily-e2e-mock-video"
                assert job.bytes_uploaded == job.byte_size
                assert job.upload_session_url_encrypted is None
                assert job.privacy_status == "private"
                assert job.publish_at == publish_at
                assert job.published_at is None
                assert job.youtube_processing_status == "succeeded"
                events = list(
                    (await session.scalars(
                        select(YouTubePublishEvent).where(
                            YouTubePublishEvent.publish_job_id == job_id
                        )
                    )).all()
                )
                assert {
                    "publish_intent_created",
                    "upload_initiation_started",
                    "upload_session_created",
                    "upload_completed",
                    "video_scheduled_private",
                }.issubset({event.event_type for event in events})
                assert await session.scalar(
                    select(func.count()).select_from(YouTubePublishJob).where(
                        YouTubePublishJob.production_run_id == production_id
                    )
                ) == 1

        try:
            asyncio.run(produce_and_upload())
        finally:
            asyncio.run(storage.aclose())
            asyncio.run(tts.aclose())
    finally:
        cleanup_user(user_id)
