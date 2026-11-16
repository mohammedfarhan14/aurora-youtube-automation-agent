import asyncio
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, update

from app.database.engine import get_session_factory
from app.main import app
from app.media.ffmpeg import FFmpegVideoRenderer
from app.media.storage import LocalMediaStorage
from app.media.tts import MockToneTTSProvider
from app.media.visuals import LocalAbstractVisualProvider
from app.models.media_artifact import MediaArtifact
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.research_run import ResearchRun
from app.models.story_artifact import StoryArtifactRecord
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.services.production import (
    PRODUCTION_STAGE_PLAN,
    claim_production_stage,
    complete_production_stage,
    create_production_run,
    record_production_stage_failure,
    recover_expired_production_stage_leases,
    renew_production_stage_lease,
    request_production_cancellation,
)
from app.services.story_artifacts import (
    load_latest_story_artifact,
    persist_story_artifact_and_complete_stage,
)
from app.story.contracts import StoryArtifact, StoryBeat, StoryScene
from app.workers import media_stage_handlers, story_stage_handlers

client = TestClient(app)


def create_user_and_organization() -> tuple[str, str, str]:
    email = f"production-worker-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Production Worker Test",
            "password": password,
        },
    )
    assert registration.status_code == 201
    user_id = registration.json()["id"]

    async def verify_user() -> None:
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
            await session.commit()

    asyncio.run(verify_user())
    login = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    organization = client.post(
        "/api/v1/organizations",
        headers={"Authorization": f"Bearer {token}"},
        json={"name": f"Production Worker {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, token, organization.json()["id"]


def seed_research_run(user_id: str, organization_id: str) -> tuple[str, str]:
    async def seed() -> str:
        async with get_session_factory()() as session:
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(user_id),
                topic="Worker lease test",
                status="completed",
            )
            channel = YouTubeChannel(
                organization_id=UUID(organization_id),
                channel_id=f"UC-worker-{uuid4().hex[:16]}",
                title="Production Worker Test Channel",
                encrypted_refresh_token="test-encrypted-refresh-token",
                key_version=1,
                granted_scopes="https://www.googleapis.com/auth/youtube.readonly",
                connected_by_user_id=UUID(user_id),
            )
            session.add_all([run, channel])
            await session.flush()
            session.add(
                YouTubeChannelProfile(
                    organization_id=UUID(organization_id),
                    youtube_channel_id=channel.id,
                    version=1,
                    profile={
                        "niche": "technology",
                        "topic_boundaries": [],
                        "target_audience": "general audience",
                        "language": "en",
                        "tone": "clear",
                        "format": "educational",
                        "prohibited_topics": [],
                        "additional_constraints": [],
                    },
                    created_by_user_id=UUID(user_id),
                )
            )
            await session.commit()
            return str(run.id), str(channel.id)

    return asyncio.run(seed())


def cleanup_user(user_id: str) -> None:
    async def cleanup() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                organization_ids = select(Organization.id).where(
                    Organization.created_by_user_id == user_id
                )
                await session.execute(
                    delete(ProductionRun).where(ProductionRun.organization_id.in_(organization_ids))
                )
                await session.execute(
                    delete(ResearchRun).where(ResearchRun.organization_id.in_(organization_ids))
                )
                await session.execute(
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )
                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )
                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def create_run(user_id: str, organization_id: str) -> str:
    research_run_id, youtube_channel_id = seed_research_run(user_id, organization_id)

    async def create() -> str:
        async with get_session_factory()() as session:
            run = await create_production_run(
                session,
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(user_id),
                research_run_id=UUID(research_run_id),
                youtube_channel_id=UUID(youtube_channel_id),
                idempotency_key="worker-test-run",
            )
            await session.commit()
            return str(run.id)

    return asyncio.run(create())


def test_claim_production_stage_enforces_sequential_stage_dependencies() -> None:
    user_id, _, organization_id = create_user_and_organization()
    create_run(user_id, organization_id)
    factory = get_session_factory()

    try:
        first = asyncio.run(claim_production_stage("worker-one", session_factory=factory))
        second = asyncio.run(claim_production_stage("worker-two", session_factory=factory))
        assert first is not None
        assert first[2]
        assert second is None

        async def inspect() -> None:
            async with factory() as session:
                stage = await session.scalar(
                    select(ProductionStage).where(ProductionStage.id == first[1])
                )
                assert stage is not None
                assert stage.stage_key == PRODUCTION_STAGE_PLAN[0]
                assert stage.status == "running"
                assert stage.attempt_count == 1

        asyncio.run(inspect())
    finally:
        cleanup_user(user_id)


def test_expired_production_stage_lease_is_recovered_and_reclaimable() -> None:
    user_id, _, organization_id = create_user_and_organization()
    create_run(user_id, organization_id)
    factory = get_session_factory()

    try:
        claimed = asyncio.run(claim_production_stage("worker-expired", session_factory=factory))
        assert claimed is not None

        async def expire_lease() -> None:
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ProductionStage)
                        .where(ProductionStage.id == claimed[1])
                        .values(lease_expires_at=(func.clock_timestamp() - timedelta(seconds=10)))
                    )

        asyncio.run(expire_lease())
        recovered = asyncio.run(recover_expired_production_stage_leases(session_factory=factory))
        assert recovered == 1

        async def inspect_retry_schedule() -> None:
            async with factory() as session:
                stage = await session.get(ProductionStage, claimed[1])
                assert stage is not None
                remaining = (stage.next_attempt_at - datetime.now(UTC)).total_seconds()
                assert 0 < remaining <= 5

        asyncio.run(inspect_retry_schedule())
        assert (
            asyncio.run(claim_production_stage("worker-too-early", session_factory=factory)) is None
        )

        async def make_retry_due() -> None:
            async with factory() as session:
                async with session.begin():
                    await session.execute(
                        update(ProductionStage)
                        .where(ProductionStage.id == claimed[1])
                        .values(next_attempt_at=func.clock_timestamp() - timedelta(seconds=1))
                    )

        asyncio.run(make_retry_due())
        reclaimed = asyncio.run(claim_production_stage("worker-retry", session_factory=factory))
        assert reclaimed is not None
        assert reclaimed[1] == claimed[1]
        assert reclaimed[2] != claimed[2]
        assert reclaimed[3] == 2
    finally:
        cleanup_user(user_id)


def test_known_stage_failure_releases_lease_and_requeues_with_backoff() -> None:
    user_id, _, organization_id = create_user_and_organization()
    create_run(user_id, organization_id)
    factory = get_session_factory()

    try:
        claimed = asyncio.run(
            claim_production_stage("worker-known-failure", session_factory=factory)
        )
        assert claimed is not None
        _, stage_id, lease_token, _ = claimed

        released = asyncio.run(
            record_production_stage_failure(
                stage_id,
                UUID(lease_token),
                max_attempts=5,
                failure_code="story_stage_execution_failed",
                failure_message=(
                    "Story stage execution failed; retry or operator review is required."
                ),
                session_factory=factory,
            )
        )

        assert released is True

        async def inspect_retry() -> None:
            async with factory() as session:
                stage = await session.get(ProductionStage, stage_id)
                assert stage is not None
                assert stage.status == "pending"
                assert stage.lease_token is None
                assert stage.lease_expires_at is None
                assert stage.failure_code == "story_stage_execution_failed"
                assert stage.next_attempt_at > datetime.now(UTC)

        asyncio.run(inspect_retry())
    finally:
        cleanup_user(user_id)


def test_stage_completion_requires_live_lease_and_advances_only_next_stage() -> None:
    user_id, _, organization_id = create_user_and_organization()
    create_run(user_id, organization_id)
    factory = get_session_factory()

    try:
        claimed = asyncio.run(claim_production_stage("worker-complete", session_factory=factory))
        assert claimed is not None
        run_id, stage_id, token, _ = claimed

        renewed = asyncio.run(
            renew_production_stage_lease(
                stage_id,
                UUID(token),
                session_factory=factory,
            )
        )
        assert renewed is True

        completed = asyncio.run(
            complete_production_stage(
                stage_id,
                UUID(token),
                output_manifest={"artifact_version": 1},
                provider_id="test-provider",
                model_id="test-model",
                prompt_version="story.v1",
                request_id="request-test",
                session_factory=factory,
            )
        )
        assert completed is True

        next_stage = asyncio.run(claim_production_stage("worker-next", session_factory=factory))
        assert next_stage is not None
        assert next_stage[0] == run_id
        assert next_stage[1] != stage_id

        async def inspect_provenance() -> None:
            async with factory() as session:
                stage = await session.scalar(
                    select(ProductionStage).where(ProductionStage.id == stage_id)
                )
                run = await session.get(ProductionRun, run_id)
                assert stage is not None
                assert stage.status == "completed"
                assert stage.output_manifest == {"artifact_version": 1}
                assert stage.provider_id == "test-provider"
                assert stage.model_id == "test-model"
                assert stage.prompt_version == "story.v1"
                assert stage.request_id == "request-test"
                assert run is not None
                assert run.status == "planning"

        asyncio.run(inspect_provenance())
    finally:
        cleanup_user(user_id)


def test_story_artifact_is_versioned_hashed_and_committed_with_stage() -> None:
    user_id, _, organization_id = create_user_and_organization()
    create_run(user_id, organization_id)
    factory = get_session_factory()

    artifact = StoryArtifact(
        title="A short science story",
        hook="What makes this possible?",
        beats=(
            StoryBeat(heading="The question", summary="Introduce the central question."),
            StoryBeat(heading="The answer", summary="Explain the evidence-backed answer."),
        ),
        narration="A concise explanation with enough detail to be useful.",
        visual_direction="Clean educational animation with readable labels.",
        scenes=(
            StoryScene(
                visual_direction="Open on the central question.",
                narration_segment="A concise explanation",
                duration_seconds=15,
            ),
            StoryScene(
                visual_direction="Reveal the key explanation.",
                narration_segment="with enough detail to be useful.",
                duration_seconds=15,
            ),
        ),
    )

    try:
        claimed = asyncio.run(
            claim_production_stage("worker-story-artifact", session_factory=factory)
        )
        assert claimed is not None
        run_id, stage_id, token, _ = claimed

        record = asyncio.run(
            persist_story_artifact_and_complete_stage(
                organization_id=UUID(organization_id),
                production_run_id=run_id,
                production_stage_id=stage_id,
                lease_token=UUID(token),
                artifact=artifact,
                artifact_type="story_draft",
                inference_provenance={
                    "provider": "test-provider",
                    "model": "test-model",
                    "prompt_version": "story-generation.test",
                    "request_id": "story-request-1",
                },
                session_factory=factory,
            )
        )
        assert record is not None
        assert record.version == 1
        assert len(record.content_hash) == 64
        assert record.payload["title"] == artifact.title

        async def inspect() -> None:
            async with factory() as session:
                stored = await session.get(StoryArtifactRecord, record.id)
                stage = await session.get(ProductionStage, stage_id)
                loaded = await load_latest_story_artifact(
                    session,
                    organization_id=UUID(organization_id),
                    production_run_id=run_id,
                    artifact_type="story_draft",
                )
                assert stored is not None
                assert stored.content_hash == record.content_hash
                assert stage is not None
                assert stage.status == "completed"
                assert stage.provider_id == "test-provider"
                assert stage.model_id == "test-model"
                assert stage.output_manifest["artifact_id"] == str(record.id)
                assert loaded == artifact

        asyncio.run(inspect())
    finally:
        cleanup_user(user_id)


def test_story_worker_renews_lease_until_execution_stops(monkeypatch) -> None:
    stop = asyncio.Event()
    lost = asyncio.Event()
    renewals: list[tuple[UUID, UUID, int]] = []

    async def renew(
        stage_id: UUID,
        lease_token: UUID,
        *,
        lease_seconds: int,
        session_factory,
    ) -> bool:
        renewals.append((stage_id, lease_token, lease_seconds))
        stop.set()
        return True

    monkeypatch.setattr(story_stage_handlers, "renew_production_stage_lease", renew)

    async def maintain() -> None:
        await story_stage_handlers._maintain_stage_lease(
            uuid4(),
            uuid4(),
            lease_seconds=3,
            stop=stop,
            lost=lost,
            session_factory=get_session_factory(),
        )

    asyncio.run(maintain())
    assert len(renewals) == 1
    assert renewals[0][2] == 3
    assert lost.is_set() is False


def test_queued_cancellation_rolls_back_run_and_stages_atomically() -> None:
    user_id, _, organization_id = create_user_and_organization()
    run_id = create_run(user_id, organization_id)
    factory = get_session_factory()

    try:

        async def cancel_then_rollback() -> None:
            try:
                async with factory() as session:
                    async with session.begin():
                        run = await request_production_cancellation(
                            session,
                            organization_id=UUID(organization_id),
                            production_run_id=UUID(run_id),
                        )
                        assert run is not None
                        assert run.status == "cancelled"
                        stages = await session.scalars(
                            select(ProductionStage).where(
                                ProductionStage.production_run_id == UUID(run_id)
                            )
                        )
                        assert all(stage.status == "cancelled" for stage in stages)
                        raise RuntimeError("simulate transaction rollback")
            except RuntimeError as exc:
                assert str(exc) == "simulate transaction rollback"

        asyncio.run(cancel_then_rollback())

        async def inspect_rollback() -> None:
            async with factory() as session:
                run = await session.get(ProductionRun, UUID(run_id))
                assert run is not None
                assert run.status == "queued"
                stages = await session.scalars(
                    select(ProductionStage).where(ProductionStage.production_run_id == UUID(run_id))
                )
                assert all(stage.status == "pending" for stage in stages)

        asyncio.run(inspect_rollback())
    finally:
        cleanup_user(user_id)


def test_cancelling_running_stage_expires_lease_for_prompt_recovery() -> None:
    user_id, _, organization_id = create_user_and_organization()
    run_id = create_run(user_id, organization_id)
    factory = get_session_factory()

    try:

        async def claim_and_cancel() -> None:
            claimed = await claim_production_stage(
                "test-worker",
                lease_seconds=900,
                session_factory=factory,
            )
            assert claimed is not None
            async with factory() as session:
                async with session.begin():
                    run = await request_production_cancellation(
                        session,
                        organization_id=UUID(organization_id),
                        production_run_id=UUID(run_id),
                    )
                    assert run is not None
                    assert run.cancel_requested_at is not None
                    stage = await session.get(ProductionStage, claimed[1])
                    assert stage is not None
                    database_now = await session.scalar(select(func.clock_timestamp()))
                    assert stage.status == "running"
                    assert stage.lease_expires_at is not None
                    assert stage.lease_expires_at <= database_now

        asyncio.run(claim_and_cancel())
    finally:
        cleanup_user(user_id)


def test_quality_check_completion_atomically_enters_awaiting_review() -> None:
    user_id, _, organization_id = create_user_and_organization()
    run_id = create_run(user_id, organization_id)
    factory = get_session_factory()

    try:

        async def prepare_quality_check() -> None:
            async with factory() as session:
                async with session.begin():
                    run = await session.get(ProductionRun, run_id)
                    assert run is not None
                    run.status = "generating"
                    stages = await session.scalars(
                        select(ProductionStage).where(
                            ProductionStage.production_run_id == UUID(run_id)
                        )
                    )
                    for stage in stages:
                        if stage.stage_key in PRODUCTION_STAGE_PLAN[:6]:
                            stage.status = "completed"

        asyncio.run(prepare_quality_check())
        claimed = asyncio.run(
            claim_production_stage(
                "quality-check-worker",
                stage_keys=["quality_check"],
                session_factory=factory,
            )
        )
        assert claimed is not None
        claimed_run_id, stage_id, token, _ = claimed
        assert claimed_run_id == UUID(run_id)

        completed = asyncio.run(
            complete_production_stage(
                stage_id,
                UUID(token),
                output_manifest={"passed": True},
                session_factory=factory,
            )
        )
        assert completed is True

        async def inspect_review_transition() -> None:
            async with factory() as session:
                run = await session.get(ProductionRun, UUID(run_id))
                assert run is not None
                assert run.status == "awaiting_review"
                assert run.completed_at is not None
                stages = await session.scalars(
                    select(ProductionStage).where(ProductionStage.production_run_id == UUID(run_id))
                )
                by_key = {stage.stage_key: stage for stage in stages}
                assert by_key["quality_check"].status == "completed"
                assert by_key["awaiting_review"].status == "completed"
                assert by_key["awaiting_review"].output_manifest == {
                    "transition": "awaiting_review"
                }

        asyncio.run(inspect_review_transition())
    finally:
        cleanup_user(user_id)


def test_media_worker_executes_real_30_second_pipeline_and_persists_reviewable_video(
    tmp_path,
) -> None:
    """Exercise durable media stages against PostgreSQL and a real FFmpeg build."""
    import pytest

    ffmpeg_path = shutil.which("ffmpeg")
    ffprobe_path = shutil.which("ffprobe")
    if ffmpeg_path is None or ffprobe_path is None:
        pytest.skip("FFmpeg and ffprobe are required for the media worker integration test.")

    encoders = subprocess.run(
        [ffmpeg_path, "-hide_banner", "-encoders"],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    if "libx264" not in encoders:
        pytest.skip("The installed FFmpeg build does not include libx264.")

    user_id, _, organization_id = create_user_and_organization()
    production_run_id = create_run(user_id, organization_id)
    factory = get_session_factory()
    organization_uuid = UUID(organization_id)
    run_uuid = UUID(production_run_id)
    narrative = (
        "Scientists studying planets noticed a tiny wobble in a star's light. "
        "Repeated observations revealed a steady pattern. The star moved because "
        "an unseen planet pulled on it. By measuring that motion, astronomers "
        "estimated the planet's orbit and mass. This method helps researchers "
        "discover worlds too faint to see directly. Measurements can reveal hidden "
        "objects across enormous distances. Independent checks help distinguish "
        "a real discovery from noise."
    )
    words = narrative.split()
    assert len(words) == 66
    story = StoryArtifact(
        title="How astronomers find hidden planets",
        hook="A star can reveal a planet it cannot show us",
        beats=(
            StoryBeat(
                heading="The signal",
                summary="A repeating wobble reveals an unseen companion.",
            ),
            StoryBeat(
                heading="The measurement",
                summary="Astronomers infer the hidden planet from the star's motion.",
            ),
        ),
        narration=narrative,
        visual_direction="Original abstract space-science visuals.",
        scenes=(
            StoryScene(
                visual_direction="A distant star with a subtle repeating light pulse.",
                narration_segment=" ".join(words[:33]),
                duration_seconds=15,
            ),
            StoryScene(
                visual_direction="A planet orbiting a luminous star.",
                narration_segment=" ".join(words[33:]),
                duration_seconds=15,
            ),
        ),
        target_duration_seconds=30,
        word_budget=70,
    )
    storage = LocalMediaStorage(tmp_path / "media", max_object_bytes=20_000_000)
    renderer = FFmpegVideoRenderer(
        ffmpeg_path=ffmpeg_path,
        ffprobe_path=ffprobe_path,
        video_encoder="libx264",
        video_preset="ultrafast",
        video_crf=30,
        timeout_seconds=120,
        max_output_bytes=20_000_000,
    )
    tts_provider = MockToneTTSProvider()
    visual_provider = LocalAbstractVisualProvider(width=360, height=640)

    async def execute_pipeline() -> None:
        for artifact_type in ("story_draft", "story_verified"):
            claimed = await claim_production_stage(
                f"media-e2e-{artifact_type}",
                session_factory=factory,
            )
            assert claimed is not None
            claimed_run_id, stage_id, lease_token, _ = claimed
            assert claimed_run_id == run_uuid
            record = await persist_story_artifact_and_complete_stage(
                organization_id=organization_uuid,
                production_run_id=run_uuid,
                production_stage_id=stage_id,
                lease_token=UUID(lease_token),
                artifact=story,
                artifact_type=artifact_type,
                inference_provenance={
                    "provider": "integration-test",
                    "model": "deterministic-story-fixture",
                    "prompt_version": "media-e2e.test",
                },
                session_factory=factory,
            )
            assert record is not None

        for stage_key in media_stage_handlers.MEDIA_STAGE_KEYS:
            processed = await media_stage_handlers.run_media_stage_once(
                f"media-e2e-{stage_key}",
                tts_provider=tts_provider,
                tts_voice="synthetic-test-tone",
                # This synthetic tone is not speech and is intentionally not
                # rights-cleared; QA must keep the run blocked from publishing.
                tts_rights_basis="unknown",
                tts_license_id=None,
                tts_license_uri=None,
                tts_source_uri=None,
                tts_attribution=None,
                visual_provider=visual_provider,
                renderer=renderer,
                storage=storage,
                session_factory=factory,
            )
            assert processed is True

        async with factory() as session:
            run = await session.get(ProductionRun, run_uuid)
            assert run is not None
            assert run.status == "awaiting_review"
            assert run.approved_at is None

            stages = list(
                (
                    await session.scalars(
                        select(ProductionStage).where(ProductionStage.production_run_id == run_uuid)
                    )
                ).all()
            )
            assert {stage.stage_key for stage in stages if stage.status == "completed"} == set(
                PRODUCTION_STAGE_PLAN
            )
            quality_stage = next(stage for stage in stages if stage.stage_key == "quality_check")
            assert quality_stage.output_manifest["publication_blocked"] is True
            assert quality_stage.output_manifest["rights_flags"]

            artifacts = list(
                (
                    await session.scalars(
                        select(MediaArtifact).where(
                            MediaArtifact.organization_id == organization_uuid,
                            MediaArtifact.production_run_id == run_uuid,
                        )
                    )
                ).all()
            )
            video = next(item for item in artifacts if item.artifact_kind == "video")
            assert video.mime_type == "video/mp4"
            assert video.duration_ms is not None
            assert abs(video.duration_ms - 30_000) <= 750
            assert (video.width, video.height) == (1080, 1920)
            assert video.storage_backend == "local"
            assert video.sha256 and len(video.sha256) == 64
            video_bytes = await storage.get(video.storage_key)
            assert video_bytes[4:8] == b"ftyp"
            report = await renderer.inspect(video_bytes)
            assert report.is_valid is True
            assert report.has_audio is True
            assert report.has_captions is True

            captions = next(item for item in artifacts if item.artifact_kind == "captions")
            assert (await storage.get(captions.storage_key)).startswith(b"WEBVTT")

    try:
        asyncio.run(execute_pipeline())
    finally:
        asyncio.run(storage.aclose())
        asyncio.run(tts_provider.aclose())
        cleanup_user(user_id)
