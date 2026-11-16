import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.main import app
from app.models.evidence_item import EvidenceItem
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.research_run import ResearchRun
from app.models.research_source import ResearchSource
from app.models.story_artifact import StoryArtifactRecord
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_analytics_review import YouTubeAnalyticsReview
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.services.production import PRODUCTION_STAGE_PLAN, create_production_run

client = TestClient(app)


def create_user_and_organization() -> tuple[str, str, str]:
    email = f"production-api-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Production API Test",
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
        json={"name": f"Production Test {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, token, organization.json()["id"]


def seed_research_run(
    user_id: str, organization_id: str, status: str = "completed"
) -> tuple[str, str]:
    async def seed() -> str:
        async with get_session_factory()() as session:
            run = ResearchRun(
                organization_id=UUID(organization_id),
                requested_by_user_id=UUID(user_id),
                topic="Grid-scale battery storage",
                status=status,
            )
            channel = YouTubeChannel(
                organization_id=UUID(organization_id),
                channel_id=f"UC-test-{uuid4().hex[:16]}",
                title="Production API Test Channel",
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
                    delete(OrganizationMember).where(OrganizationMember.user_id == user_id)
                )
                await session.execute(
                    delete(Organization).where(Organization.created_by_user_id == user_id)
                )
                await session.execute(delete(User).where(User.id == user_id))

    asyncio.run(cleanup())


def test_create_production_run_requires_authentication() -> None:
    response = client.post(
        f"/api/v1/organizations/{uuid4()}/production-runs",
        headers={"Idempotency-Key": "unauthenticated"},
        json={"research_run_id": str(uuid4()), "youtube_channel_id": str(uuid4())},
    )
    assert response.status_code == 401


def test_production_readiness_reports_configured_and_missing_prerequisites(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.v1.endpoints.production.get_settings",
        lambda: Settings(_env_file=None),
    )
    user_id, token, organization_id = create_user_and_organization()
    try:
        seed_research_run(user_id, organization_id)
        response = client.get(
            f"/api/v1/organizations/{organization_id}/production-readiness",
            headers={"Authorization": f"Bearer {token}"},
        )

        assert response.status_code == 200
        payload = response.json()
        checks = {check["key"]: check for check in payload["checks"]}
        assert payload["summary"] in {"attention", "blocked"}
        assert checks["channel"]["status"] == "attention"
        assert checks["channel"]["detail"].endswith("read-only access.")
        assert checks["youtube_analytics"]["status"] == "attention"
        assert "YouTube read-only" in checks["youtube_analytics"]["detail"]
        assert checks["story"]["status"] == "blocked"
        assert checks["media"]["status"] == "blocked"
        assert checks["comfyui_connectivity"]["status"] == "attention"
        assert "ComfyUI is not configured" in checks["comfyui_connectivity"]["detail"]
        assert checks["publish_consent"]["status"] == "attention"

        monkeypatch.setattr(
            "app.api.v1.endpoints.production.get_settings",
            lambda: Settings(
                _env_file=None,
                media_visual_provider="comfyui",
                media_comfyui_workflow_path="/tmp/aurora-missing-comfy-workflow.json",
                media_comfyui_output_node_id="9",
                story_worker_llm_base_url=None,
                media_tts_base_url=None,
            ),
        )
        verified = client.post(
            f"/api/v1/organizations/{organization_id}/production-readiness/verify",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert verified.status_code == 200
        comfy = next(
            check
            for check in verified.json()["checks"]
            if check["key"] == "comfyui_connectivity"
        )
        assert comfy["status"] == "blocked"
        assert "workflow file or output node is missing" in comfy["detail"]
    finally:
        cleanup_user(user_id)


def test_analytics_review_decision_is_tenant_and_channel_scoped() -> None:
    user_id, token, organization_id = create_user_and_organization()
    _, channel_id = seed_research_run(user_id, organization_id)

    async def seed_review() -> str:
        async with get_session_factory()() as session:
            review = YouTubeAnalyticsReview(
                organization_id=UUID(organization_id),
                youtube_channel_id=UUID(channel_id),
                current_start_date=date(2026, 11, 8),
                current_end_date=date(2026, 11, 14),
                previous_start_date=date(2026, 11, 1),
                previous_end_date=date(2026, 11, 7),
                status="pending",
                suggestion="Review the story opening.",
                evidence_metrics={"views_change_percent": 12.0},
            )
            session.add(review)
            await session.commit()
            return str(review.id)

    review_id = asyncio.run(seed_review())
    url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/{channel_id}"
        f"/analytics-reviews/{review_id}"
    )

    try:
        response = client.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"decision": "approved"},
        )
        wrong_channel = client.post(
            f"/api/v1/organizations/{organization_id}/youtube-channels/{uuid4()}"
            f"/analytics-reviews/{review_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"decision": "dismissed"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "approved"
        assert response.json()["reviewed_by_user_id"] == user_id
        assert wrong_channel.status_code == 404
    finally:
        cleanup_user(user_id)


def test_create_production_run_is_idempotent_and_persists_stage_plan() -> None:
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "daily-video-2026-11-12",
    }
    url = f"/api/v1/organizations/{organization_id}/production-runs"

    try:
        payload = {
            "research_run_id": research_run_id,
            "youtube_channel_id": channel_id,
        }
        first = client.post(url, headers=headers, json=payload)
        replay = client.post(url, headers=headers, json=payload)

        assert first.status_code == 201
        assert replay.status_code == 201
        assert first.json()["id"] == replay.json()["id"]
        assert first.json()["organization_id"] == organization_id
        assert first.json()["research_run_id"] == research_run_id
        assert first.json()["status"] == "queued"
        assert {stage["stage_key"] for stage in first.json()["stages"]} == set(
            PRODUCTION_STAGE_PLAN
        )
        assert all(stage["status"] == "pending" for stage in first.json()["stages"])

        async def verify_one_run() -> None:
            async with get_session_factory()() as session:
                runs = await session.scalars(
                    select(ProductionRun).where(
                        ProductionRun.organization_id == UUID(organization_id)
                    )
                )
                assert len(runs.all()) == 1
                stages = await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == UUID(first.json()["id"])
                    )
                )
                assert len(stages.all()) == len(PRODUCTION_STAGE_PLAN)

        asyncio.run(verify_one_run())
    finally:
        cleanup_user(user_id)


def test_create_production_run_rejects_incomplete_research() -> None:
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id, status="researching")
    try:
        response = client.post(
            f"/api/v1/organizations/{organization_id}/production-runs",
            headers={
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "incomplete-research",
            },
            json={"research_run_id": research_run_id, "youtube_channel_id": channel_id},
        )
        assert response.status_code == 409
    finally:
        cleanup_user(user_id)


def test_production_run_api_does_not_disclose_other_tenant() -> None:
    owner_id, owner_token, owner_org_id = create_user_and_organization()
    outsider_id, outsider_token, outsider_org_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(owner_id, owner_org_id)
    try:
        created = client.post(
            f"/api/v1/organizations/{owner_org_id}/production-runs",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": "private-production-run",
            },
            json={"research_run_id": research_run_id, "youtube_channel_id": channel_id},
        )
        assert created.status_code == 201
        response = client.get(
            f"/api/v1/organizations/{outsider_org_id}/production-runs/{created.json()['id']}",
            headers={"Authorization": f"Bearer {outsider_token}"},
        )
        assert response.status_code == 404
    finally:
        cleanup_user(outsider_id)
        cleanup_user(owner_id)


def test_cancelling_queued_production_run_cancels_pending_stages() -> None:
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "cancel-before-worker",
    }
    base_url = f"/api/v1/organizations/{organization_id}/production-runs"

    try:
        created = client.post(
            base_url,
            headers=headers,
            json={"research_run_id": research_run_id, "youtube_channel_id": channel_id},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]
        cancelled = client.post(
            f"{base_url}/{run_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"

        details = client.get(
            f"{base_url}/{run_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert details.status_code == 200
        assert all(stage["status"] == "cancelled" for stage in details.json()["stages"])
    finally:
        cleanup_user(user_id)



def test_production_run_requires_explicit_owner_approval_after_review() -> None:
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    base_url = f"/api/v1/organizations/{organization_id}/production-runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "human-review-gate",
    }

    try:
        created = client.post(
            base_url,
            headers=headers,
            json={"research_run_id": research_run_id, "youtube_channel_id": channel_id},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        early_approval = client.post(
            f"{base_url}/{run_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert early_approval.status_code == 409

        async def complete_review_stages() -> None:
            async with get_session_factory()() as session:
                run = await session.get(ProductionRun, run_id)
                assert run is not None
                run.status = "awaiting_review"
                stages = await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == UUID(run_id)
                    )
                )
                for stage in stages:
                    stage.status = "completed"
                    if stage.stage_key == "quality_check":
                        stage.output_manifest = {
                            "publication_blocked": True,
                            "quality_report": {"is_valid": True},
                        }
                await session.commit()

        asyncio.run(complete_review_stages())
        blocked_approval = client.post(
            f"{base_url}/{run_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert blocked_approval.status_code == 409

        async def clear_publication_block() -> None:
            async with get_session_factory()() as session:
                quality_stage = await session.scalar(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == UUID(run_id),
                        ProductionStage.stage_key == "quality_check",
                    )
                )
                assert quality_stage is not None
                quality_stage.output_manifest = {
                    "publication_blocked": False,
                    "quality_report": {"is_valid": True},
                }
                await session.commit()

        asyncio.run(clear_publication_block())
        approved = client.post(
            f"{base_url}/{run_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        assert approved.json()["approved_by_user_id"] == user_id
        assert approved.json()["approved_at"] is not None
        assert approved.json()["approval_mode"] == "human"
        assert approved.json()["approval_consent_event_id"] is None
    finally:
        cleanup_user(user_id)


def test_editor_can_reject_rendered_run_and_preserve_rejection_history() -> None:
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "editorial-rejection-test",
    }
    base_url = f"/api/v1/organizations/{organization_id}/production-runs"

    try:
        created = client.post(
            base_url,
            headers=headers,
            json={"research_run_id": research_run_id, "youtube_channel_id": channel_id},
        )
        assert created.status_code == 201
        run_id = created.json()["id"]

        async def mark_ready_for_review() -> None:
            async with get_session_factory()() as session:
                run = await session.get(ProductionRun, run_id)
                assert run is not None
                run.status = "awaiting_review"
                await session.commit()

        asyncio.run(mark_ready_for_review())
        rejected = client.post(
            f"{base_url}/{run_id}/reject",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "The scene choices do not match the narration."},
        )

        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["rejected_by_user_id"] == user_id
        assert rejected.json()["rejected_at"] is not None
        assert rejected.json()["rejection_reason"] == (
            "The scene choices do not match the narration."
        )
        rejected_again = client.post(
            f"{base_url}/{run_id}/reject",
            headers={"Authorization": f"Bearer {token}"},
            json={"reason": "Second decision should not change history."},
        )
        assert rejected_again.status_code == 409
    finally:
        cleanup_user(user_id)


def _profile_payload(**overrides):
    profile = {
        "niche": "technology",
        "topic_boundaries": ["AI research", "computer science"],
        "target_audience": "curious adults",
        "language": "en",
        "tone": "clear and evidence-led",
        "format": "educational",
        "prohibited_topics": ["unverified medical claims"],
        "additional_constraints": ["Cite primary sources"],
    }
    profile.update(overrides)
    return profile


def test_channel_editorial_profile_updates_create_immutable_versions():
    user_id, token, organization_id = create_user_and_organization()
    _, channel_id = seed_research_run(user_id, organization_id)
    url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/"
        f"{channel_id}/profile"
    )
    headers = {"Authorization": f"Bearer {token}"}

    try:
        first = client.put(url, headers=headers, json=_profile_payload())
        second = client.put(
            url,
            headers=headers,
            json=_profile_payload(tone="calm documentary narration"),
        )
        current = client.get(url, headers=headers)

        assert first.status_code == 200
        assert first.json()["version"] == 2
        assert second.status_code == 200
        assert second.json()["version"] == 3
        assert current.status_code == 200
        assert current.json()["version"] == 3
        assert current.json()["profile"]["tone"] == "calm documentary narration"

        async def inspect_versions() -> None:
            async with get_session_factory()() as session:
                versions = await session.scalars(
                    select(YouTubeChannelProfile)
                    .where(
                        YouTubeChannelProfile.organization_id == UUID(organization_id),
                        YouTubeChannelProfile.youtube_channel_id == UUID(channel_id),
                    )
                    .order_by(YouTubeChannelProfile.version)
                )
                records = versions.all()
                assert [record.version for record in records] == [1, 2, 3]
                assert records[1].profile["tone"] == "clear and evidence-led"
                assert records[2].profile["tone"] == "calm documentary narration"

        asyncio.run(inspect_versions())
    finally:
        cleanup_user(user_id)


def test_daily_schedule_requires_publish_consent_and_persists_timezone():
    user_id, token, organization_id = create_user_and_organization()
    _, channel_id = seed_research_run(user_id, organization_id)
    url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/"
        f"{channel_id}/schedule"
    )
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "enabled": True,
        "timezone": "Asia/Kolkata",
        "posting_time": "09:30:00",
        "catch_up_policy": "run_once",
    }

    try:
        denied = client.put(url, headers=headers, json=payload)
        assert denied.status_code == 409
        assert "publishing consent" in denied.json()["detail"]

        async def grant_publish_scope() -> None:
            async with get_session_factory()() as session:
                channel = await session.get(YouTubeChannel, UUID(channel_id))
                assert channel is not None
                channel.granted_scopes = (
                    "https://www.googleapis.com/auth/youtube.readonly "
                    "https://www.googleapis.com/auth/youtube.upload "
                    "https://www.googleapis.com/auth/youtube.force-ssl"
                )
                await session.commit()

        asyncio.run(grant_publish_scope())
        enabled = client.put(url, headers=headers, json=payload)
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True
        assert enabled.json()["timezone"] == "Asia/Kolkata"
        assert enabled.json()["posting_time"] == "09:30:00"
        assert enabled.json()["catch_up_policy"] == "run_once"
        assert enabled.json()["next_run_at"] is not None

        disabled = client.put(
            url,
            headers=headers,
            json={**payload, "enabled": False},
        )
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False
        assert disabled.json()["next_run_at"] is None
    finally:
        cleanup_user(user_id)


def test_production_run_keeps_immutable_channel_profile_snapshot():
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    profile_url = (
        f"/api/v1/organizations/{organization_id}/youtube-channels/"
        f"{channel_id}/profile"
    )
    run_url = f"/api/v1/organizations/{organization_id}/production-runs"
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "profile-snapshot-run",
    }

    try:
        profile = client.put(
            profile_url,
            headers=headers,
            json=_profile_payload(tone="version-two tone"),
        )
        assert profile.status_code == 200
        created = client.post(
            run_url,
            headers=headers,
            json={
                "research_run_id": research_run_id,
                "youtube_channel_id": channel_id,
            },
        )
        assert created.status_code == 201
        assert created.json()["channel_profile_version"] == 2
        assert created.json()["channel_profile_snapshot"]["tone"] == "version-two tone"
        assert created.json()["channel_profile_snapshot"]["_workspace_preferences"][
            "target_duration_seconds"
        ] == 30

        newer_profile = client.put(
            profile_url,
            headers=headers,
            json=_profile_payload(tone="version-three tone"),
        )
        assert newer_profile.status_code == 200
        details = client.get(
            f"{run_url}/{created.json()['id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert details.status_code == 200
        assert details.json()["channel_profile_version"] == 2
        assert details.json()["channel_profile_snapshot"]["tone"] == "version-two tone"
    finally:
        cleanup_user(user_id)


def test_workspace_video_switch_blocks_new_production_runs():
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    headers = {"Authorization": f"Bearer {token}"}
    settings_url = f"/api/v1/organizations/{organization_id}/studio-settings"
    run_url = f"/api/v1/organizations/{organization_id}/production-runs"
    try:
        current = client.get(settings_url, headers=headers)
        assert current.status_code == 200
        paused = client.put(
            settings_url,
            headers=headers,
            json={
                **current.json()["settings"],
                "video_generation_enabled": False,
            },
        )
        assert paused.status_code == 200
        response = client.post(
            run_url,
            headers={**headers, "Idempotency-Key": "video-generation-paused"},
            json={
                "research_run_id": research_run_id,
                "youtube_channel_id": channel_id,
            },
        )
        assert response.status_code == 409
        assert "paused" in response.json()["detail"]
    finally:
        cleanup_user(user_id)


def test_production_run_rejects_channel_from_another_workspace():
    owner_id, owner_token, owner_org_id = create_user_and_organization()
    outsider_id, _, outsider_org_id = create_user_and_organization()
    research_run_id, _ = seed_research_run(owner_id, owner_org_id)
    _, outsider_channel_id = seed_research_run(outsider_id, outsider_org_id)

    try:
        response = client.post(
            f"/api/v1/organizations/{owner_org_id}/production-runs",
            headers={
                "Authorization": f"Bearer {owner_token}",
                "Idempotency-Key": "cross-tenant-channel",
            },
            json={
                "research_run_id": research_run_id,
                "youtube_channel_id": outsider_channel_id,
            },
        )
        assert response.status_code == 409
        assert "active YouTube channel" in response.json()["detail"]
    finally:
        cleanup_user(outsider_id)
        cleanup_user(owner_id)


def test_database_prevents_duplicate_daily_runs_for_one_channel():
    user_id, _, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)
    scheduled_date = date(2026, 11, 12)

    try:
        async def create_scheduled_run(idempotency_key: str) -> None:
            async with get_session_factory()() as session:
                run = await create_production_run(
                    session,
                    organization_id=UUID(organization_id),
                    requested_by_user_id=UUID(user_id),
                    research_run_id=UUID(research_run_id),
                    youtube_channel_id=UUID(channel_id),
                    idempotency_key=idempotency_key,
                    scheduled_local_date=scheduled_date,
                    scheduled_for=datetime(2026, 11, 12, 3, 30, tzinfo=UTC),
                )
                assert run.scheduled_local_date == scheduled_date
                await session.commit()

        asyncio.run(create_scheduled_run("daily-channel-run-one"))

        async def attempt_duplicate() -> None:
            async with get_session_factory()() as session:
                try:
                    await create_production_run(
                        session,
                        organization_id=UUID(organization_id),
                        requested_by_user_id=UUID(user_id),
                        research_run_id=UUID(research_run_id),
                        youtube_channel_id=UUID(channel_id),
                        idempotency_key="daily-channel-run-two",
                        scheduled_local_date=scheduled_date,
                        scheduled_for=datetime(2026, 11, 12, 3, 30, tzinfo=UTC),
                    )
                except IntegrityError:
                    await session.rollback()
                    return
                raise AssertionError("Duplicate channel/day production run was accepted.")

        asyncio.run(attempt_duplicate())
    finally:
        cleanup_user(user_id)


def test_story_artifact_review_endpoint_checks_tenant_scope_schema_and_hash():
    user_id, token, organization_id = create_user_and_organization()
    research_run_id, channel_id = seed_research_run(user_id, organization_id)

    source_id = uuid4()
    evidence_id = uuid4()
    payload = {
        "schema_version": "1.0.0",
        "title": "A better battery",
        "hook": "What if a battery could store power for days?",
        "beats": [
            {"heading": "The challenge", "summary": "Renewables need reliable storage."},
            {"heading": "The solution", "summary": "Long-duration batteries can help."},
        ],
        "narration": "Renewable power is growing. Long-duration batteries can help store it.",
        "visual_direction": "Show a grid battery beside wind turbines.",
        "scenes": [
            {
                "visual_direction": "Wind turbines at sunset.",
                "narration_segment": "Renewable power is growing.",
                "duration_seconds": 15,
            },
            {
                "visual_direction": "Large battery containers connected to a grid.",
                "narration_segment": "Long-duration batteries can help store it.",
                "duration_seconds": 15,
            },
        ],
        "target_duration_seconds": 30,
        "word_budget": 70,
        "source_references": [
            {"source_id": str(source_id), "evidence_ids": [str(evidence_id)]}
        ],
        "factual_claims": [
            {
                "claim": "Long-duration batteries can store renewable power.",
                "verification_status": "supported",
                "source_ids": [str(source_id)],
                "evidence": [
                    {
                        "source_id": str(source_id),
                        "evidence_id": str(evidence_id),
                        "relation": "supports",
                    }
                ],
                "model_confidence": 0.91,
            }
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    content_hash = hashlib.sha256(canonical).hexdigest()

    try:
        async def seed_artifact() -> tuple[str, str]:
            async with get_session_factory()() as session:
                run = await create_production_run(
                    session,
                    organization_id=UUID(organization_id),
                    requested_by_user_id=UUID(user_id),
                    research_run_id=UUID(research_run_id),
                    youtube_channel_id=UUID(channel_id),
                    idempotency_key="story-review-endpoint",
                )
                stage = await session.scalar(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == run.id,
                        ProductionStage.stage_key == "story_generation",
                    )
                )
                assert stage is not None
                source = ResearchSource(
                    id=source_id,
                    research_run_id=UUID(research_run_id),
                    canonical_url="https://example.com/battery-storage",
                    title="Long-duration storage overview",
                    publisher="Example Energy Institute",
                    source_type="paper",
                )
                session.add(source)
                await session.flush()
                session.add(
                    EvidenceItem(
                        id=evidence_id,
                        research_source_id=source.id,
                        excerpt=(
                            "Long-duration storage can shift renewable electricity across time."
                        ),
                        content_hash="a" * 64,
                        locator="Executive summary",
                    )
                )
                record = StoryArtifactRecord(
                    organization_id=UUID(organization_id),
                    production_run_id=run.id,
                    production_stage_id=stage.id,
                    artifact_type="story_draft",
                    schema_version="1.0.0",
                    version=1,
                    content_hash=content_hash,
                    payload=payload,
                )
                session.add(record)
                await session.commit()
                return str(run.id), str(record.id)

        production_run_id, artifact_id = asyncio.run(seed_artifact())
        headers = {"Authorization": f"Bearer {token}"}
        url = (
            f"/api/v1/organizations/{organization_id}/production-runs/"
            f"{production_run_id}/story-artifacts"
        )

        response = client.get(url, headers=headers)
        assert response.status_code == 200
        artifacts = response.json()
        assert len(artifacts) == 1
        assert artifacts[0]["id"] == artifact_id
        assert artifacts[0]["artifact_type"] == "story_draft"
        assert artifacts[0]["payload"]["title"] == "A better battery"
        assert artifacts[0]["evidence_context"][0]["title"] == "Long-duration storage overview"
        assert artifacts[0]["evidence_context"][0]["evidence"][0]["excerpt"].startswith(
            "Long-duration storage can shift"
        )

        foreign = client.get(
            f"/api/v1/organizations/{uuid4()}/production-runs/"
            f"{production_run_id}/story-artifacts",
            headers=headers,
        )
        assert foreign.status_code in (403, 404)

        async def tamper_with_artifact() -> None:
            async with get_session_factory()() as session:
                record = await session.get(StoryArtifactRecord, artifact_id)
                assert record is not None
                record.payload = {**record.payload, "title": "Tampered"}
                await session.commit()

        asyncio.run(tamper_with_artifact())
        integrity_failure = client.get(url, headers=headers)
        assert integrity_failure.status_code == 409
        assert "integrity check" in integrity_failure.json()["detail"]
    finally:
        cleanup_user(user_id)
