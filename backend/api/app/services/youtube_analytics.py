"""Read-only daily channel analytics from the YouTube Analytics API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.core.config import Settings
from app.models.youtube import YouTubeChannel
from app.models.youtube_analytics_review import YouTubeAnalyticsReview
from app.models.youtube_analytics_snapshot import YouTubeAnalyticsSnapshot
from app.services.youtube_tokens import (
    YOUTUBE_ANALYTICS_READONLY_SCOPE,
    YouTubeReauthorizationRequired,
    channel_can_read_analytics,
    refresh_youtube_access_token,
)

YOUTUBE_ANALYTICS_REPORTS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
_METRICS = (
    "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
    "subscribersGained,subscribersLost,likes,comments,shares"
)


class YouTubeAnalyticsError(RuntimeError):
    """Safe analytics error suitable for a user-facing API response."""


async def persist_channel_analytics(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
    rows: tuple[DailyChannelAnalytics, ...],
) -> int:
    """Upsert verified report days without duplicating channel history."""
    if not rows:
        return 0
    values = [
        {
            "organization_id": organization_id,
            "youtube_channel_id": youtube_channel_id,
            "report_day": row.day,
            "views": row.views,
            "minutes_watched": row.minutes_watched,
            "average_view_duration_seconds": row.average_view_duration_seconds,
            "average_view_percentage": row.average_view_percentage,
            "subscribers_gained": row.subscribers_gained,
            "subscribers_lost": row.subscribers_lost,
            "likes": row.likes,
            "comments": row.comments,
            "shares": row.shares,
        }
        for row in rows
    ]
    statement = pg_insert(YouTubeAnalyticsSnapshot).values(values)
    statement = statement.on_conflict_do_update(
        constraint="uq_youtube_analytics_snapshots_channel_day",
        set_={
            **{
                column: getattr(statement.excluded, column)
                for column in values[0]
                if column not in {"organization_id", "youtube_channel_id", "report_day"}
            },
            "fetched_at": func.now(),
        },
    )
    await session.execute(statement)
    return len(values)


@dataclass(frozen=True, slots=True)
class DailyChannelAnalytics:
    day: date
    views: int
    minutes_watched: float
    average_view_duration_seconds: float
    average_view_percentage: float
    subscribers_gained: int
    subscribers_lost: int
    likes: int
    comments: int
    shares: int


def _numeric(value: Any, *, integer: bool = False) -> int | float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise YouTubeAnalyticsError("YouTube returned invalid analytics data.") from exc
    if not number >= 0:
        raise YouTubeAnalyticsError("YouTube returned invalid analytics data.")
    return int(number) if integer else number


def _parse_rows(payload: Any) -> tuple[DailyChannelAnalytics, ...]:
    if not isinstance(payload, dict):
        raise YouTubeAnalyticsError("YouTube returned an invalid analytics response.")
    headers = payload.get("columnHeaders")
    rows = payload.get("rows", [])
    if not isinstance(headers, list) or not isinstance(rows, list):
        raise YouTubeAnalyticsError("YouTube returned an invalid analytics response.")
    names = [item.get("name") for item in headers if isinstance(item, dict)]
    required = {"day", *_METRICS.split(",")}
    if not required.issubset(names):
        raise YouTubeAnalyticsError("YouTube returned an incomplete analytics report.")
    indexes = {name: names.index(name) for name in required}
    parsed: list[DailyChannelAnalytics] = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(names):
            raise YouTubeAnalyticsError("YouTube returned an invalid analytics row.")
        try:
            row_date = date.fromisoformat(str(row[indexes["day"]]))
            parsed.append(
                DailyChannelAnalytics(
                    day=row_date,
                    views=_numeric(row[indexes["views"]], integer=True),
                    minutes_watched=_numeric(row[indexes["estimatedMinutesWatched"]]),
                    average_view_duration_seconds=_numeric(row[indexes["averageViewDuration"]]),
                    average_view_percentage=_numeric(row[indexes["averageViewPercentage"]]),
                    subscribers_gained=_numeric(row[indexes["subscribersGained"]], integer=True),
                    subscribers_lost=_numeric(row[indexes["subscribersLost"]], integer=True),
                    likes=_numeric(row[indexes["likes"]], integer=True),
                    comments=_numeric(row[indexes["comments"]], integer=True),
                    shares=_numeric(row[indexes["shares"]], integer=True),
                )
            )
        except (IndexError, ValueError) as exc:
            raise YouTubeAnalyticsError("YouTube returned an invalid analytics row.") from exc
    return tuple(parsed)


def compare_complete_weeks(
    rows: tuple[DailyChannelAnalytics, ...], *, end_date: date
) -> dict[str, Any] | None:
    """Compare two fully reported seven-day windows; never fill missing days."""
    current_start = end_date - timedelta(days=6)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=6)
    current_by_day = {row.day: row for row in rows}
    current_days = {current_start + timedelta(days=offset) for offset in range(7)}
    previous_days = {previous_start + timedelta(days=offset) for offset in range(7)}
    if not current_days.issubset(current_by_day) or not previous_days.issubset(current_by_day):
        return None

    current = [current_by_day[day] for day in sorted(current_days)]
    previous = [current_by_day[day] for day in sorted(previous_days)]
    current_views = sum(row.views for row in current)
    previous_views = sum(row.views for row in previous)
    current_watch = sum(row.minutes_watched for row in current)
    previous_watch = sum(row.minutes_watched for row in previous)
    current_retention = sum(row.average_view_percentage for row in current) / 7
    previous_retention = sum(row.average_view_percentage for row in previous) / 7

    def percent_change(now: float, before: float) -> float | None:
        return round((now - before) / before * 100, 1) if before else None

    views_change = percent_change(current_views, previous_views)
    watch_change = percent_change(current_watch, previous_watch)
    retention_points = round(current_retention - previous_retention, 1)
    if (
        views_change is not None
        and views_change >= 10
        and retention_points <= -5
    ):
        suggestion = (
            "Views rose while average viewed percentage fell. Review the opening and "
            "pacing of recent videos before carrying this topic pattern forward."
        )
    elif views_change is not None and views_change <= -10:
        suggestion = (
            "Views declined across the complete comparison windows. Review topic fit "
            "and packaging against the evidence before changing the next plan."
        )
    elif views_change is not None and views_change >= 10:
        suggestion = (
            "Views increased across the complete comparison windows. Review which "
            "topics and openings were used; this is a candidate pattern, not proof of cause."
        )
    elif retention_points >= 5:
        suggestion = (
            "Average viewed percentage improved. Review the story structure and pacing "
            "from these days before deciding whether to reuse them."
        )
    else:
        suggestion = (
            "No large directional change stands out. Keep collecting complete reports "
            "before drawing a content conclusion."
        )

    return {
        "current_start_date": current_start,
        "current_end_date": end_date,
        "previous_start_date": previous_start,
        "previous_end_date": previous_end,
        "current_views": current_views,
        "previous_views": previous_views,
        "current_watch_minutes": round(current_watch, 1),
        "previous_watch_minutes": round(previous_watch, 1),
        "current_average_view_percentage": round(current_retention, 1),
        "previous_average_view_percentage": round(previous_retention, 1),
        "views_change_percent": views_change,
        "watch_minutes_change_percent": watch_change,
        "average_view_percentage_change_points": retention_points,
        "suggestion": suggestion,
    }


async def persist_analytics_review_cue(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
    comparison: dict[str, Any],
) -> YouTubeAnalyticsReview:
    """Upsert a comparison cue while preserving any operator decision."""
    evidence_metrics = {
        key: value
        for key, value in comparison.items()
        if key not in {"suggestion", "current_start_date", "current_end_date",
                       "previous_start_date", "previous_end_date"}
    }
    statement = pg_insert(YouTubeAnalyticsReview).values(
        organization_id=organization_id,
        youtube_channel_id=youtube_channel_id,
        current_start_date=comparison["current_start_date"],
        current_end_date=comparison["current_end_date"],
        previous_start_date=comparison["previous_start_date"],
        previous_end_date=comparison["previous_end_date"],
        status="pending",
        suggestion=comparison["suggestion"],
        evidence_metrics=evidence_metrics,
    )
    statement = statement.on_conflict_do_update(
        constraint="uq_youtube_analytics_reviews_channel_window",
        set_={
            "suggestion": statement.excluded.suggestion,
            "evidence_metrics": statement.excluded.evidence_metrics,
            "updated_at": func.now(),
        },
    )
    await session.execute(statement)
    review = await session.scalar(
        select(YouTubeAnalyticsReview).where(
            YouTubeAnalyticsReview.organization_id == organization_id,
            YouTubeAnalyticsReview.youtube_channel_id == youtube_channel_id,
            YouTubeAnalyticsReview.current_start_date == comparison["current_start_date"],
            YouTubeAnalyticsReview.current_end_date == comparison["current_end_date"],
        )
    )
    if review is None:
        raise RuntimeError("Analytics review cue was not persisted.")
    return review


async def fetch_channel_analytics(
    settings: Settings,
    channel: YouTubeChannel,
    *,
    start_date: date,
    end_date: date,
    token_client: httpx.AsyncClient,
    analytics_client: httpx.AsyncClient,
) -> tuple[DailyChannelAnalytics, ...]:
    """Fetch only the selected channel's non-monetary daily metrics."""
    if channel.status != "active":
        raise YouTubeReauthorizationRequired("The YouTube channel is disconnected.")
    if not channel_can_read_analytics(channel.granted_scopes or ""):
        raise YouTubeReauthorizationRequired(
            "Reconnect the channel and grant YouTube read-only and YouTube Analytics "
            "read-only access before loading analytics."
        )

    access = await refresh_youtube_access_token(
        settings,
        channel,
        client=token_client,
        required_scope=YOUTUBE_ANALYTICS_READONLY_SCOPE,
    )
    try:
        response = await analytics_client.get(
            YOUTUBE_ANALYTICS_REPORTS_URL,
            params={
                "ids": f"channel=={channel.channel_id}",
                "startDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "metrics": _METRICS,
                "dimensions": "day",
                "sort": "day",
            },
            headers={"Authorization": f"Bearer {access.value}"},
        )
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in {401, 403}:
            try:
                error = exc.response.json().get("error", {})
                reasons = {
                    item.get("reason")
                    for item in error.get("errors", [])
                    if isinstance(item, dict)
                }
                google_status = error.get("status")
            except (ValueError, AttributeError):
                reasons = set()
                google_status = None
            if "accessNotConfigured" in reasons:
                raise YouTubeAnalyticsError(
                    "The YouTube Analytics API is disabled for this Google Cloud project. "
                    "Enable youtubeanalytics.googleapis.com, wait a few minutes, and retry."
                ) from exc
            if google_status == "PERMISSION_DENIED":
                raise YouTubeAnalyticsError(
                    "Google denied Analytics access. Reconnect the channel and grant both "
                    "YouTube read-only and YouTube Analytics read-only permissions."
                ) from exc
            raise YouTubeAnalyticsError(
                "Google denied Analytics access. Confirm the YouTube Analytics API is "
                "enabled and the channel has both required read-only permissions."
            ) from exc
        raise YouTubeAnalyticsError(
            f"YouTube Analytics returned HTTP {exc.response.status_code}."
        ) from exc
    except (httpx.RequestError, ValueError) as exc:
        raise YouTubeAnalyticsError(
            "YouTube Analytics could not be reached or returned invalid JSON."
        ) from exc
    return _parse_rows(payload)
