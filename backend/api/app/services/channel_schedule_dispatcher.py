import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.database.engine import get_session_factory
from app.models.organization import Organization
from app.models.research_run import ResearchRun
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.research.providers.curated_rss import CuratedFeed, CuratedRssTrendProvider
from app.research.providers.hacker_news import HackerNewsTrendProvider
from app.research.providers.searxng import SearXNGTrendProvider
from app.research.topic_discovery import (
    select_research_topic,
    select_trend_research_topic,
)
from app.schemas.studio_settings import OrganizationStudioSettings
from app.services.research import create_research_run
from app.services.youtube_channel_settings import (
    YouTubeChannelSettingsError,
    next_schedule_occurrence,
    validate_timezone,
)
from app.services.youtube_oauth import channel_can_publish

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScheduleDispatchSummary:
    candidates: int = 0
    dispatched: int = 0
    skipped: int = 0
    disabled: int = 0
    busy: int = 0


def _build_research_topic(
    profile: dict,
    recent_topics: tuple[str, ...] | list[str] = (),
    trend_observations=(),
    *,
    now: datetime | None = None,
) -> str:
    """Select a permitted live trend lead or deterministic profile-derived topic."""

    try:
        if trend_observations and now is not None:
            trend_topic = select_trend_research_topic(
                profile,
                trend_observations,
                recent_topics,
                now=now,
            )
            if trend_topic is not None:
                return trend_topic
        return select_research_topic(profile, recent_topics)
    except ValueError as exc:
        raise YouTubeChannelSettingsError(str(exc)) from exc


async def _discover_trend_observations(settings) -> tuple:
    """Fetch one bounded provider snapshot before any schedule row locks."""

    if settings.trend_discovery_provider == "none":
        return ()
    try:
        if settings.trend_discovery_provider == "hacker_news":
            return await HackerNewsTrendProvider().discover(limit=30)
        if settings.trend_discovery_provider == "searxng":
            return await SearXNGTrendProvider(
                base_url=settings.trend_discovery_searxng_base_url or "",
                query=settings.trend_discovery_searxng_query,
            ).discover(limit=30)
        if settings.trend_discovery_provider == "curated_rss":
            feeds = tuple(
                CuratedFeed(url=item["url"], publisher=item["publisher"])
                for item in settings.trend_discovery_feeds
            )
            return await CuratedRssTrendProvider(feeds).discover()
        raise ValueError("Unsupported trend discovery provider.")
    except Exception as exc:
        logger.warning(
            "Trend discovery unavailable; using deterministic channel topics: "
            "provider=%s error_type=%s",
            settings.trend_discovery_provider,
            type(exc).__name__,
        )
        return ()


async def dispatch_due_channel_schedules_once(
    *,
    batch_size: int = 20,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> ScheduleDispatchSummary:
    """Create one channel-scoped research run for each due daily schedule."""

    if batch_size < 1 or batch_size > 500:
        raise ValueError("Schedule dispatch batch size must be between 1 and 500.")
    session_factory = session_factory or get_session_factory()
    settings = get_settings()

    async with session_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("Could not obtain the database clock.")
        result = await session.execute(
            select(
                YouTubeChannelSchedule.organization_id,
                YouTubeChannelSchedule.youtube_channel_id,
            )
            .where(
                YouTubeChannelSchedule.enabled.is_(True),
                YouTubeChannelSchedule.next_run_at
                <= now + timedelta(minutes=settings.channel_schedule_lead_minutes),
            )
            .order_by(
                YouTubeChannelSchedule.next_run_at,
                YouTubeChannelSchedule.id,
            )
            .limit(batch_size)
        )
        candidates = list(result.all())

    trend_observations = ()
    trend_now = datetime.now(UTC)
    if candidates and settings.trend_discovery_provider != "none":
        # Fetch once before channel/schedule row locks; rank per channel below.
        trend_observations = await _discover_trend_observations(settings)

    dispatched = 0
    skipped = 0
    disabled = 0
    busy = 0

    for organization_id, channel_id in candidates:
        try:
            async with session_factory() as session:
                async with session.begin():
                    now = await session.scalar(select(func.clock_timestamp()))
                    if now is None:
                        raise RuntimeError("Could not obtain the database clock.")

                    # Lock order is channel then schedule, matching settings and
                    # disconnect operations. Competing dispatchers skip busy channels.
                    channel = await session.scalar(
                        select(YouTubeChannel)
                        .where(
                            YouTubeChannel.id == channel_id,
                            YouTubeChannel.organization_id == organization_id,
                        )
                        .with_for_update(skip_locked=True)
                    )
                    if channel is None:
                        busy += 1
                        continue

                    schedule = await session.scalar(
                        select(YouTubeChannelSchedule)
                        .where(
                            YouTubeChannelSchedule.organization_id == organization_id,
                            YouTubeChannelSchedule.youtube_channel_id == channel_id,
                        )
                        .with_for_update()
                    )
                    if (
                        schedule is None
                        or not schedule.enabled
                        or schedule.next_run_at is None
                        or schedule.next_run_at
                        > now + timedelta(minutes=settings.channel_schedule_lead_minutes)
                    ):
                        continue

                    organization = await session.get(Organization, organization_id)
                    studio_settings = OrganizationStudioSettings.model_validate(
                        organization.studio_settings or {}
                        if organization is not None
                        else {}
                    )
                    if not studio_settings.research_enabled:
                        schedule.next_run_at = next_schedule_occurrence(
                            now=max(now, schedule.next_run_at),
                            timezone_name=schedule.timezone,
                            posting_time=schedule.posting_time,
                        )
                        skipped += 1
                        continue

                    if channel.status != "active" or not channel_can_publish(
                        channel.granted_scopes
                    ):
                        schedule.enabled = False
                        schedule.next_run_at = None
                        disabled += 1
                        continue

                    profile = await session.scalar(
                        select(YouTubeChannelProfile)
                        .where(
                            YouTubeChannelProfile.organization_id == organization_id,
                            YouTubeChannelProfile.youtube_channel_id == channel_id,
                        )
                        .order_by(YouTubeChannelProfile.version.desc())
                        .limit(1)
                    )
                    if profile is None:
                        schedule.enabled = False
                        schedule.next_run_at = None
                        disabled += 1
                        continue

                    try:
                        zone = validate_timezone(schedule.timezone)
                        if not isinstance(profile.profile, dict):
                            raise YouTubeChannelSettingsError(
                                "The channel editorial profile is invalid."
                            )
                        recent_topics = list(
                            await session.scalars(
                                select(ResearchRun.topic)
                                .where(
                                    ResearchRun.organization_id == organization_id,
                                    ResearchRun.youtube_channel_id == channel_id,
                                )
                                .order_by(
                                    ResearchRun.created_at.desc(),
                                    ResearchRun.id.desc(),
                                )
                                .limit(30)
                            )
                        )
                        topic = _build_research_topic(
                            profile.profile,
                            recent_topics,
                            trend_observations,
                            now=trend_now,
                        )
                    except YouTubeChannelSettingsError:
                        schedule.enabled = False
                        schedule.next_run_at = None
                        disabled += 1
                        continue

                    local_today = now.astimezone(zone).date()
                    due_local_date = schedule.next_run_at.astimezone(zone).date()

                    if (
                        schedule.catch_up_policy == "skip"
                        and due_local_date < local_today
                    ):
                        schedule.last_scheduled_local_date = local_today
                        schedule.next_run_at = next_schedule_occurrence(
                            now=max(now, schedule.next_run_at),
                            timezone_name=schedule.timezone,
                            posting_time=schedule.posting_time,
                        )
                        skipped += 1
                        continue

                    scheduled_local_date = (
                        local_today if due_local_date < local_today else due_local_date
                    )
                    if (
                        schedule.last_scheduled_local_date is not None
                        and schedule.last_scheduled_local_date >= scheduled_local_date
                    ):
                        schedule.next_run_at = next_schedule_occurrence(
                            now=max(now, schedule.next_run_at),
                            timezone_name=schedule.timezone,
                            posting_time=schedule.posting_time,
                        )
                        skipped += 1
                        continue

                    existing_research = await session.scalar(
                        select(ResearchRun).where(
                            ResearchRun.organization_id == organization_id,
                            ResearchRun.youtube_channel_id == channel_id,
                            ResearchRun.scheduled_local_date == scheduled_local_date,
                        )
                    )
                    if existing_research is not None:
                        schedule.last_scheduled_local_date = scheduled_local_date
                        schedule.next_run_at = next_schedule_occurrence(
                            now=max(now, schedule.next_run_at),
                            timezone_name=schedule.timezone,
                            posting_time=schedule.posting_time,
                        )
                        skipped += 1
                        continue

                    scheduled_for = (
                        now if due_local_date < local_today else schedule.next_run_at
                    )
                    await create_research_run(
                        session,
                        organization_id=organization_id,
                        requested_by_user_id=schedule.updated_by_user_id,
                        topic=topic,
                        youtube_channel_id=channel_id,
                        channel_profile_id=profile.id,
                        channel_profile_version=profile.version,
                        channel_profile_snapshot=deepcopy(profile.profile),
                        scheduled_local_date=scheduled_local_date,
                        scheduled_for=scheduled_for,
                    )
                    schedule.last_scheduled_local_date = scheduled_local_date
                    schedule.next_run_at = next_schedule_occurrence(
                        now=max(now, schedule.next_run_at),
                        timezone_name=schedule.timezone,
                        posting_time=schedule.posting_time,
                    )
                    dispatched += 1
        except Exception as exc:
            logger.error(
                "Daily channel schedule dispatch failed: channel_id=%s error_type=%s",
                channel_id,
                type(exc).__name__,
            )
            # A failed transaction leaves the due schedule unchanged for retry.

    return ScheduleDispatchSummary(
        candidates=len(candidates),
        dispatched=dispatched,
        skipped=skipped,
        disabled=disabled,
        busy=busy,
    )
