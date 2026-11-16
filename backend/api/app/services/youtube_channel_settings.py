from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.services.youtube_oauth import channel_can_publish


class YouTubeChannelSettingsError(ValueError):
    """A channel profile or schedule cannot be changed in the current state."""


class YouTubeChannelNotFoundError(YouTubeChannelSettingsError):
    """The selected channel is not active in the requested workspace."""


def validate_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise YouTubeChannelSettingsError("A valid IANA timezone is required.") from exc


def next_schedule_occurrence(
    *,
    now: datetime,
    timezone_name: str,
    posting_time: time,
) -> datetime:
    zone = validate_timezone(timezone_name)
    local_now = now.astimezone(zone)
    local_date = local_now.date()
    candidate_naive = datetime.combine(local_date, posting_time)
    candidate = candidate_naive.replace(tzinfo=zone)
    if candidate.astimezone(UTC) <= now.astimezone(UTC):
        candidate_naive = datetime.combine(local_date + timedelta(days=1), posting_time)
        candidate = candidate_naive.replace(tzinfo=zone)
    # Normalize nonexistent local times across daylight-saving transitions.
    normalized = candidate.astimezone(UTC).astimezone(zone)
    if normalized.replace(tzinfo=None) != candidate_naive:
        candidate = normalized
    return candidate.astimezone(UTC)


async def get_latest_channel_profile(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
) -> YouTubeChannelProfile | None:
    return await session.scalar(
        select(YouTubeChannelProfile)
        .where(
            YouTubeChannelProfile.organization_id == organization_id,
            YouTubeChannelProfile.youtube_channel_id == youtube_channel_id,
        )
        .order_by(YouTubeChannelProfile.version.desc())
        .limit(1)
    )


async def create_channel_profile_version(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
    created_by_user_id: UUID,
    profile: dict,
) -> YouTubeChannelProfile:
    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == youtube_channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        raise YouTubeChannelNotFoundError(
            "The YouTube channel is not active in this workspace."
        )
    current_version = await session.scalar(
        select(func.coalesce(func.max(YouTubeChannelProfile.version), 0)).where(
            YouTubeChannelProfile.organization_id == organization_id,
            YouTubeChannelProfile.youtube_channel_id == youtube_channel_id,
        )
    )
    record = YouTubeChannelProfile(
        organization_id=organization_id,
        youtube_channel_id=youtube_channel_id,
        version=int(current_version or 0) + 1,
        profile=profile,
        created_by_user_id=created_by_user_id,
    )
    session.add(record)
    await session.flush()
    return record


async def get_channel_schedule(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
) -> YouTubeChannelSchedule | None:
    return await session.scalar(
        select(YouTubeChannelSchedule).where(
            YouTubeChannelSchedule.organization_id == organization_id,
            YouTubeChannelSchedule.youtube_channel_id == youtube_channel_id,
        )
    )


async def save_channel_schedule(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
    updated_by_user_id: UUID,
    enabled: bool,
    timezone_name: str,
    posting_time: time,
    catch_up_policy: str,
) -> YouTubeChannelSchedule:
    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == youtube_channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        raise YouTubeChannelNotFoundError(
            "The YouTube channel is not active in this workspace."
        )
    validate_timezone(timezone_name)
    if catch_up_policy not in {"skip", "run_once"}:
        raise YouTubeChannelSettingsError("Unsupported schedule catch-up policy.")
    if enabled and not channel_can_publish(channel.granted_scopes):
        raise YouTubeChannelSettingsError(
            "Enable publishing consent for this channel before activating its daily schedule."
        )
    if enabled:
        profile = await get_latest_channel_profile(
            session,
            organization_id=organization_id,
            youtube_channel_id=youtube_channel_id,
        )
        if profile is None:
            raise YouTubeChannelSettingsError(
                "Configure an editorial profile before activating this daily schedule."
            )
    now = await session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise RuntimeError("Could not obtain the database clock.")
    next_run_at = (
        next_schedule_occurrence(
            now=now,
            timezone_name=timezone_name,
            posting_time=posting_time,
        )
        if enabled
        else None
    )
    schedule = await session.scalar(
        select(YouTubeChannelSchedule)
        .where(
            YouTubeChannelSchedule.organization_id == organization_id,
            YouTubeChannelSchedule.youtube_channel_id == youtube_channel_id,
        )
        .with_for_update()
    )
    if schedule is None:
        schedule = YouTubeChannelSchedule(
            organization_id=organization_id,
            youtube_channel_id=youtube_channel_id,
            updated_by_user_id=updated_by_user_id,
            enabled=enabled,
            timezone=timezone_name,
            posting_time=posting_time,
            catch_up_policy=catch_up_policy,
            next_run_at=next_run_at,
        )
        session.add(schedule)
    else:
        schedule.enabled = enabled
        schedule.timezone = timezone_name
        schedule.posting_time = posting_time
        schedule.catch_up_policy = catch_up_policy
        schedule.next_run_at = next_run_at
        schedule.updated_by_user_id = updated_by_user_id
    await session.flush()
    return schedule
