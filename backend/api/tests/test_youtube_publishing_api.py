import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.main import app
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
from app.models.youtube_publishing import (
    YouTubePublishEvent,
    YouTubePublishingConsentEvent,
    YouTubePublishJob,
)
from app.services.production import approve_production_run, complete_production_stage
from app.services.youtube_crypto import decrypt_refresh_token, encrypt_refresh_token
from app.services.youtube_publish_validation import validate_youtube_publish_job_snapshot
from app.services.youtube_publishing import (
    YouTubePublishLeaseLostError,
    claim_next_youtube_publish_job,
    create_youtube_publish_job,
    update_youtube_publish_job_under_lease,
)
from app.services.youtube_tokens import YouTubeReauthorizationRequired
from app.services.youtube_upload import (
    UploadChunkResult,
    YouTubeUploadPermanentError,
    YouTubeVideoProcessingStatus,
)
from app.workers import youtube_publish_worker
from app.workers.youtube_publish_worker import process_claimed_youtube_publish_job

client = TestClient(app)
YOUTUBE_READ_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_VIDEO_WRITE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


def create_user_and_organization() -> tuple[str, str, str]:
    email = f"youtube-publishing-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "YouTube Publishing Test",
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
        json={"name": f"YouTube Publishing {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, token, organization.json()["id"]


def create_verified_user() -> str:
    email = f"youtube-connector-{uuid4().hex}@example.com"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Channel Connector",
            "password": "Aurora-Secure-Password-2026",
        },
    )
    assert registration.status_code == 201
    user_id = registration.json()["id"]

    async def verify() -> None:
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.email_verified_at = datetime.now(UTC)
            await session.commit()

    asyncio.run(verify())
    return user_id


def seed_approved_video(
    user_id: str,
    organization_id: str,
    *,
    upload_scope: bool = True,
    approved: bool = True,
    publication_blocked: bool = False,
    connected_by_user_id: str | None = None,
) -> tuple[str, str, str]:
    async def seed() -> tuple[str, str, str]:
        async with get_session_factory()() as session:
            research = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(user_id),
                topic="Grid-scale battery storage",
                status="completed",
            )
            channel = YouTubeChannel(
                organization_id=UUID(organization_id),
                channel_id=f"UC-{uuid4().hex[:20]}",
                title="Publishing Test Channel",
                encrypted_refresh_token="test-encrypted-refresh-token",
                key_version=1,
                granted_scopes=(
                    f"{YOUTUBE_READ_SCOPE} {YOUTUBE_UPLOAD_SCOPE} {YOUTUBE_VIDEO_WRITE_SCOPE}"
                    if upload_scope
                    else YOUTUBE_READ_SCOPE
                ),
                connected_by_user_id=UUID(connected_by_user_id or user_id),
            )
            session.add_all([research, channel])
            await session.flush()

            profile_payload = {
                "niche": "technology",
                "topic_boundaries": [],
                "target_audience": "general audience",
                "language": "en",
                "tone": "clear",
                "format": "educational",
                "prohibited_topics": [],
                "additional_constraints": [],
            }
            profile = YouTubeChannelProfile(
                organization_id=UUID(organization_id),
                youtube_channel_id=channel.id,
                version=1,
                profile=profile_payload,
                created_by_user_id=UUID(user_id),
            )
            session.add(profile)
            await session.flush()

            now = datetime.now(UTC)
            run = ProductionRun(
                organization_id=UUID(organization_id),
                research_run_id=research.id,
                requested_by_user_id=UUID(user_id),
                youtube_channel_id=channel.id,
                channel_profile_id=profile.id,
                channel_profile_version=1,
                channel_profile_snapshot=profile_payload,
                idempotency_key=f"production-{uuid4().hex}",
                status="approved" if approved else "awaiting_review",
                approved_by_user_id=UUID(user_id) if approved else None,
                approved_at=now if approved else None,
                approval_mode="human" if approved else "none",
                approval_consent_event_id=None,
            )
            session.add(run)
            await session.flush()

            rendering = ProductionStage(
                production_run_id=run.id,
                stage_key="rendering",
                status="completed",
                idempotency_key=f"render-{uuid4().hex}",
                output_manifest={},
            )
            quality = ProductionStage(
                production_run_id=run.id,
                stage_key="quality_check",
                status="completed",
                idempotency_key=f"quality-{uuid4().hex}",
                output_manifest={},
            )
            session.add_all([rendering, quality])
            await session.flush()

            source_id = str(uuid4())
            source_rights = [
                {
                    "source_id": source_id,
                    "canonical_url": "https://example.com/research",
                    "title": "Example Research Report",
                    "publisher": "Example Research",
                    "status": "cleared",
                    "scope": "source_text",
                    "license_ids": ["CC0-1.0"],
                    "license_urls": [
                        "https://creativecommons.org/publicdomain/zero/1.0/"
                    ],
                    "rights_holder": "Example Research",
                    "creators": ["Example Research"],
                    "source_revision_url": "https://example.com/research/revision/1",
                    "attribution_text": "Example Research, CC0 1.0",
                    "reuse_restrictions": ["attribution"],
                }
            ]
            credits = [
                {
                    "source_id": source_id,
                    "source_uri": "https://example.com/research",
                    "title": "Example Research Report",
                    "publisher": "Example Research",
                    "rights_status": "cleared",
                    "rights_scope": "source_text",
                    "license_ids": ["CC0-1.0"],
                    "license_urls": [
                        "https://creativecommons.org/publicdomain/zero/1.0/"
                    ],
                    "rights_holder": "Example Research",
                    "creators": ["Example Research"],
                    "source_revision_url": "https://example.com/research/revision/1",
                    "reuse_restrictions": ["attribution"],
                    "modification_note": None,
                    "attribution": "Example Research, CC0 1.0",
                }
            ]
            artifact_id = uuid4()
            metadata = {
                "rights_manifest_version": "1.0.0",
                "attribution_credits": credits,
                "source_rights": source_rights,
                "contains_synthetic_media": False,
            }
            artifact = MediaArtifact(
                id=artifact_id,
                organization_id=UUID(organization_id),
                production_run_id=run.id,
                production_stage_id=rendering.id,
                artifact_kind="video",
                storage_backend="local",
                storage_key=f"{organization_id}/{artifact_id}.mp4",
                mime_type="video/mp4",
                sha256="a" * 64,
                byte_size=123456,
                duration_ms=30000,
                width=1080,
                height=1920,
                metadata_json=metadata,
            )
            session.add(artifact)
            rendering.output_manifest = {
                "video_artifact_id": str(artifact.id),
                "contains_synthetic_media": False,
            }
            quality.output_manifest = {
                "video_artifact_id": str(artifact.id),
                "quality_report": {"is_valid": True, "issues": []},
                "publication_blocked": publication_blocked,
                "claim_flags": [],
                "rights_flags": (
                    [{"reason": "test_blocked"}] if publication_blocked else []
                ),
                "attribution_credits": credits,
                "source_rights": source_rights,
                "rights_manifest_version": "1.0.0",
                "contains_synthetic_media": False,
                "media_manifest_flags": [],
            }
            await session.commit()
            return str(run.id), str(channel.id), str(artifact.id)

    return asyncio.run(seed())


def cleanup_user(user_id: str) -> None:
    async def cleanup() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                organization_ids = select(Organization.id).where(
                    Organization.created_by_user_id == user_id
                )
                await session.execute(
                    delete(ProductionRun).where(
                        ProductionRun.organization_id.in_(organization_ids)
                    )
                )
                await session.execute(
                    delete(ResearchRun).where(
                        ResearchRun.organization_id.in_(organization_ids)
                    )
                )
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.user_id == user_id
                    )
                )
                await session.execute(
                    delete(Organization).where(
                        Organization.created_by_user_id == user_id
                    )
                )
                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def test_youtube_publish_intent_is_idempotent_and_preserves_render_credits():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, artifact_id = seed_approved_video(user_id, organization_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "daily-publish-2026-11-13",
    }
    url = (
        f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
        "/youtube-publish-jobs"
    )
    payload = {
        "title": "Grid-scale storage explained",
        "description": "A short evidence-backed explanation.",
        "tags": ["energy", "batteries"],
        "privacy_status": "private",
    }

    try:
        first = client.post(url, headers=headers, json=payload)
        replay = client.post(url, headers=headers, json=payload)

        assert first.status_code == 201
        assert replay.status_code == 201
        body = first.json()
        assert replay.json()["id"] == body["id"]
        changed_replay = client.post(
            url,
            headers=headers,
            json={**payload, "title": "Different title"},
        )
        assert changed_replay.status_code == 409
        assert body["status"] == "queued"
        assert body["production_run_id"] == run_id
        assert body["youtube_channel_id"] == channel_id
        assert body["media_artifact_id"] == artifact_id
        assert body["approved_by_user_id"] == user_id
        assert body["privacy_status"] == "private"
        assert "Sources and credits" in body["description"]
        assert "Example Research, CC0 1.0" in body["description"]
        assert "https://creativecommons.org/publicdomain/zero/1.0/" in body["description"]

        async def assert_handoff_snapshot():
            async with get_session_factory()() as session:
                run = await session.get(ProductionRun, UUID(run_id))
                job = await session.get(YouTubePublishJob, UUID(body["id"]))
                artifact = await session.get(MediaArtifact, UUID(artifact_id))
                assert run is not None and job is not None and artifact is not None
                profile_snapshot = job.metadata_snapshot["editorial_profile"]
                assert profile_snapshot["channel_profile_id"] == str(run.channel_profile_id)
                assert profile_snapshot["channel_profile_version"] == run.channel_profile_version
                assert profile_snapshot["channel_profile_snapshot"] == run.channel_profile_snapshot
                production_snapshot = job.metadata_snapshot["production"]
                assert production_snapshot["production_run_id"] == str(run.id)
                assert production_snapshot["research_run_id"] == str(run.research_run_id)
                media_snapshot = job.metadata_snapshot["media_artifact"]
                assert media_snapshot["artifact_id"] == str(artifact.id)
                assert media_snapshot["sha256"] == artifact.sha256
                assert media_snapshot["metadata"] == artifact.metadata_json
                assert job.youtube_channel_id == run.youtube_channel_id
                assert job.production_run_id == run.id
                assert job.media_artifact_id == artifact.id

        asyncio.run(assert_handoff_snapshot())

        events = client.get(
            f"/api/v1/organizations/{organization_id}/youtube-publish-jobs/{body['id']}/events",
            headers=headers,
        )
        assert events.status_code == 200
        assert [event["event_type"] for event in events.json()] == [
            "publish_intent_created"
        ]
        assert events.json()[0]["event_metadata"]["channel_profile_version"] == 1
        assert events.json()[0]["event_metadata"]["channel_profile_id"]

        listing = client.get(
            f"/api/v1/organizations/{organization_id}/youtube-publish-jobs",
            headers=headers,
        )
        assert listing.status_code == 200
        assert [job["id"] for job in listing.json()] == [body["id"]]
    finally:
        cleanup_user(user_id)


def test_youtube_publish_intent_rejects_read_only_channel():
    user_id, token, organization_id = create_user_and_organization()
    run_id, _, _ = seed_approved_video(
        user_id,
        organization_id,
        upload_scope=False,
    )
    try:
        response = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
                "/youtube-publish-jobs"
            ),
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "read-only-channel",
            },
            json={"title": "Must not upload"},
        )
        assert response.status_code == 409
        assert "upload permission" in response.json()["detail"]
    finally:
        cleanup_user(user_id)


def test_reconcile_existing_youtube_video_resumes_without_duplicate_upload():
    user_id, token, organization_id = create_user_and_organization()
    run_id, _, _ = seed_approved_video(user_id, organization_id)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        created = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
                "/youtube-publish-jobs"
            ),
            headers={**headers, "Idempotency-Key": "reconcile-existing-video"},
            json={"title": "Existing video", "privacy_status": "public"},
        )
        assert created.status_code == 201
        job_id = UUID(created.json()["id"])

        async def mark_existing_video() -> None:
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, job_id)
                assert job is not None
                snapshot = dict(job.metadata_snapshot)
                snapshot["thumbnail_artifact"] = {
                    "artifact_id": str(uuid4()),
                    "storage_backend": "local",
                    "storage_key": "thumbnail.jpg",
                    "mime_type": "image/jpeg",
                    "sha256": "b" * 64,
                    "byte_size": 12,
                    "uploaded": False,
                }
                job.metadata_snapshot = snapshot
                job.status = "reconciliation_required"
                job.failure_code = "youtube_upload_reconciliation_required"
                job.failure_message = "The provider response needs reconciliation."
                job.youtube_video_id = "already-created-video"
                await session.commit()

        asyncio.run(mark_existing_video())
        resumed = client.post(
            f"/api/v1/organizations/{organization_id}/youtube-publish-jobs/{job_id}/reconcile",
            headers=headers,
        )
        assert resumed.status_code == 200
        assert resumed.json()["status"] == "processing"
        assert resumed.json()["youtube_video_id"] == "already-created-video"

        async def assert_resume_snapshot() -> None:
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, job_id)
                assert job is not None
                thumbnail = job.metadata_snapshot["thumbnail_artifact"]
                assert thumbnail["upload_skipped"] is True
                assert thumbnail["uploaded"] is False
                events = await session.scalars(
                    select(YouTubePublishEvent).where(
                        YouTubePublishEvent.publish_job_id == job_id
                    )
                )
                assert "upload_reconciliation_resumed" in {
                    event.event_type for event in events.all()
                }

        asyncio.run(assert_resume_snapshot())
    finally:
        cleanup_user(user_id)


def test_reconcile_existing_video_requires_video_update_scope_before_requeue():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, _ = seed_approved_video(user_id, organization_id)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        created = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
                "/youtube-publish-jobs"
            ),
            headers={**headers, "Idempotency-Key": "reconcile-needs-video-write"},
            json={"title": "Already uploaded video", "privacy_status": "public"},
        )
        assert created.status_code == 201
        job_id = UUID(created.json()["id"])

        async def simulate_video_already_received_without_write_scope() -> None:
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, job_id)
                channel = await session.get(YouTubeChannel, UUID(channel_id))
                assert job is not None and channel is not None
                job.status = "reconciliation_required"
                job.youtube_video_id = "already-created-video"
                channel.granted_scopes = f"{YOUTUBE_READ_SCOPE} {YOUTUBE_UPLOAD_SCOPE}"
                await session.commit()

        asyncio.run(simulate_video_already_received_without_write_scope())
        response = client.post(
            f"/api/v1/organizations/{organization_id}/youtube-publish-jobs/{job_id}/reconcile",
            headers=headers,
        )
        assert response.status_code == 409
        assert "video-update permissions" in response.json()["detail"]

        async def assert_job_stays_paused() -> None:
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, job_id)
                assert job is not None
                assert job.status == "reconciliation_required"
                assert job.youtube_video_id == "already-created-video"

        asyncio.run(assert_job_stays_paused())
    finally:
        cleanup_user(user_id)


def test_youtube_publish_intent_rejects_unapproved_or_blocked_runs():
    user_id, token, organization_id = create_user_and_organization()
    unapproved_run_id, _, _ = seed_approved_video(
        user_id,
        organization_id,
        approved=False,
    )
    blocked_run_id, _, _ = seed_approved_video(
        user_id,
        organization_id,
        publication_blocked=True,
    )
    headers = {"Authorization": f"Bearer {token}"}

    try:
        unapproved = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/"
                f"{unapproved_run_id}/youtube-publish-jobs"
            ),
            headers={**headers, "Idempotency-Key": "unapproved"},
            json={"title": "Must not upload"},
        )
        blocked = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/"
                f"{blocked_run_id}/youtube-publish-jobs"
            ),
            headers={**headers, "Idempotency-Key": "blocked"},
            json={"title": "Must not upload"},
        )
        assert unapproved.status_code == 409
        assert "explicit human approval" in unapproved.json()["detail"]
        assert blocked.status_code == 409
        assert "quality gate blocks publication" in blocked.json()["detail"]
    finally:
        cleanup_user(user_id)


def test_automatic_publishing_requires_exact_channel_connector_and_upload_scope():
    user_id, token, organization_id = create_user_and_organization()
    other_user_id = create_verified_user()
    _, channel_id, _ = seed_approved_video(
        user_id,
        organization_id,
        connected_by_user_id=other_user_id,
    )
    url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/"
        f"{channel_id}/publishing-settings"
    )
    headers = {"Authorization": f"Bearer {token}"}

    try:
        denied = client.put(
            url,
            headers=headers,
            json={"auto_publish_enabled": True},
        )
        assert denied.status_code == 403

        async def set_connector():
            async with get_session_factory()() as session:
                channel = await session.get(YouTubeChannel, UUID(channel_id))
                assert channel is not None
                channel.connected_by_user_id = UUID(user_id)
                await session.commit()

        asyncio.run(set_connector())
        enabled = client.put(
            url,
            headers=headers,
            json={"auto_publish_enabled": True, "privacy_status": "public"},
        )
        assert enabled.status_code == 200
        assert enabled.json()["auto_publish_enabled"] is True
        assert enabled.json()["auto_publish_privacy_status"] == "public"
        assert enabled.json()["automatic_execution_available"] is False
        assert enabled.json()["can_publish"] is True
        assert enabled.json()["can_manage_auto_publish"] is True

        revoked = client.put(
            url,
            headers=headers,
            json={"auto_publish_enabled": False},
        )
        assert revoked.status_code == 200
        assert revoked.json()["auto_publish_enabled"] is False
        assert revoked.json()["auto_publish_privacy_status"] == "public"

        async def verify_events():
            async with get_session_factory()() as session:
                events = await session.scalars(
                    select(YouTubePublishingConsentEvent)
                    .where(
                        YouTubePublishingConsentEvent.youtube_channel_id
                        == UUID(channel_id)
                    )
                    .order_by(YouTubePublishingConsentEvent.created_at.asc())
                )
                assert [event.action for event in events.all()] == [
                    "enabled",
                    "disabled",
                ]

        asyncio.run(verify_events())
        consent_history = client.get(
            (
                f"/api/v1/organizations/{organization_id}/youtube-channels/"
                f"{channel_id}/publishing-consent-events"
            ),
            headers=headers,
        )
        assert consent_history.status_code == 200
        assert {event["action"] for event in consent_history.json()} == {
            "enabled",
            "disabled",
        }
    finally:
        cleanup_user(user_id)
        async def cleanup_other():
            async with get_session_factory()() as session:
                await session.execute(delete(User).where(User.id == UUID(other_user_id)))
                await session.commit()
        asyncio.run(cleanup_other())


def test_automatic_publishing_cannot_be_enabled_without_upload_scope():
    user_id, token, organization_id = create_user_and_organization()
    _, channel_id, _ = seed_approved_video(
        user_id,
        organization_id,
        upload_scope=False,
    )
    response = client.put(
        (
            f"/api/v1/organizations/{organization_id}/youtube-channels/"
            f"{channel_id}/publishing-settings"
        ),
        headers={"Authorization": f"Bearer {token}"},
        json={"auto_publish_enabled": True},
    )
    try:
        assert response.status_code == 409
        assert "upload permission" in response.json()["detail"]
    finally:
        cleanup_user(user_id)


def test_youtube_publish_worker_resumes_and_completes_approved_public_upload():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, artifact_id = seed_approved_video(user_id, organization_id)
    payload = {
        "title": "Worker integration test",
        "description": "Approved render.",
        "privacy_status": "public",
    }
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "worker-integration-test",
        },
        json=payload,
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    video_bytes = b"synthetic approved MP4 bytes"
    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    encryption_key = "ef" * 32
    worker_settings = Settings(
        integration_token_encryption_key=encryption_key,
        integration_token_active_key_version=1,
        youtube_client_id="worker-test-client",
        youtube_client_secret="worker-test-secret",
    )

    async def prepare_worker_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            artifact = await session.get(MediaArtifact, UUID(artifact_id))
            job = await session.get(YouTubePublishJob, UUID(job_id))
            assert channel is not None
            assert artifact is not None
            assert job is not None
            channel.encrypted_refresh_token = encrypt_refresh_token(
                "refresh-token-secret",
                encryption_key,
                UUID(organization_id),
                channel.channel_id,
                key_version=1,
            )
            channel.key_version = 1
            artifact.sha256 = video_sha256
            artifact.byte_size = len(video_bytes)
            job.sha256 = video_sha256
            job.byte_size = len(video_bytes)
            await session.commit()

    asyncio.run(prepare_worker_job())

    class FakeStorage:
        async def get(self, storage_key: str) -> bytes:
            assert storage_key.endswith(".mp4")
            return video_bytes

        async def aclose(self) -> None:
            return None

    class FakeUploadClient:
        def __init__(self):
            def token_handler(request: httpx.Request) -> httpx.Response:
                assert str(request.url) == "https://oauth2.googleapis.com/token"
                assert b"grant_type=refresh_token" in request.content
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ephemeral-access-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "rotated-refresh-token",
                    },
                )

            self.http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(token_handler)
            )

        async def refresh_access_token(self, refresh_token: str) -> str:
            raise AssertionError("The worker must use the proxy-bound token service.")

        async def initiate_upload(self, **kwargs) -> str:
            assert kwargs["title"] == payload["title"]
            assert kwargs["privacy_status"] == "private"
            assert kwargs["total_bytes"] == len(video_bytes)
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, UUID(job_id))
                assert job is not None
                assert job.status == "initiating"
            return (
                "https://www.googleapis.com/upload/youtube/v3/videos"
                "?upload_id=opaque-worker-session"
            )

        async def upload_chunk(self, **kwargs) -> UploadChunkResult:
            assert kwargs["access_token"] == "ephemeral-access-token"
            assert kwargs["content"] == video_bytes
            return UploadChunkResult(
                bytes_received=len(video_bytes),
                video_id="youtube-video-worker-test",
            )

        async def query_upload_status(self, **kwargs) -> UploadChunkResult:
            raise AssertionError("New upload must not require a session reconciliation.")

        async def get_video_processing_status(
            self,
            *,
            access_token: str,
            video_id: str,
        ) -> YouTubeVideoProcessingStatus:
            assert access_token == "ephemeral-access-token"
            assert video_id == "youtube-video-worker-test"
            return YouTubeVideoProcessingStatus(
                upload_status="processed",
                processing_status="succeeded",
                privacy_status="private",
                published_at=None,
            )

        async def update_video_privacy_status(
            self,
            *,
            access_token: str,
            video_id: str,
            privacy_status: str,
        ) -> str:
            assert access_token == "ephemeral-access-token"
            assert video_id == "youtube-video-worker-test"
            assert privacy_status == "public"
            return "public"

    async def run_worker_once():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-youtube-worker",
            lease_seconds=300,
        )
        assert claim is not None
        claimed_id, lease_token = claim
        assert claimed_id == UUID(job_id)
        duplicate_claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="second-youtube-worker",
            lease_seconds=300,
        )
        assert duplicate_claim is None
        upload_client = FakeUploadClient()
        try:
            await process_claimed_youtube_publish_job(
                job_id=claimed_id,
                lease_token=lease_token,
                worker_id="test-youtube-worker",
                session_factory=get_session_factory(),
                storage=FakeStorage(),
                upload_client=upload_client,
                settings=worker_settings,
            )
        finally:
            await upload_client.http_client.aclose()

    try:
        asyncio.run(run_worker_once())

        async def verify_published_job():
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, UUID(job_id))
                assert job is not None
                assert job.status == "published"
                assert job.youtube_video_id == "youtube-video-worker-test"
                assert job.bytes_uploaded == len(video_bytes)
                assert job.upload_session_url_encrypted is None
                assert job.lease_token is None
                channel = await session.get(YouTubeChannel, UUID(channel_id))
                assert channel is not None
                assert decrypt_refresh_token(
                    channel.encrypted_refresh_token,
                    {1: encryption_key},
                    UUID(organization_id),
                    channel.channel_id,
                    channel.key_version,
                ) == "rotated-refresh-token"
                events = await session.scalars(
                    select(YouTubePublishEvent)
                    .where(YouTubePublishEvent.publish_job_id == UUID(job_id))
                    .order_by(YouTubePublishEvent.created_at.asc())
                )
                event_types = {event.event_type for event in events.all()}
                assert {
                    "worker_claimed",
                    "upload_initiation_started",
                    "upload_session_created",
                    "upload_completed",
                    "video_published",
                }.issubset(event_types)

        asyncio.run(verify_published_job())
    finally:
        cleanup_user(user_id)


def test_youtube_worker_requires_actual_visibility_to_match_approved_policy():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, artifact_id = seed_approved_video(user_id, organization_id)
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "private-visibility-mismatch",
        },
        json={"title": "Must remain private", "privacy_status": "private"},
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    video_bytes = b"synthetic private video bytes"
    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    encryption_key = "12" * 32
    worker_settings = Settings(
        integration_token_encryption_key=encryption_key,
        integration_token_active_key_version=1,
    )

    async def prepare_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            artifact = await session.get(MediaArtifact, UUID(artifact_id))
            job = await session.get(YouTubePublishJob, UUID(job_id))
            assert channel is not None
            assert artifact is not None
            assert job is not None
            channel.encrypted_refresh_token = encrypt_refresh_token(
                "refresh-token-secret",
                encryption_key,
                UUID(organization_id),
                channel.channel_id,
                key_version=1,
            )
            channel.key_version = 1
            artifact.sha256 = video_sha256
            artifact.byte_size = len(video_bytes)
            job.sha256 = video_sha256
            job.byte_size = len(video_bytes)
            await session.commit()

    asyncio.run(prepare_job())

    class FakeStorage:
        async def get(self, storage_key: str) -> bytes:
            return video_bytes

        async def aclose(self) -> None:
            return None

    class FakeUploadClient:
        async def refresh_access_token(self, refresh_token: str) -> str:
            return "ephemeral-access-token"

        async def initiate_upload(self, **kwargs) -> str:
            assert kwargs["privacy_status"] == "private"
            return (
                "https://www.googleapis.com/upload/youtube/v3/videos"
                "?upload_id=private-visibility-session"
            )

        async def upload_chunk(self, **kwargs) -> UploadChunkResult:
            return UploadChunkResult(
                bytes_received=len(video_bytes),
                video_id="unexpectedly-public-video",
            )

        async def query_upload_status(self, **kwargs) -> UploadChunkResult:
            raise AssertionError("New upload must not query the existing session.")

        async def get_video_processing_status(
            self,
            *,
            access_token: str,
            video_id: str,
        ) -> YouTubeVideoProcessingStatus:
            return YouTubeVideoProcessingStatus(
                upload_status="processed",
                processing_status="succeeded",
                privacy_status="public",
                published_at="2026-11-13T03:10:00Z",
            )

    async def run_worker_once():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-private-policy-worker",
            lease_seconds=300,
        )
        assert claim is not None
        await process_claimed_youtube_publish_job(
            job_id=claim[0],
            lease_token=claim[1],
            worker_id="test-private-policy-worker",
            session_factory=get_session_factory(),
            storage=FakeStorage(),
            upload_client=FakeUploadClient(),
            settings=worker_settings,
        )

    try:
        asyncio.run(run_worker_once())

        async def verify_not_published():
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, UUID(job_id))
                assert job is not None
                assert job.status == "reconciliation_required"
                assert job.failure_code == "privacy_status_mismatch"
                assert job.published_at is None
                assert job.youtube_video_id == "unexpectedly-public-video"

        asyncio.run(verify_not_published())
    finally:
        cleanup_user(user_id)


@pytest.mark.parametrize("mutation", ["approval_revoked", "quality_blocked"])
def test_worker_revalidates_approval_and_quality_before_provider_calls(mutation):
    user_id, token, organization_id = create_user_and_organization()
    run_id, _, _ = seed_approved_video(user_id, organization_id)
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"worker-revalidation-{mutation}",
        },
        json={"title": "Revalidation test", "privacy_status": "private"},
    )
    assert response.status_code == 201
    job_id = UUID(response.json()["id"])

    async def claim_and_mutate():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-revalidation-worker",
            lease_seconds=300,
        )
        assert claim is not None
        assert claim[0] == job_id
        async with get_session_factory()() as session:
            if mutation == "approval_revoked":
                run = await session.get(ProductionRun, UUID(run_id))
                assert run is not None
                run.status = "awaiting_review"
            else:
                quality = await session.scalar(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == UUID(run_id),
                        ProductionStage.stage_key == "quality_check",
                    )
                )
                assert quality is not None
                quality.output_manifest = {
                    **quality.output_manifest,
                    "publication_blocked": True,
                }
            await session.commit()
        return claim

    try:
        claim = asyncio.run(claim_and_mutate())
        with pytest.raises(YouTubeUploadPermanentError):
            asyncio.run(
                validate_youtube_publish_job_snapshot(
                    get_session_factory(),
                    job_id=claim[0],
                    lease_token=claim[1],
                )
            )
    finally:
        cleanup_user(user_id)


def test_worker_revalidates_approval_between_resumable_chunks():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, artifact_id = seed_approved_video(user_id, organization_id)
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "worker-revalidate-between-chunks",
        },
        json={"title": "Chunk revalidation test", "privacy_status": "private"},
    )
    assert response.status_code == 201
    job_id = UUID(response.json()["id"])
    video_bytes = b"x" * 600000
    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    encryption_key = "34" * 32
    worker_settings = Settings(
        integration_token_encryption_key=encryption_key,
        integration_token_active_key_version=1,
        youtube_upload_chunk_size_bytes=262144,
    )

    async def prepare_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            artifact = await session.get(MediaArtifact, UUID(artifact_id))
            job = await session.get(YouTubePublishJob, job_id)
            assert channel is not None
            assert artifact is not None
            assert job is not None
            channel.encrypted_refresh_token = encrypt_refresh_token(
                "refresh-token-secret",
                encryption_key,
                UUID(organization_id),
                channel.channel_id,
                key_version=1,
            )
            channel.key_version = 1
            artifact.sha256 = video_sha256
            artifact.byte_size = len(video_bytes)
            job.sha256 = video_sha256
            job.byte_size = len(video_bytes)
            await session.commit()

    asyncio.run(prepare_job())

    class FakeStorage:
        async def get(self, storage_key: str) -> bytes:
            return video_bytes

        async def aclose(self) -> None:
            return None

    class FakeUploadClient:
        def __init__(self):
            self.chunk_calls = 0

        async def refresh_access_token(self, refresh_token: str) -> str:
            assert refresh_token == "refresh-token-secret"
            return "ephemeral-access-token"

        async def initiate_upload(self, **kwargs) -> str:
            return (
                "https://www.googleapis.com/upload/youtube/v3/videos"
                "?upload_id=chunk-revalidation-session"
            )

        async def query_upload_status(self, **kwargs) -> UploadChunkResult:
            return UploadChunkResult(bytes_received=0)

        async def upload_chunk(self, **kwargs) -> UploadChunkResult:
            self.chunk_calls += 1
            assert self.chunk_calls == 1
            async with get_session_factory()() as session:
                run = await session.get(ProductionRun, UUID(run_id))
                assert run is not None
                run.status = "awaiting_review"
                await session.commit()
            return UploadChunkResult(
                bytes_received=kwargs["start"] + len(kwargs["content"])
            )

    async def run_worker_once():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-chunk-revalidation-worker",
            lease_seconds=300,
        )
        assert claim is not None
        upload_client = FakeUploadClient()
        with pytest.raises(YouTubeUploadPermanentError):
            await process_claimed_youtube_publish_job(
                job_id=claim[0],
                lease_token=claim[1],
                worker_id="test-chunk-revalidation-worker",
                session_factory=get_session_factory(),
                storage=FakeStorage(),
                upload_client=upload_client,
                settings=worker_settings,
            )
        assert upload_client.chunk_calls == 1

    try:
        asyncio.run(run_worker_once())
    finally:
        cleanup_user(user_id)



def test_worker_requires_reauthorization_for_disconnected_channel():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, _ = seed_approved_video(user_id, organization_id)
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "disconnected-channel-revalidation",
        },
        json={"title": "Disconnected channel test", "privacy_status": "private"},
    )
    assert response.status_code == 201
    job_id = UUID(response.json()["id"])

    async def claim_and_disconnect():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-disconnected-channel-worker",
            lease_seconds=300,
        )
        assert claim is not None
        assert claim[0] == job_id
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            assert channel is not None
            channel.status = "disconnected"
            await session.commit()
        return claim

    try:
        claim = asyncio.run(claim_and_disconnect())
        with pytest.raises(YouTubeReauthorizationRequired):
            asyncio.run(
                validate_youtube_publish_job_snapshot(
                    get_session_factory(),
                    job_id=claim[0],
                    lease_token=claim[1],
                )
            )
    finally:
        cleanup_user(user_id)


def test_final_quality_stage_automatically_creates_consent_bound_publish_job(monkeypatch):
    monkeypatch.setattr(
        "app.core.config.get_settings",
        lambda: Settings(
            youtube_publish_worker_enabled=True,
            youtube_client_id="test-youtube-client",
            youtube_client_secret="test-youtube-secret",
            integration_token_encryption_key="56" * 32,
            integration_token_active_key_version=1,
            youtube_egress_proxy_url="http://127.0.0.1:3128",
        ),
    )
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, _ = seed_approved_video(
        user_id, organization_id, approved=False
    )
    lease_token = uuid4()

    async def prepare_quality_completion():
        async with get_session_factory()() as session:
            run = await session.get(ProductionRun, UUID(run_id))
            organization = await session.get(Organization, UUID(organization_id))
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            quality = await session.scalar(
                select(ProductionStage).where(
                    ProductionStage.production_run_id == UUID(run_id),
                    ProductionStage.stage_key == "quality_check",
                )
            )
            assert run is not None
            assert organization is not None
            assert channel is not None
            assert quality is not None
            now = datetime.now(UTC)
            organization.studio_settings = {
                "automatic_publishing_enabled": True,
            }
            run.channel_profile_snapshot = {
                **run.channel_profile_snapshot,
                "_workspace_preferences": {
                    "automatic_publishing_enabled": True,
                },
            }
            channel.auto_publish_enabled = True
            channel.auto_publish_privacy_status = "public"
            channel.auto_publish_consent_granted_by_user_id = UUID(user_id)
            channel.auto_publish_consent_granted_at = now
            consent_event = YouTubePublishingConsentEvent(
                id=uuid4(),
                organization_id=UUID(organization_id),
                youtube_channel_id=UUID(channel_id),
                actor_user_id=UUID(user_id),
                action="enabled",
                reason="explicit_channel_owner_consent",
                granted_scopes_snapshot=channel.granted_scopes,
            )
            channel.auto_publish_consent_event_id = consent_event.id
            session.add(consent_event)
            quality.status = "running"
            quality.lease_token = lease_token
            quality.lease_expires_at = now + timedelta(minutes=5)
            completed_stages = [
                ProductionStage(
                    production_run_id=run.id,
                    stage_key=stage_key,
                    status="completed",
                    idempotency_key=f"{run.id}:{stage_key}",
                    input_manifest={},
                    output_manifest={},
                )
                for stage_key in (
                    "story_generation",
                    "claim_verification",
                    "ai_direction",
                    "narration",
                    "visual_generation",
                )
            ]
            review = ProductionStage(
                production_run_id=run.id,
                stage_key="awaiting_review",
                status="pending",
                idempotency_key=f"{run.id}:awaiting_review",
                input_manifest={},
                output_manifest={},
            )
            session.add_all([*completed_stages, review])
            await session.flush()
            verified_stage = next(
                stage for stage in completed_stages
                if stage.stage_key == "claim_verification"
            )
            session.add(
                StoryArtifactRecord(
                    organization_id=UUID(organization_id),
                    production_run_id=run.id,
                    production_stage_id=verified_stage.id,
                    artifact_type="story_verified",
                    schema_version="1.3.0",
                    version=1,
                    content_hash="a" * 64,
                    payload={
                        "title": "A safer energy future",
                        "hook": "A new battery design could change grid storage.",
                    },
                )
            )
            quality_stage_id = quality.id
            quality_manifest = quality.output_manifest
            await session.commit()
            return quality_stage_id, quality_manifest

    quality_stage_id, quality_manifest = asyncio.run(prepare_quality_completion())
    completed = asyncio.run(
        complete_production_stage(
            quality_stage_id,
            lease_token,
            output_manifest=quality_manifest,
            session_factory=get_session_factory(),
        )
    )
    assert completed is True

    async def verify_automatic_job():
        async with get_session_factory()() as session:
            run = await session.get(ProductionRun, UUID(run_id))
            job = await session.scalar(
                select(YouTubePublishJob).where(
                    YouTubePublishJob.production_run_id == UUID(run_id)
                )
            )
            assert run is not None
            assert job is not None
            assert run.status == "approved"
            assert run.approval_mode == "automatic_consent"
            assert run.approved_by_user_id is None
            assert run.approval_consent_event_id is not None
            assert job.status == "queued"
            assert job.approval_mode == "automatic_consent"
            assert job.approved_by_user_id is None
            assert job.approval_consent_event_id == run.approval_consent_event_id
            assert job.requested_by_user_id == UUID(user_id)
            assert job.privacy_status == "public"
            assert job.metadata_snapshot["approval_mode"] == "automatic_consent"
            assert job.metadata_snapshot["contains_synthetic_media"] is False
            assert job.metadata_snapshot["auto_publish_privacy_status"] == "public"
            return job.id

    job_id = asyncio.run(verify_automatic_job())
    claim = asyncio.run(
        claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id="test-auto-consent-revocation-worker",
            lease_seconds=300,
        )
    )
    assert claim is not None
    assert claim[0] == job_id
    asyncio.run(
        validate_youtube_publish_job_snapshot(
            get_session_factory(),
            job_id=claim[0],
            lease_token=claim[1],
        )
    )

    revoke = client.put(
        (
            f"/api/v1/organizations/{organization_id}/youtube-channels/{channel_id}"
            "/publishing-settings"
        ),
        headers={"Authorization": f"Bearer {token}"},
        json={"auto_publish_enabled": False, "privacy_status": "public"},
    )
    assert revoke.status_code == 200
    assert revoke.json()["auto_publish_enabled"] is False
    with pytest.raises(YouTubeUploadPermanentError):
        asyncio.run(
            validate_youtube_publish_job_snapshot(
                get_session_factory(),
                job_id=claim[0],
                lease_token=claim[1],
            )
        )
    # A late response from a request that was already in flight must not
    # overwrite the reconciliation_required state established by revocation.
    with pytest.raises(YouTubePublishLeaseLostError):
        asyncio.run(
            update_youtube_publish_job_under_lease(
                get_session_factory(),
                job_id=claim[0],
                lease_token=claim[1],
                values={"status": "uploading", "bytes_uploaded": 262144},
                event_type="late_upload_progress",
                event_metadata={"bytes_uploaded": 262144},
            )
        )

    async def verify_revocation():
        async with get_session_factory()() as session:
            run = await session.get(ProductionRun, UUID(run_id))
            job = await session.get(YouTubePublishJob, job_id)
            assert run is not None
            assert job is not None
            assert run.status == "awaiting_review"
            assert run.approval_mode == "none"
            assert run.approved_by_user_id is None
            assert run.approved_at is None
            assert job.status == "reconciliation_required"
            assert job.failure_code == (
                "automatic_publishing_consent_revoked_reconciliation_required"
            )
            events = await session.scalars(
                select(YouTubePublishEvent).where(
                    YouTubePublishEvent.publish_job_id == job_id
                )
            )
            assert "automatic_publishing_stopped" in {
                event.event_type for event in events.all()
            }

    try:
        asyncio.run(verify_revocation())
    finally:
        cleanup_user(user_id)


def test_revoked_unclaimed_automatic_job_can_return_to_human_publish_flow():
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, _ = seed_approved_video(
        user_id,
        organization_id,
        approved=False,
    )

    async def seed_automatic_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            run = await session.get(ProductionRun, UUID(run_id))
            assert channel is not None and run is not None
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            consent = YouTubePublishingConsentEvent(
                id=uuid4(),
                organization_id=UUID(organization_id),
                youtube_channel_id=channel.id,
                actor_user_id=UUID(user_id),
                action="enabled",
                reason="explicit_channel_owner_consent",
                granted_scopes_snapshot=channel.granted_scopes,
            )
            session.add(consent)
            # The production-run composite FK references this consent event;
            # persist it before assigning the event ID to the run.
            await session.flush()
            channel.auto_publish_enabled = True
            channel.auto_publish_consent_granted_by_user_id = UUID(user_id)
            channel.auto_publish_consent_granted_at = now
            channel.auto_publish_consent_event_id = consent.id
            run.status = "approved"
            run.approval_mode = "automatic_consent"
            run.approved_by_user_id = None
            run.approved_at = now
            run.approval_consent_event_id = consent.id
            await session.flush()
            job = await create_youtube_publish_job(
                session,
                organization_id=UUID(organization_id),
                production_run_id=run.id,
                requested_by_user_id=UUID(user_id),
                idempotency_key=f"auto-consent:{run.id}",
                title="Grid-scale battery storage",
                description="A short evidence-backed explanation.",
                tags=["technology"],
                privacy_status="private",
                publish_at=None,
            )
            await session.commit()
            return job.id

    job_id = asyncio.run(seed_automatic_job())
    settings_url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/"
        f"{channel_id}/publishing-settings"
    )
    headers = {"Authorization": f"Bearer {token}"}

    try:
        revoked = client.put(
            settings_url,
            headers=headers,
            json={"auto_publish_enabled": False},
        )
        assert revoked.status_code == 200
        assert revoked.json()["auto_publish_enabled"] is False

        async def approve_after_revocation():
            async with get_session_factory()() as session:
                run = await session.get(ProductionRun, UUID(run_id))
                job = await session.get(YouTubePublishJob, job_id)
                assert run is not None and job is not None
                assert run.status == "awaiting_review"
                assert job.status == "cancelled"
                assert job.lease_token is None
                await approve_production_run(
                    session,
                    organization_id=UUID(organization_id),
                    production_run_id=run.id,
                    approved_by_user_id=UUID(user_id),
                )
                await session.commit()

        asyncio.run(approve_after_revocation())

        response = client.post(
            (
                f"/api/v1/organizations/{organization_id}/production-runs/"
                f"{run_id}/youtube-publish-jobs"
            ),
            headers={**headers, "Idempotency-Key": "human-approved-after-revoke"},
            json={
                "title": "Grid-scale battery storage",
                "description": "A short evidence-backed explanation.",
                "tags": ["technology"],
                "privacy_status": "private",
            },
        )
        assert response.status_code == 201
        assert response.json()["id"] != str(job_id)
        assert response.json()["approval_mode"] == "human"
        assert response.json()["approved_by_user_id"] == user_id
    finally:
        cleanup_user(user_id)

def test_automatic_consent_can_create_a_private_upload_scheduled_for_channel_publish_time():
    user_id, _, organization_id = create_user_and_organization()
    run_id, channel_id, _ = seed_approved_video(
        user_id,
        organization_id,
        approved=False,
    )
    target_publish_at = datetime.now(UTC) + timedelta(hours=1)

    async def create_scheduled_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            run = await session.get(ProductionRun, UUID(run_id))
            assert channel is not None and run is not None
            now = await session.scalar(select(func.clock_timestamp()))
            assert now is not None
            consent = YouTubePublishingConsentEvent(
                id=uuid4(),
                organization_id=UUID(organization_id),
                youtube_channel_id=UUID(channel_id),
                actor_user_id=UUID(user_id),
                action="enabled",
                reason="explicit_channel_owner_consent",
                granted_scopes_snapshot=channel.granted_scopes,
            )
            session.add(consent)
            await session.flush()
            channel.auto_publish_enabled = True
            channel.auto_publish_privacy_status = "public"
            channel.auto_publish_consent_granted_by_user_id = UUID(user_id)
            channel.auto_publish_consent_granted_at = now
            channel.auto_publish_consent_event_id = consent.id
            run.status = "approved"
            run.approval_mode = "automatic_consent"
            run.approved_by_user_id = None
            run.approved_at = now
            run.approval_consent_event_id = consent.id
            run.scheduled_local_date = target_publish_at.date()
            run.scheduled_for = target_publish_at
            await session.flush()
            job = await create_youtube_publish_job(
                session,
                organization_id=UUID(organization_id),
                production_run_id=run.id,
                requested_by_user_id=UUID(user_id),
                idempotency_key=f"scheduled-auto-consent:{run.id}",
                title="Scheduled energy storage explainer",
                description="An evidence-backed short.",
                tags=["technology"],
                privacy_status="private",
                publish_at=target_publish_at,
            )
            await session.commit()
            return job.id

    try:
        job_id = asyncio.run(create_scheduled_job())

        async def verify():
            async with get_session_factory()() as session:
                job = await session.get(YouTubePublishJob, job_id)
                assert job is not None
                assert job.approval_mode == "automatic_consent"
                assert job.privacy_status == "private"
                assert job.publish_at == target_publish_at
                assert job.metadata_snapshot["auto_publish_privacy_status"] == "public"

        asyncio.run(verify())
    finally:
        cleanup_user(user_id)


@pytest.mark.parametrize(
    "crash_point",
    ["before_upload", "after_upload_before_reconciliation", "after_reconciliation"],
)
def test_publish_worker_crash_recovery_never_creates_duplicate_intents(
    crash_point, monkeypatch
):
    user_id, token, organization_id = create_user_and_organization()
    run_id, channel_id, artifact_id = seed_approved_video(user_id, organization_id)
    response = client.post(
        (
            f"/api/v1/organizations/{organization_id}/production-runs/{run_id}"
            "/youtube-publish-jobs"
        ),
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": f"crash-recovery-{crash_point}",
        },
        json={
            "title": "Crash recovery test",
            "description": "A synthetic upload used to verify durable recovery.",
            "privacy_status": "public",
        },
    )
    assert response.status_code == 201
    job_id = response.json()["id"]
    video_bytes = b"synthetic crash-recovery MP4 bytes"
    video_sha256 = hashlib.sha256(video_bytes).hexdigest()
    encryption_key = "ab" * 32
    worker_settings = Settings(
        integration_token_encryption_key=encryption_key,
        integration_token_active_key_version=1,
        youtube_client_id="crash-test-client",
        youtube_client_secret="crash-test-secret",
    )

    async def prepare_worker_job():
        async with get_session_factory()() as session:
            channel = await session.get(YouTubeChannel, UUID(channel_id))
            artifact = await session.get(MediaArtifact, UUID(artifact_id))
            job = await session.get(YouTubePublishJob, UUID(job_id))
            assert channel is not None and artifact is not None and job is not None
            channel.encrypted_refresh_token = encrypt_refresh_token(
                "refresh-token-secret",
                encryption_key,
                UUID(organization_id),
                channel.channel_id,
                key_version=1,
            )
            channel.key_version = 1
            artifact.sha256 = video_sha256
            artifact.byte_size = len(video_bytes)
            job.sha256 = video_sha256
            job.byte_size = len(video_bytes)
            await session.commit()

    class FakeStorage:
        async def get(self, storage_key: str) -> bytes:
            assert storage_key.endswith(".mp4")
            return video_bytes

        async def aclose(self):
            return None

    class FakeUploadClient:
        def __init__(self):
            self.initiate_calls = 0
            self.upload_calls = 0
            self.query_calls = 0
            self.server_video_id = None
            self.crash_triggered = False
            self.provider_upload_completed = False

            def token_handler(request: httpx.Request) -> httpx.Response:
                assert str(request.url) == "https://oauth2.googleapis.com/token"
                return httpx.Response(
                    200,
                    json={
                        "access_token": "ephemeral-crash-access-token",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "refresh_token": "rotated-crash-refresh-token",
                    },
                )

            self.http_client = httpx.AsyncClient(
                transport=httpx.MockTransport(token_handler)
            )

        async def refresh_access_token(self, refresh_token: str) -> str:
            raise AssertionError("The worker must use the proxy-bound token service.")

        async def initiate_upload(self, **kwargs) -> str:
            self.initiate_calls += 1
            assert kwargs["privacy_status"] == "private"
            assert kwargs["total_bytes"] == len(video_bytes)
            return (
                "https://www.googleapis.com/upload/youtube/v3/videos"
                "?upload_id=opaque-crash-recovery-session"
            )

        async def upload_chunk(self, **kwargs) -> UploadChunkResult:
            self.upload_calls += 1
            assert kwargs["content"] == video_bytes
            if crash_point == "before_upload" and not self.crash_triggered:
                self.crash_triggered = True
                raise SystemExit("injected crash before provider upload")
            if (
                crash_point == "after_upload_before_reconciliation"
                and not self.provider_upload_completed
            ):
                self.provider_upload_completed = True
                self.server_video_id = "youtube-video-crash-recovery"
                return UploadChunkResult(
                    bytes_received=len(video_bytes),
                    video_id=self.server_video_id,
                )
            if crash_point == "after_reconciliation" and not self.provider_upload_completed:
                self.provider_upload_completed = True
                self.server_video_id = "youtube-video-crash-recovery"
                return UploadChunkResult(
                    bytes_received=len(video_bytes),
                    video_id=None,
                )
            self.server_video_id = "youtube-video-crash-recovery"
            return UploadChunkResult(
                bytes_received=len(video_bytes),
                video_id=self.server_video_id,
            )

        async def query_upload_status(self, **kwargs) -> UploadChunkResult:
            self.query_calls += 1
            return UploadChunkResult(
                bytes_received=len(video_bytes) if self.server_video_id else 0,
                video_id=self.server_video_id,
            )

        async def get_video_processing_status(
            self, *, access_token: str, video_id: str
        ) -> YouTubeVideoProcessingStatus:
            assert video_id == "youtube-video-crash-recovery"
            return YouTubeVideoProcessingStatus(
                upload_status="processed",
                processing_status="succeeded",
                privacy_status="private",
                published_at=None,
            )

        async def update_video_privacy_status(
            self, *, access_token: str, video_id: str, privacy_status: str
        ) -> str:
            assert video_id == "youtube-video-crash-recovery"
            assert privacy_status == "public"
            return "public"

    upload_client = FakeUploadClient()

    async def run_worker_once():
        claim = await claim_next_youtube_publish_job(
            get_session_factory(),
            worker_id=f"crash-recovery-{crash_point}",
            lease_seconds=300,
        )
        assert claim is not None
        claimed_id, lease_token = claim
        assert claimed_id == UUID(job_id)
        await process_claimed_youtube_publish_job(
            job_id=claimed_id,
            lease_token=lease_token,
            worker_id=f"crash-recovery-{crash_point}",
            session_factory=get_session_factory(),
            storage=FakeStorage(),
            upload_client=upload_client,
            settings=worker_settings,
        )

    original_persist = youtube_publish_worker._persist
    original_poll = youtube_publish_worker._poll_processing_status

    async def crash_before_reconciliation(*args, **kwargs):
        if kwargs.get("event_type") == "upload_completed" and not upload_client.crash_triggered:
            upload_client.crash_triggered = True
            raise SystemExit("injected crash after provider upload")
        return await original_persist(*args, **kwargs)

    async def crash_after_reconciliation(**kwargs):
        if not upload_client.crash_triggered:
            upload_client.crash_triggered = True
            raise SystemExit("injected crash after durable reconciliation")
        return await original_poll(**kwargs)

    async def inspect_crash_state():
        async with get_session_factory()() as session:
            job = await session.get(YouTubePublishJob, UUID(job_id))
            assert job is not None
            if crash_point == "after_reconciliation":
                assert job.status == "processing"
                assert job.youtube_video_id == "youtube-video-crash-recovery"
                assert job.upload_session_url_encrypted is None
            else:
                assert job.status == "uploading"
                assert job.youtube_video_id is None
                assert job.upload_session_url_encrypted is not None
            job.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
            job.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()

    async def verify_single_completed_intent():
        async with get_session_factory()() as session:
            jobs = await session.scalars(
                select(YouTubePublishJob).where(
                    YouTubePublishJob.production_run_id == UUID(run_id)
                )
            )
            all_jobs = jobs.all()
            assert len(all_jobs) == 1
            job = all_jobs[0]
            assert job.id == UUID(job_id)
            assert job.status == "published"
            assert job.youtube_video_id == "youtube-video-crash-recovery"
            assert job.bytes_uploaded == len(video_bytes)
            assert job.upload_session_url_encrypted is None
            assert job.lease_token is None
            events = await session.scalars(
                select(YouTubePublishEvent).where(
                    YouTubePublishEvent.publish_job_id == UUID(job_id)
                )
            )
            event_types = {event.event_type for event in events.all()}
            assert "video_published" in event_types
            if crash_point != "before_upload":
                assert "upload_completion_reconciled" in event_types

    async def exercise_recovery():
        await prepare_worker_job()
        try:
            if crash_point == "after_upload_before_reconciliation":
                monkeypatch.setattr(
                    youtube_publish_worker, "_persist", crash_before_reconciliation
                )
            elif crash_point == "after_reconciliation":
                monkeypatch.setattr(
                    youtube_publish_worker, "_poll_processing_status", crash_after_reconciliation
                )

            with pytest.raises(SystemExit):
                await run_worker_once()

            await inspect_crash_state()
            monkeypatch.setattr(youtube_publish_worker, "_persist", original_persist)
            monkeypatch.setattr(
                youtube_publish_worker, "_poll_processing_status", original_poll
            )

            await run_worker_once()
            await verify_single_completed_intent()
            assert upload_client.initiate_calls == 1
            assert upload_client.server_video_id == "youtube-video-crash-recovery"
            if crash_point in {
                "after_reconciliation",
                "after_upload_before_reconciliation",
            }:
                assert upload_client.upload_calls == 1
            if crash_point == "before_upload":
                assert upload_client.upload_calls == 2
        finally:
            await upload_client.http_client.aclose()

    try:
        asyncio.run(exercise_recovery())
    finally:
        cleanup_user(user_id)
