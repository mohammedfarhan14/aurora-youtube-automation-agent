from dataclasses import asdict
from datetime import UTC, date, datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_verified_user, require_organization_roles
from app.core.config import get_settings
from app.database.session import get_db_session
from app.models.organization import Organization
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_analytics_review import YouTubeAnalyticsReview
from app.schemas.youtube import (
    YouTubeAnalyticsComparison,
    YouTubeAnalyticsDay,
    YouTubeAnalyticsResponse,
    YouTubeAnalyticsReviewRequest,
    YouTubeAnalyticsReviewSummary,
)
from app.services.youtube_analytics import (
    YouTubeAnalyticsError,
    compare_complete_weeks,
    fetch_channel_analytics,
    persist_analytics_review_cue,
    persist_channel_analytics,
)
from app.services.youtube_tokens import (
    YouTubeReauthorizationRequired,
    YouTubeTokenConfigurationError,
    YouTubeTokenError,
)

router = APIRouter()


def _analytics_response_rows(rows) -> list[YouTubeAnalyticsDay]:
    """Convert service dataclasses into the API's validated response models."""
    return [YouTubeAnalyticsDay.model_validate(asdict(row)) for row in rows]


@router.get(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/analytics",
    response_model=YouTubeAnalyticsResponse,
    summary="Read daily non-monetary YouTube Analytics",
)
async def get_youtube_channel_analytics(
    organization_id: UUID,
    channel_record_id: UUID,
    start_date: date = Query(alias="startDate"),
    end_date: date = Query(alias="endDate"),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeAnalyticsResponse:
    """Query analytics for the exact tenant-owned channel; never include revenue."""
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="YouTube channel not found.")
    span_days = (end_date - start_date).days
    if span_days < 0 or span_days > 90:
        raise HTTPException(
            status_code=422,
            detail="Choose a date range no longer than 90 days.",
        )
    channel = await session.scalar(
        select(YouTubeChannel).where(
            YouTubeChannel.id == channel_record_id,
            YouTubeChannel.organization_id == organization.id,
            YouTubeChannel.status == "active",
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="YouTube channel not found.")

    settings = get_settings()
    try:
        async with (
            httpx.AsyncClient(timeout=15.0) as token_client,
            httpx.AsyncClient(timeout=20.0) as analytics_client,
        ):
            rows = await fetch_channel_analytics(
                settings,
                channel,
                start_date=start_date,
                end_date=end_date,
                token_client=token_client,
                analytics_client=analytics_client,
            )
        snapshot_count = await persist_channel_analytics(
            session,
            organization_id=organization.id,
            youtube_channel_id=channel.id,
            rows=rows,
        )
        await session.commit()
    except YouTubeReauthorizationRequired as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YouTubeTokenConfigurationError as exc:
        await session.rollback()
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except YouTubeTokenError as exc:
        await session.rollback()
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except YouTubeAnalyticsError as exc:
        # A successful token exchange may rotate the encrypted refresh token
        # even when the subsequent report request is rejected. Keep rotation.
        await session.commit()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    comparison_data = compare_complete_weeks(rows, end_date=end_date)
    review = None
    if comparison_data is not None:
        review = await persist_analytics_review_cue(
            session,
            organization_id=organization.id,
            youtube_channel_id=channel.id,
            comparison=comparison_data,
        )
        await session.commit()
        comparison_data["review"] = YouTubeAnalyticsReviewSummary.model_validate(review)

    latest_report_day = max((row.day for row in rows), default=None)
    pending_days = (
        max(0, (end_date - latest_report_day).days)
        if latest_report_day is not None
        else (end_date - start_date).days + 1
    )

    return YouTubeAnalyticsResponse(
        youtube_channel_id=channel.id,
        channel_title=channel.title,
        start_date=start_date,
        end_date=end_date,
        latest_report_day=latest_report_day,
        pending_days=pending_days,
        snapshot_count=snapshot_count,
        snapshot_saved_at=datetime.now(UTC) if snapshot_count else None,
        comparison=(
            YouTubeAnalyticsComparison.model_validate(comparison_data)
            if comparison_data is not None
            else None
        ),
        # fetch_channel_analytics returns frozen dataclasses. Passing those
        # objects to model_validate() only works for Pydantic models and caused
        # the analytics endpoint to raise an uncaught 500 after a valid report.
        rows=_analytics_response_rows(rows),
    )


@router.post(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/analytics-reviews/{review_id}",
    response_model=YouTubeAnalyticsReviewSummary,
    summary="Approve or dismiss an analytics review cue",
)
async def decide_youtube_analytics_review(
    organization_id: UUID,
    channel_record_id: UUID,
    review_id: UUID,
    payload: YouTubeAnalyticsReviewRequest,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor")
    ),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeAnalyticsReviewSummary:
    """Persist an explicit operator decision for a channel-scoped cue."""
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Analytics review not found.")
    review = await session.scalar(
        select(YouTubeAnalyticsReview).where(
            YouTubeAnalyticsReview.id == review_id,
            YouTubeAnalyticsReview.organization_id == organization.id,
            YouTubeAnalyticsReview.youtube_channel_id == channel_record_id,
        )
    )
    if review is None:
        raise HTTPException(status_code=404, detail="Analytics review not found.")
    review.status = payload.decision
    review.reviewed_by_user_id = current_user.id
    review.reviewed_at = datetime.now(UTC)
    await session.commit()
    return YouTubeAnalyticsReviewSummary.model_validate(review)
