import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.main import app
from app.models.email_outbox import EmailOutbox
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.research_run import ResearchRun
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.research.providers.mock import MockResearchProvider
from app.research.trend_ranking import TrendObservation
from app.services.channel_schedule_dispatcher import (
    _build_research_topic,
    _discover_trend_observations,
    dispatch_due_channel_schedules_once,
)
from app.workers.research_worker import claim_research_batch, process_research_run

client = TestClient(app)


def create_user_and_organization() -> tuple[str, str, str]:
    email = f"schedule-dispatch-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"
    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "display_name": "Schedule Dispatcher Test",
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
        json={"name": f"Schedule Dispatcher {uuid4().hex[:8]}"},
    )
    assert organization.status_code == 201
    return user_id, token, organization.json()["id"]


def cleanup_user(user_id: str) -> None:
    async def cleanup() -> None:
        async with get_session_factory()() as session:
            user = await session.get(User, user_id)
            if user is None:
                return
            await session.execute(
                delete(EmailOutbox).where(EmailOutbox.recipient == user.email)
            )
            organization_ids = list(
                await session.scalars(
                    select(Organization.id).where(
                        Organization.created_by_user_id == UUID(user_id)
                    )
                )
            )
            for organization_id in organization_ids:
                await session.execute(
                    delete(ProductionRun).where(
                        ProductionRun.organization_id == organization_id
                    )
                )
                await session.execute(
                    delete(ResearchRun).where(
                        ResearchRun.organization_id == organization_id
                    )
                )
                await session.execute(
                    delete(OrganizationMember).where(
                        OrganizationMember.organization_id == organization_id
                    )
                )
                await session.execute(
                    delete(Organization).where(Organization.id == organization_id)
                )
            await session.delete(user)
            await session.commit()

    asyncio.run(cleanup())


def seed_channel_schedule(
    user_id: str,
    organization_id: str,
    *,
    catch_up_policy: str = "run_once",
    due_at: datetime | None = None,
    granted_scopes: str | None = None,
) -> tuple[str, str]:
    async def seed() -> tuple[str, str]:
        async with get_session_factory()() as session:
            channel = YouTubeChannel(
                organization_id=UUID(organization_id),
                channel_id=f"UC-schedule-{uuid4().hex[:16]}",
                title="Scheduled Test Channel",
                encrypted_refresh_token="test-encrypted-refresh-token",
                key_version=1,
                granted_scopes=granted_scopes
                or (
                    "https://www.googleapis.com/auth/youtube.readonly "
                    "https://www.googleapis.com/auth/youtube.upload"
                ),
                connected_by_user_id=UUID(user_id),
            )
            session.add(channel)
            await session.flush()
            profile = YouTubeChannelProfile(
                organization_id=UUID(organization_id),
                youtube_channel_id=channel.id,
                version=1,
                profile={
                    "niche": "space science",
                    "topic_boundaries": ["astronomy", "space exploration"],
                    "target_audience": "curious adults",
                    "language": "en",
                    "tone": "documentary",
                    "format": "educational",
                    "prohibited_topics": ["unsupported medical claims"],
                    "additional_constraints": ["Explain technical terms"],
                },
                created_by_user_id=UUID(user_id),
            )
            schedule = YouTubeChannelSchedule(
                organization_id=UUID(organization_id),
                youtube_channel_id=channel.id,
                enabled=True,
                timezone="UTC",
                posting_time=datetime.now(UTC).time().replace(tzinfo=None),
                catch_up_policy=catch_up_policy,
                next_run_at=due_at or datetime.now(UTC) - timedelta(minutes=5),
                updated_by_user_id=UUID(user_id),
            )
            session.add_all([profile, schedule])
            await session.commit()
            return str(channel.id), str(profile.id)

    return asyncio.run(seed())


def test_due_schedule_creates_channel_bound_research_and_production_atomically(monkeypatch):
    monkeypatch.setattr(
        "app.services.channel_schedule_dispatcher.get_settings",
        lambda: Settings(_env_file=None),
    )
    user_id, _, organization_id = create_user_and_organization()
    channel_id, profile_id = seed_channel_schedule(user_id, organization_id)

    try:
        summary = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert summary.dispatched == 1

        async def get_research_run() -> ResearchRun:
            async with get_session_factory()() as session:
                return await session.scalar(
                    select(ResearchRun).where(
                        ResearchRun.organization_id == UUID(organization_id),
                        ResearchRun.youtube_channel_id == UUID(channel_id),
                    )
                )

        research_run = asyncio.run(get_research_run())
        assert research_run is not None
        target_publish_at = research_run.scheduled_for
        assert target_publish_at is not None
        assert research_run.status == "queued"
        assert research_run.channel_profile_id == UUID(profile_id)
        assert research_run.channel_profile_version == 1
        assert research_run.channel_profile_snapshot["niche"] == "space science"
        assert "Focus: astronomy, space exploration" in research_run.topic

        async def create_new_profile_version() -> None:
            async with get_session_factory()() as session:
                session.add(
                    YouTubeChannelProfile(
                        organization_id=UUID(organization_id),
                        youtube_channel_id=UUID(channel_id),
                        version=2,
                        profile={
                            **research_run.channel_profile_snapshot,
                            "tone": "new profile version",
                        },
                        created_by_user_id=UUID(user_id),
                    )
                )
                await session.commit()

        asyncio.run(create_new_profile_version())

        async def process_research() -> bool:
            claimed = await claim_research_batch(
                "schedule-dispatch-test-worker",
                session_factory=get_session_factory(),
            )
            selected = next(run for run in claimed if run.id == research_run.id)
            return await process_research_run(
                selected,
                MockResearchProvider(),
                session_factory=get_session_factory(),
            )

        assert asyncio.run(process_research()) is True

        async def inspect_production() -> tuple[ProductionRun, ResearchRun, YouTubeChannelSchedule]:
            async with get_session_factory()() as session:
                production = await session.scalar(
                    select(ProductionRun).where(
                        ProductionRun.organization_id == UUID(organization_id),
                        ProductionRun.youtube_channel_id == UUID(channel_id),
                    )
                )
                completed_research = await session.get(ResearchRun, research_run.id)
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert production is not None
                assert completed_research is not None
                assert schedule is not None
                return production, completed_research, schedule

        production, completed_research, schedule = asyncio.run(inspect_production())
        assert completed_research.status == "completed"
        assert production.research_run_id == research_run.id
        assert production.channel_profile_id == UUID(profile_id)
        assert production.channel_profile_version == 1
        assert production.channel_profile_snapshot["tone"] == "documentary"
        assert production.scheduled_local_date == research_run.scheduled_local_date
        assert schedule.last_scheduled_local_date == research_run.scheduled_local_date
        assert schedule.next_run_at > datetime.now(UTC)

        async def rewind_duplicate_schedule():
            async with get_session_factory()() as session:
                async with session.begin():
                    schedule = await session.scalar(
                        select(YouTubeChannelSchedule).where(
                            YouTubeChannelSchedule.organization_id == UUID(organization_id),
                            YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                        )
                    )
                    assert schedule is not None
                    schedule.next_run_at = target_publish_at
                    schedule.last_scheduled_local_date = target_publish_at.date()

        asyncio.run(rewind_duplicate_schedule())
        duplicate = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert duplicate.dispatched == 0
        assert duplicate.skipped == 1

        async def verify_advanced_again():
            async with get_session_factory()() as session:
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert schedule is not None
                assert schedule.next_run_at > target_publish_at

        asyncio.run(verify_advanced_again())
    finally:
        cleanup_user(user_id)


def test_concurrent_dispatchers_create_only_one_research_run():
    user_id, _, organization_id = create_user_and_organization()
    channel_id, _ = seed_channel_schedule(user_id, organization_id)

    try:
        async def dispatch_concurrently():
            return await asyncio.gather(
                dispatch_due_channel_schedules_once(session_factory=get_session_factory()),
                dispatch_due_channel_schedules_once(session_factory=get_session_factory()),
            )

        first, second = asyncio.run(dispatch_concurrently())
        assert first.dispatched + second.dispatched == 1

        async def count_runs() -> int:
            async with get_session_factory()() as session:
                return await session.scalar(
                    select(func.count(ResearchRun.id)).where(
                        ResearchRun.organization_id == UUID(organization_id),
                        ResearchRun.youtube_channel_id == UUID(channel_id),
                    )
                )

        assert asyncio.run(count_runs()) == 1
    finally:
        cleanup_user(user_id)


def test_skip_policy_drops_missed_local_day_and_advances_schedule():
    user_id, _, organization_id = create_user_and_organization()
    channel_id, _ = seed_channel_schedule(
        user_id,
        organization_id,
        catch_up_policy="skip",
        due_at=datetime.now(UTC) - timedelta(days=2),
    )

    try:
        summary = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert summary.skipped == 1
        assert summary.dispatched == 0

        async def inspect() -> tuple[int, YouTubeChannelSchedule]:
            async with get_session_factory()() as session:
                count = await session.scalar(
                    select(func.count(ResearchRun.id)).where(
                        ResearchRun.organization_id == UUID(organization_id),
                        ResearchRun.youtube_channel_id == UUID(channel_id),
                    )
                )
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert schedule is not None
                return count, schedule

        count, schedule = asyncio.run(inspect())
        assert count == 0
        assert schedule.last_scheduled_local_date == datetime.now(UTC).date()
        assert schedule.next_run_at > datetime.now(UTC)
    finally:
        cleanup_user(user_id)


def test_dispatch_disables_schedule_when_publishing_consent_is_revoked():
    user_id, _, organization_id = create_user_and_organization()
    channel_id, _ = seed_channel_schedule(
        user_id,
        organization_id,
        granted_scopes="https://www.googleapis.com/auth/youtube.readonly",
    )

    try:
        summary = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert summary.disabled == 1
        assert summary.dispatched == 0

        async def inspect() -> YouTubeChannelSchedule:
            async with get_session_factory()() as session:
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert schedule is not None
                return schedule

        schedule = asyncio.run(inspect())
        assert schedule.enabled is False
        assert schedule.next_run_at is None
    finally:
        cleanup_user(user_id)


def test_pausing_schedule_before_research_completion_prevents_production_run():
    user_id, _, organization_id = create_user_and_organization()
    channel_id, _ = seed_channel_schedule(user_id, organization_id)

    try:
        summary = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert summary.dispatched == 1

        async def pause_schedule_and_get_run():
            async with get_session_factory()() as session:
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                research = await session.scalar(
                    select(ResearchRun).where(
                        ResearchRun.organization_id == UUID(organization_id),
                        ResearchRun.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert schedule is not None
                assert research is not None
                schedule.enabled = False
                schedule.next_run_at = None
                await session.commit()
                return research.id

        research_run_id = asyncio.run(pause_schedule_and_get_run())

        async def process_research() -> bool:
            claimed = await claim_research_batch(
                "paused-schedule-test-worker",
                session_factory=get_session_factory(),
            )
            selected = next(run for run in claimed if run.id == research_run_id)
            return await process_research_run(
                selected,
                MockResearchProvider(),
                session_factory=get_session_factory(),
            )

        assert asyncio.run(process_research()) is True

        async def count_production_runs() -> int:
            async with get_session_factory()() as session:
                return await session.scalar(
                    select(func.count(ProductionRun.id)).where(
                        ProductionRun.organization_id == UUID(organization_id),
                        ProductionRun.youtube_channel_id == UUID(channel_id),
                    )
                )

        assert asyncio.run(count_production_runs()) == 0
    finally:
        cleanup_user(user_id)


def test_schedule_dispatch_uses_channel_relevant_trend_lead():
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    profile = {
        "niche": "space science",
        "topic_boundaries": ["astronomy", "space exploration"],
        "target_audience": "curious adults",
        "language": "en",
        "prohibited_topics": ["unsupported medical claims"],
    }
    observations = (
        TrendObservation(
            provider="hacker_news",
            topic="New astronomy telescope discovery",
            observed_at=now - timedelta(hours=1),
            retrieved_at=now,
            momentum_score=0.95,
        ),
    )

    selected = _build_research_topic(
        profile,
        recent_topics=(),
        trend_observations=observations,
        now=now,
    )

    assert selected == "New astronomy telescope discovery"


def test_schedule_dispatch_falls_back_to_profile_topics_when_trends_are_irrelevant():
    now = datetime(2026, 11, 12, 12, tzinfo=UTC)
    profile = {
        "niche": "space science",
        "topic_boundaries": ["astronomy", "space exploration"],
        "target_audience": "curious adults",
        "language": "en",
    }
    observations = (
        TrendObservation(
            provider="hacker_news",
            topic="Cooking recipes and kitchen appliances",
            observed_at=now - timedelta(hours=1),
            retrieved_at=now,
            momentum_score=0.95,
        ),
    )

    selected = _build_research_topic(
        profile,
        recent_topics=(),
        trend_observations=observations,
        now=now,
    )

    assert selected.startswith("space science")



def test_curated_rss_provider_is_configured_and_discovered_once(monkeypatch):
    from app.research.providers.curated_rss import CuratedRssTrendProvider

    observed = (
        TrendObservation(
            provider="curated_rss",
            topic="Astronomy observatory publishes new space image",
            observed_at=datetime(2026, 11, 12, 11, tzinfo=UTC),
            retrieved_at=datetime(2026, 11, 12, 12, tzinfo=UTC),
            publisher="Example Science",
        ),
    )
    calls = []

    async def fake_discover(self):
        calls.append(self._feeds)
        return observed

    monkeypatch.setattr(CuratedRssTrendProvider, "discover", fake_discover)
    settings = Settings(
        _env_file=None,
        trend_discovery_provider="curated_rss",
        trend_discovery_feeds=[
            {"url": "https://science.example.com/rss.xml", "publisher": "Example Science"}
        ],
    )

    result = asyncio.run(_discover_trend_observations(settings))

    assert result == observed
    assert len(calls) == 1
    assert calls[0][0].url == "https://science.example.com/rss.xml"
    assert calls[0][0].publisher == "Example Science"


def test_trend_discovery_provider_failure_falls_back_without_raising(monkeypatch):
    from app.research.providers.curated_rss import CuratedRssTrendProvider

    async def failed_discover(self):
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr(CuratedRssTrendProvider, "discover", failed_discover)
    settings = Settings(
        _env_file=None,
        trend_discovery_provider="curated_rss",
        trend_discovery_feeds=[
            {"url": "https://science.example.com/rss.xml", "publisher": "Example Science"}
        ],
    )

    assert asyncio.run(_discover_trend_observations(settings)) == ()

def test_schedule_dispatches_ahead_of_publish_time_and_advances_to_next_day(monkeypatch):
    user_id, _, organization_id = create_user_and_organization()
    target_publish_at = datetime.now(UTC) + timedelta(minutes=30)
    channel_id, _ = seed_channel_schedule(
        user_id,
        organization_id,
        due_at=target_publish_at,
    )
    monkeypatch.setattr(
        "app.services.channel_schedule_dispatcher.get_settings",
        lambda: Settings(_env_file=None, channel_schedule_lead_minutes=60),
    )

    try:
        summary = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert summary.dispatched == 1

        async def inspect() -> tuple[ResearchRun, YouTubeChannelSchedule]:
            async with get_session_factory()() as session:
                research = await session.scalar(
                    select(ResearchRun).where(
                        ResearchRun.organization_id == UUID(organization_id),
                        ResearchRun.youtube_channel_id == UUID(channel_id),
                    )
                )
                schedule = await session.scalar(
                    select(YouTubeChannelSchedule).where(
                        YouTubeChannelSchedule.organization_id == UUID(organization_id),
                        YouTubeChannelSchedule.youtube_channel_id == UUID(channel_id),
                    )
                )
                assert research is not None
                assert schedule is not None
                return research, schedule

        research, schedule = asyncio.run(inspect())
        assert research.scheduled_for == target_publish_at
        assert schedule.next_run_at > target_publish_at
        second = asyncio.run(
            dispatch_due_channel_schedules_once(session_factory=get_session_factory())
        )
        assert second.dispatched == 0
    finally:
        cleanup_user(user_id)
