from datetime import date, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.research_run import ResearchRun


async def create_research_run(
    session: AsyncSession,
    organization_id: UUID,
    requested_by_user_id: UUID,
    topic: str,
    *,
    youtube_channel_id: UUID | None = None,
    channel_profile_id: UUID | None = None,
    channel_profile_version: int | None = None,
    channel_profile_snapshot: dict | None = None,
    scheduled_local_date: date | None = None,
    scheduled_for: datetime | None = None,
) -> ResearchRun:
    """Create a queued research run for an authorized organization."""

    channel_binding = (
        youtube_channel_id,
        channel_profile_id,
        channel_profile_version,
        channel_profile_snapshot,
    )
    if any(value is not None for value in channel_binding) and not all(
        value is not None for value in channel_binding
    ):
        raise ValueError("Channel-scoped research requires a complete profile snapshot.")
    if channel_profile_snapshot is not None and not isinstance(
        channel_profile_snapshot, dict
    ):
        raise ValueError("Channel profile snapshot must be a JSON object.")
    if (scheduled_local_date is None) != (scheduled_for is None):
        raise ValueError("Scheduled local date and timestamp must be supplied together.")

    research_run = ResearchRun(
        organization_id=organization_id,
        requested_by_user_id=requested_by_user_id,
        topic=topic,
        youtube_channel_id=youtube_channel_id,
        channel_profile_id=channel_profile_id,
        channel_profile_version=channel_profile_version,
        channel_profile_snapshot=channel_profile_snapshot,
        scheduled_local_date=scheduled_local_date,
        scheduled_for=scheduled_for,
        status="queued",
    )

    session.add(research_run)
    await session.flush()

    return research_run
