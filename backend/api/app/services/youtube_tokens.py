"""Short-lived Google access-token acquisition for YouTube publishing workers.

Refresh tokens are decrypted only for the outbound token exchange. Rotated refresh
tokens are re-encrypted with the active integration key and the existing
organization/channel AAD before the caller commits its database transaction.
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core.config import Settings
from app.models.youtube import YouTubeChannel
from app.services.youtube_crypto import (
    decrypt_refresh_token,
    encrypt_refresh_token,
    get_active_encryption_key,
    get_encryption_keys,
)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_READONLY_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_ANALYTICS_REQUIRED_SCOPES = frozenset(
    {YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_READONLY_SCOPE}
)


def channel_can_read_analytics(granted_scopes: str | set[str]) -> bool:
    scopes = (
        set(granted_scopes.split())
        if isinstance(granted_scopes, str)
        else granted_scopes
    )
    return YOUTUBE_ANALYTICS_REQUIRED_SCOPES.issubset(scopes)


class YouTubeTokenError(RuntimeError):
    """Base class for safe, non-secret YouTube token acquisition failures."""


class YouTubeTokenTransientError(YouTubeTokenError):
    """The token endpoint may succeed on a later attempt."""


class YouTubeReauthorizationRequired(YouTubeTokenError):
    """The stored grant is no longer usable and the channel must reconnect."""


class YouTubeTokenConfigurationError(YouTubeTokenError):
    """OAuth or encryption configuration is missing or invalid."""


@dataclass(frozen=True, slots=True)
class YouTubeAccessToken:
    value: str
    expires_at: datetime


def _error_code(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    return error if isinstance(error, str) else None


async def refresh_youtube_access_token(
    settings: Settings,
    channel: YouTubeChannel,
    *,
    client: httpx.AsyncClient,
    required_scope: str = YOUTUBE_UPLOAD_SCOPE,
) -> YouTubeAccessToken:
    """Exchange the channel's encrypted refresh token for a short-lived token.

    The function never commits the ORM object. If Google rotates the refresh
    token, the encrypted replacement and key version are assigned to channel
    and must be committed by the caller in its own transaction.
    """
    if channel.status != "active":
        raise YouTubeReauthorizationRequired("The YouTube channel is disconnected.")
    if required_scope not in set((channel.granted_scopes or "").split()):
        raise YouTubeReauthorizationRequired(
            "The YouTube channel does not have the permission required for this action."
        )
    if not settings.youtube_client_id or settings.youtube_client_secret is None:
        raise YouTubeTokenConfigurationError("YouTube OAuth client credentials are missing.")

    try:
        keys = get_encryption_keys(settings)
        refresh_token = decrypt_refresh_token(
            channel.encrypted_refresh_token,
            keys,
            channel.organization_id,
            channel.channel_id,
            channel.key_version,
        )
    except (ValueError, TypeError) as exc:
        raise YouTubeTokenConfigurationError(
            "The stored YouTube refresh token cannot be decrypted."
        ) from exc

    client_secret = settings.youtube_client_secret.get_secret_value()
    try:
        response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": settings.youtube_client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except httpx.TimeoutException as exc:
        raise YouTubeTokenTransientError("Google's token endpoint timed out.") from exc
    except httpx.RequestError as exc:
        raise YouTubeTokenTransientError("Google's token endpoint could not be reached.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        if response.status_code >= 500 or response.status_code == 429:
            raise YouTubeTokenTransientError(
                "Google's token endpoint returned a temporary error."
            ) from exc
        raise YouTubeTokenError("Google returned an invalid token response.") from exc

    if response.status_code >= 500 or response.status_code == 429:
        raise YouTubeTokenTransientError("Google's token endpoint is temporarily unavailable.")
    if response.status_code >= 400 or not response.is_success:
        if _error_code(payload) == "invalid_grant":
            raise YouTubeReauthorizationRequired(
                "Google rejected the stored refresh grant; reconnect this channel."
            )
        raise YouTubeTokenError("Google rejected the YouTube token refresh request.")

    if not isinstance(payload, dict):
        raise YouTubeTokenError("Google returned an invalid token response.")
    access_token = payload.get("access_token")
    token_type = payload.get("token_type")
    expires_in = payload.get("expires_in")
    if (
        not isinstance(access_token, str)
        or len(access_token) > 8192
        or re.fullmatch(r"[-A-Za-z0-9._~+/]+=*", access_token) is None
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or expires_in < 1
        or expires_in > 86400
    ):
        raise YouTubeTokenError("Google returned invalid access-token metadata.")

    rotated_refresh_token = payload.get("refresh_token")
    if rotated_refresh_token is not None:
        if (
            not isinstance(rotated_refresh_token, str)
            or not rotated_refresh_token
            or len(rotated_refresh_token) > 8192
        ):
            raise YouTubeTokenError("Google returned invalid refresh-token metadata.")
        try:
            key_version, encryption_key = get_active_encryption_key(settings)
            channel.encrypted_refresh_token = encrypt_refresh_token(
                rotated_refresh_token,
                encryption_key,
                channel.organization_id,
                channel.channel_id,
                key_version,
            )
            channel.key_version = key_version
        except (ValueError, TypeError) as exc:
            raise YouTubeTokenConfigurationError(
                "The rotated YouTube refresh token could not be encrypted."
            ) from exc

    return YouTubeAccessToken(
        value=access_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
    )
