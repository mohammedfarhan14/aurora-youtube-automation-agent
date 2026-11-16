from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

import app.api.v1.endpoints.youtube as youtube_endpoint
from app.core.config import Settings
from app.models.youtube import YouTubeOAuthAttempt


@pytest.mark.anyio
async def test_youtube_callback_redirect_is_not_cached_or_used_as_referrer(
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        youtube_frontend_result_url="https://app.example.com/settings/integrations",
    )
    monkeypatch.setattr(youtube_endpoint, "get_settings", lambda: settings)
    complete = AsyncMock()
    monkeypatch.setattr(youtube_endpoint, "complete_authorization", complete)
    session = AsyncMock()

    response = await youtube_endpoint.youtube_callback(
        state="s" * 32,
        code="authorization-code",
        error=None,
        session=session,
    )

    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert parse_qs(urlparse(response.headers["location"]).query) == {
        "youtube": ["connected"]
    }
    complete.assert_awaited_once()


@pytest.mark.anyio
async def test_youtube_callback_error_redirect_has_same_security_headers(
    monkeypatch,
):
    settings = Settings(
        _env_file=None,
        youtube_frontend_result_url="https://app.example.com/settings/integrations",
    )
    monkeypatch.setattr(youtube_endpoint, "get_settings", lambda: settings)
    consume = AsyncMock()
    monkeypatch.setattr(youtube_endpoint, "_consume_state", consume)
    session = AsyncMock()

    response = await youtube_endpoint.youtube_callback(
        state="s" * 32,
        code=None,
        error="access_denied",
        session=session,
    )

    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert parse_qs(urlparse(response.headers["location"]).query) == {
        "youtube": ["error"]
    }
    consume.assert_awaited_once_with(session, "s" * 32)
    session.rollback.assert_awaited_once()


@pytest.mark.anyio
async def test_youtube_callback_redirects_to_explicit_channel_selection(monkeypatch):
    from uuid import uuid4

    settings = Settings(
        _env_file=None,
        youtube_frontend_result_url="https://app.example.com/settings/integrations",
    )
    monkeypatch.setattr(youtube_endpoint, "get_settings", lambda: settings)
    organization_id = uuid4()
    attempt = YouTubeOAuthAttempt(
        id=uuid4(),
        organization_id=organization_id,
    )
    complete = AsyncMock(return_value=attempt)
    monkeypatch.setattr(youtube_endpoint, "complete_authorization", complete)

    response = await youtube_endpoint.youtube_callback(
        state="s" * 32,
        code="authorization-code",
        error=None,
        session=AsyncMock(),
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query == {
        "youtube": ["select"],
        "attempt_id": [str(attempt.id)],
        "organization_id": [str(organization_id)],
    }
    assert response.headers["cache-control"] == "no-store"
