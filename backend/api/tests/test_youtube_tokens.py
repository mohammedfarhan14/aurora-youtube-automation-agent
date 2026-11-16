from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

from app.services.youtube_crypto import decrypt_refresh_token, encrypt_refresh_token
from app.services.youtube_tokens import (
    YOUTUBE_ANALYTICS_READONLY_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    YouTubeReauthorizationRequired,
    YouTubeTokenError,
    YouTubeTokenTransientError,
    channel_can_read_analytics,
    refresh_youtube_access_token,
)

KEY = "ab" * 32


def test_analytics_access_requires_both_youtube_read_scopes():
    assert channel_can_read_analytics(
        f"{YOUTUBE_READONLY_SCOPE} {YOUTUBE_ANALYTICS_READONLY_SCOPE}"
    )
    assert not channel_can_read_analytics(YOUTUBE_ANALYTICS_READONLY_SCOPE)
    assert not channel_can_read_analytics(YOUTUBE_READONLY_SCOPE)


def _settings():
    return SimpleNamespace(
        youtube_client_id="client-id",
        youtube_client_secret=SecretStr("client-secret"),
        integration_token_encryption_keyring=None,
        integration_token_encryption_key=SecretStr(KEY),
        integration_token_active_key_version=1,
    )


def _channel():
    organization_id = uuid4()
    channel_id = "UC-token-refresh"
    return SimpleNamespace(
        organization_id=organization_id, channel_id=channel_id, status="active",
        granted_scopes=(
            "https://www.googleapis.com/auth/youtube.readonly "
            "https://www.googleapis.com/auth/youtube.upload "
            "https://www.googleapis.com/auth/youtube.force-ssl"
        ),
        encrypted_refresh_token=encrypt_refresh_token(
            "refresh-secret", KEY, organization_id, channel_id, 1
        ),
        key_version=1,
    )


@pytest.mark.anyio
async def test_refresh_returns_short_lived_access_token():
    channel = _channel()
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={
            "access_token": "access-secret", "token_type": "Bearer", "expires_in": 3600,
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        token = await refresh_youtube_access_token(_settings(), channel, client=client)

    assert token.value == "access-secret"
    assert token.expires_at > datetime.now(UTC)
    assert len(requests) == 1
    assert str(requests[0].url) == "https://oauth2.googleapis.com/token"
    assert b"grant_type=refresh_token" in requests[0].content


@pytest.mark.anyio
async def test_rotated_refresh_token_is_reencrypted_and_context_bound():
    channel = _channel()

    def handler(request):
        return httpx.Response(200, json={
            "access_token": "access-secret", "token_type": "Bearer", "expires_in": 1800,
            "refresh_token": "rotated-refresh-secret",
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await refresh_youtube_access_token(_settings(), channel, client=client)

    assert decrypt_refresh_token(
        channel.encrypted_refresh_token, {1: KEY}, channel.organization_id,
        channel.channel_id, channel.key_version,
    ) == "rotated-refresh-secret"


@pytest.mark.anyio
async def test_invalid_grant_requires_reauthorization():
    channel = _channel()

    def handler(request):
        return httpx.Response(400, json={"error": "invalid_grant"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(YouTubeReauthorizationRequired):
            await refresh_youtube_access_token(_settings(), channel, client=client)


@pytest.mark.anyio
async def test_rate_limit_is_retryable():
    channel = _channel()

    def handler(request):
        return httpx.Response(429, json={"error": "rate_limited"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(YouTubeTokenTransientError):
            await refresh_youtube_access_token(_settings(), channel, client=client)


@pytest.mark.anyio
async def test_missing_upload_scope_fails_before_network_call():
    channel = _channel()
    channel.granted_scopes = "https://www.googleapis.com/auth/youtube.readonly"

    def handler(request):
        raise AssertionError("No token request should be sent without upload scope.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(YouTubeReauthorizationRequired):
            await refresh_youtube_access_token(_settings(), channel, client=client)


@pytest.mark.parametrize(
    ("access_token", "token_type"),
    [
        ("access token with spaces", "Bearer"),
        ("access-secret", None),
        ("access-secret", "Basic"),
    ],
)
@pytest.mark.anyio
async def test_rejects_invalid_bearer_token_metadata(access_token, token_type):
    channel = _channel()

    def handler(request):
        payload = {"access_token": access_token, "expires_in": 3600}
        if token_type is not None:
            payload["token_type"] = token_type
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(YouTubeTokenError):
            await refresh_youtube_access_token(_settings(), channel, client=client)
