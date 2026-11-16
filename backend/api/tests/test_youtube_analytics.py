from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.dialects import postgresql

from app.api.v1.endpoints.youtube_analytics import _analytics_response_rows
from app.services import youtube_analytics
from app.services.youtube_analytics import (
    YouTubeAnalyticsError,
    fetch_channel_analytics,
)
from app.services.youtube_tokens import (
    YOUTUBE_ANALYTICS_READONLY_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    YouTubeAccessToken,
    YouTubeReauthorizationRequired,
)


def _report() -> dict:
    names = [
        "day",
        "views",
        "estimatedMinutesWatched",
        "averageViewDuration",
        "averageViewPercentage",
        "subscribersGained",
        "subscribersLost",
        "likes",
        "comments",
        "shares",
    ]
    return {
        "columnHeaders": [{"name": name} for name in names],
        "rows": [["2026-11-10", 120, 48.5, 24.25, 81.2, 5, 1, 9, 2, 3]],
    }


def test_analytics_service_rows_are_serialized_into_response_models():
    rows = youtube_analytics._parse_rows(_report())
    response_rows = _analytics_response_rows(rows)

    assert response_rows[0].day == date(2026, 11, 10)
    assert response_rows[0].views == 120
    assert response_rows[0].minutes_watched == 48.5


@pytest.mark.anyio
async def test_fetch_channel_analytics_uses_channel_scoped_readonly_query(monkeypatch):
    channel = SimpleNamespace(
        status="active",
        granted_scopes=f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_ANALYTICS_READONLY_SCOPE}",
        channel_id="UC_demo_channel",
    )
    captured: dict = {}

    async def refresh(settings, selected_channel, *, client, required_scope):
        assert selected_channel is channel
        assert required_scope == YOUTUBE_ANALYTICS_READONLY_SCOPE
        return YouTubeAccessToken("short-lived-token", datetime.now(UTC) + timedelta(minutes=50))

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_report())

    monkeypatch.setattr(youtube_analytics, "refresh_youtube_access_token", refresh)
    async with (
        httpx.AsyncClient() as token_client,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as analytics_client,
    ):
        rows = await fetch_channel_analytics(
            object(),
            channel,
            start_date=date(2026, 11, 1),
            end_date=date(2026, 11, 28),
            token_client=token_client,
            analytics_client=analytics_client,
        )

    assert captured["authorization"] == "Bearer short-lived-token"
    assert "ids=channel%3D%3DUC_demo_channel" in captured["url"]
    assert "estimatedMinutesWatched" in captured["url"]
    assert rows[0].day == date(2026, 11, 10)
    assert rows[0].views == 120
    assert rows[0].minutes_watched == 48.5
    assert rows[0].subscribers_gained - rows[0].subscribers_lost == 4


@pytest.mark.anyio
async def test_fetch_analytics_requires_analytics_readonly_scope():
    channel = SimpleNamespace(
        status="active",
        granted_scopes=f"{YOUTUBE_READONLY_SCOPE} https://www.googleapis.com/auth/youtube.upload",
        channel_id="UC_demo_channel",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(YouTubeReauthorizationRequired):
            await fetch_channel_analytics(
                object(),
                channel,
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 2),
                token_client=client,
                analytics_client=client,
            )


@pytest.mark.anyio
async def test_fetch_analytics_requires_youtube_readonly_scope_too():
    channel = SimpleNamespace(
        status="active",
        granted_scopes=YOUTUBE_ANALYTICS_READONLY_SCOPE,
        channel_id="UC_demo_channel",
    )
    async with httpx.AsyncClient() as client:
        with pytest.raises(YouTubeReauthorizationRequired, match="YouTube read-only"):
            await fetch_channel_analytics(
                object(),
                channel,
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 2),
                token_client=client,
                analytics_client=client,
            )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("google_error", "expected"),
    [
        (
            {"status": "PERMISSION_DENIED", "errors": [{"reason": "accessNotConfigured"}]},
            "API is disabled",
        ),
        (
            {"status": "PERMISSION_DENIED", "errors": [{"reason": "insufficientPermissions"}]},
            "grant both YouTube read-only",
        ),
    ],
)
async def test_analytics_access_denials_explain_the_specific_google_fix(
    monkeypatch, google_error, expected
):
    channel = SimpleNamespace(
        status="active",
        granted_scopes=f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_ANALYTICS_READONLY_SCOPE}",
        channel_id="UC_demo_channel",
    )

    async def refresh(settings, selected_channel, *, client, required_scope):
        return YouTubeAccessToken("short-lived-token", datetime.now(UTC) + timedelta(minutes=50))

    def respond(request):
        return httpx.Response(403, json={"error": google_error})

    monkeypatch.setattr(youtube_analytics, "refresh_youtube_access_token", refresh)
    async with (
        httpx.AsyncClient() as token_client,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)) as analytics_client,
    ):
        with pytest.raises(YouTubeAnalyticsError, match=expected):
            await fetch_channel_analytics(
                object(),
                channel,
                start_date=date(2026, 11, 1),
                end_date=date(2026, 11, 2),
                token_client=token_client,
                analytics_client=analytics_client,
            )


def test_analytics_rejects_missing_metrics_in_provider_report():
    report = _report()
    report["columnHeaders"] = report["columnHeaders"][:-1]
    report["rows"][0] = report["rows"][0][:-1]
    with pytest.raises(YouTubeAnalyticsError, match="incomplete analytics report"):
        youtube_analytics._parse_rows(report)


def test_empty_analytics_report_is_not_invented_as_zero():
    report = _report()
    report["rows"] = []
    assert youtube_analytics._parse_rows(report) == ()


def test_week_comparison_requires_every_report_day_and_builds_review_cue():
    end_date = date(2026, 11, 28)
    start_date = end_date - timedelta(days=13)
    rows = tuple(
        youtube_analytics.DailyChannelAnalytics(
            day=start_date + timedelta(days=offset),
            views=100 if offset < 7 else 120,
            minutes_watched=50 if offset < 7 else 60,
            average_view_duration_seconds=30,
            average_view_percentage=80 if offset < 7 else 70,
            subscribers_gained=2,
            subscribers_lost=0,
            likes=4,
            comments=1,
            shares=1,
        )
        for offset in range(14)
    )

    comparison = youtube_analytics.compare_complete_weeks(rows, end_date=end_date)

    assert comparison is not None
    assert comparison["views_change_percent"] == 20.0
    assert comparison["watch_minutes_change_percent"] == 20.0
    assert comparison["average_view_percentage_change_points"] == -10.0
    assert "opening and pacing" in comparison["suggestion"]
    assert youtube_analytics.compare_complete_weeks(rows[:-1], end_date=end_date) is None


@pytest.mark.anyio
async def test_persist_channel_analytics_upserts_verified_days():
    row = youtube_analytics.DailyChannelAnalytics(
        day=date(2026, 11, 10),
        views=120,
        minutes_watched=48.5,
        average_view_duration_seconds=24.25,
        average_view_percentage=81.2,
        subscribers_gained=5,
        subscribers_lost=1,
        likes=9,
        comments=2,
        shares=3,
    )

    class SessionStub:
        statement = None

        async def execute(self, statement):
            self.statement = statement

    session = SessionStub()
    count = await youtube_analytics.persist_channel_analytics(
        session,
        organization_id=uuid4(),
        youtube_channel_id=uuid4(),
        rows=(row,),
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert count == 1
    assert "ON CONFLICT ON CONSTRAINT uq_youtube_analytics_snapshots_channel_day" in str(compiled)
    assert "fetched_at = now()" in str(compiled)
    assert 120 in compiled.params.values()


@pytest.mark.anyio
async def test_review_cue_upsert_preserves_operator_decision():
    end_date = date(2026, 11, 28)
    rows = tuple(
        youtube_analytics.DailyChannelAnalytics(
            day=end_date - timedelta(days=offset),
            views=100 + offset,
            minutes_watched=40.0,
            average_view_duration_seconds=24.0,
            average_view_percentage=80.0,
            subscribers_gained=1,
            subscribers_lost=0,
            likes=2,
            comments=0,
            shares=0,
        )
        for offset in range(14)
    )
    comparison = youtube_analytics.compare_complete_weeks(rows, end_date=end_date)
    existing_review = SimpleNamespace(status="approved")

    class SessionStub:
        statement = None

        async def execute(self, statement):
            self.statement = statement

        async def scalar(self, statement):
            return existing_review

    session = SessionStub()
    result = await youtube_analytics.persist_analytics_review_cue(
        session,
        organization_id=uuid4(),
        youtube_channel_id=uuid4(),
        comparison=comparison,
    )
    compiled = session.statement.compile(dialect=postgresql.dialect())

    assert result.status == "approved"
    assert "ON CONFLICT ON CONSTRAINT uq_youtube_analytics_reviews_channel_window" in str(compiled)
    assert "status = excluded.status" not in str(compiled)
