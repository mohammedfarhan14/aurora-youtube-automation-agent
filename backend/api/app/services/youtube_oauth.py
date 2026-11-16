from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tokens import generate_secure_token, hash_token
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.models.youtube import YouTubeChannel, YouTubeOAuthAttempt
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.models.youtube_publishing import YouTubePublishingConsentEvent
from app.services.youtube_crypto import (
    decrypt_refresh_token,
    encrypt_refresh_token,
    get_active_encryption_key,
    get_encryption_keys,
)

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_CONNECT_SCOPES = (YOUTUBE_SCOPE, YOUTUBE_ANALYTICS_SCOPE)
YOUTUBE_PUBLISH_SCOPES = (YOUTUBE_SCOPE, YOUTUBE_ANALYTICS_SCOPE, YOUTUBE_UPLOAD_SCOPE)
YOUTUBE_PUBLISH_REQUIRED_SCOPES = frozenset((YOUTUBE_SCOPE, YOUTUBE_UPLOAD_SCOPE))
_LEGACY_CONNECT_SCOPES = frozenset((YOUTUBE_SCOPE,))
_LEGACY_PUBLISH_SCOPES = frozenset((YOUTUBE_SCOPE, YOUTUBE_UPLOAD_SCOPE))
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"


class YouTubeOAuthError(Exception):
    """Raised when a YouTube authorization operation cannot be completed."""


class YouTubeChannelNotFoundError(YouTubeOAuthError):
    """Raised when a requested channel is not active in the selected workspace."""


def validate_granted_scopes(
    scope_value: object, requested_scopes: tuple[str, ...]
) -> set[str]:
    """Validate Google's effective grant before persisting a channel connection."""
    if scope_value is None:
        granted_scopes = set(requested_scopes)
    elif isinstance(scope_value, str) and scope_value.strip():
        granted_scopes = set(scope_value.split())
    else:
        raise YouTubeOAuthError("Google returned an invalid YouTube scope grant.")

    if not set(requested_scopes).issubset(granted_scopes):
        raise YouTubeOAuthError(
            "Google did not grant all requested YouTube permissions. "
            "The existing channel connection was left unchanged."
        )
    return granted_scopes


def channel_can_publish(granted_scopes: str | set[str]) -> bool:
    scopes = (
        set(granted_scopes.split())
        if isinstance(granted_scopes, str)
        else granted_scopes
    )
    return YOUTUBE_PUBLISH_REQUIRED_SCOPES.issubset(scopes)


def validate_target_channel(
    target_channel_id: UUID | None,
    target_channel: YouTubeChannel | None,
    returned_channel_id: str,
) -> YouTubeChannel | None:
    """Ensure publishing re-consent returns the exact channel selected by the user."""
    if target_channel_id is None:
        return None
    if (
        target_channel is None
        or target_channel.id != target_channel_id
        or target_channel.channel_id != returned_channel_id
    ):
        raise YouTubeOAuthError(
            "The Google account does not match the channel selected for publishing consent. "
            "The existing channel connection was left unchanged."
        )
    return target_channel


def _credentials(settings: Settings) -> tuple[str, str, str]:
    client_id = settings.youtube_client_id
    client_secret = (
        settings.youtube_client_secret.get_secret_value()
        if settings.youtube_client_secret else None
    )
    if not client_id or not client_secret:
        raise YouTubeOAuthError("YouTube OAuth is not configured.")
    return client_id, client_secret, settings.youtube_redirect_uri


async def create_authorization_url(
    session: AsyncSession,
    settings: Settings,
    organization_id: UUID,
    user_id: UUID,
    purpose: Literal["connect", "publish"] = "connect",
    target_channel_id: UUID | None = None,
) -> str:
    client_id, _, redirect_uri = _credentials(settings)
    state = generate_secure_token()
    if purpose == "connect":
        if target_channel_id is not None:
            raise YouTubeOAuthError("A channel target is not valid for read-only connection.")
        requested_scopes = YOUTUBE_CONNECT_SCOPES
    elif purpose == "publish":
        if target_channel_id is None:
            raise YouTubeOAuthError("Select a connected channel before enabling publishing.")
        target_channel = await session.scalar(
            select(YouTubeChannel).where(
                YouTubeChannel.id == target_channel_id,
                YouTubeChannel.organization_id == organization_id,
                YouTubeChannel.status == "active",
            )
        )
        if target_channel is None:
            raise YouTubeChannelNotFoundError(
                "The selected YouTube channel is not active in this workspace."
            )
        requested_scopes = YOUTUBE_PUBLISH_SCOPES
    else:
        raise YouTubeOAuthError("Unsupported YouTube authorization purpose.")
    attempt = YouTubeOAuthAttempt(
        organization_id=organization_id,
        user_id=user_id,
        requested_scopes=" ".join(requested_scopes),
        target_channel_id=target_channel_id,
        state_hash=hash_token(state),
        expires_at=datetime.now(UTC) + timedelta(
            seconds=settings.youtube_oauth_state_ttl_seconds
        ),
    )
    session.add(attempt)
    await session.commit()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(requested_scopes),
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


async def _consume_state(
    session: AsyncSession, state: str
) -> tuple[UUID, UUID, tuple[str, ...], UUID | None]:
    result = await session.execute(
        select(YouTubeOAuthAttempt)
        .where(YouTubeOAuthAttempt.state_hash == hash_token(state))
        .with_for_update()
    )
    attempt = result.scalar_one_or_none()
    now = datetime.now(UTC)
    if (
        attempt is None
        or attempt.consumed_at is not None
        or attempt.expires_at <= now
    ):
        raise YouTubeOAuthError("OAuth state is invalid, expired, or already used.")
    attempt.consumed_at = now
    organization_id = attempt.organization_id
    user_id = attempt.user_id
    requested_scopes = tuple(attempt.requested_scopes.split())
    requested_scope_set = set(requested_scopes)
    target_channel_id = attempt.target_channel_id
    if requested_scope_set in (set(YOUTUBE_CONNECT_SCOPES), _LEGACY_CONNECT_SCOPES):
        if target_channel_id is not None:
            raise YouTubeOAuthError("Read-only OAuth attempt has an unexpected channel target.")
    elif requested_scope_set in (set(YOUTUBE_PUBLISH_SCOPES), _LEGACY_PUBLISH_SCOPES):
        if target_channel_id is None:
            raise YouTubeOAuthError("Publishing OAuth attempt is missing its channel target.")
    else:
        raise YouTubeOAuthError("OAuth attempt contains unsupported requested scopes.")
    await session.commit()
    return organization_id, user_id, requested_scopes, target_channel_id


async def _validate_authorization_actor(
    session: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
) -> None:
    """Re-check the initiating user's authorization when Google returns.

    The user's role, account status, email verification, or organization
    status may have changed while the browser was at Google's consent screen.
    """
    result = await session.execute(
        select(User.id)
        .join(OrganizationMember, OrganizationMember.user_id == User.id)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            User.id == user_id,
            User.is_active.is_(True),
            User.email_verified_at.is_not(None),
            Organization.id == organization_id,
            Organization.status == "active",
            OrganizationMember.status == "active",
            OrganizationMember.role.in_(("owner", "admin")),
        )
    )
    if result.scalar_one_or_none() is None:
        raise YouTubeOAuthError(
            "You are no longer authorized to connect a YouTube channel to this workspace."
        )


def _normalize_channel_items(items: object) -> list[dict[str, str | None]]:
    if not isinstance(items, list):
        raise YouTubeOAuthError("Google returned an invalid YouTube channel list.")
    channels: dict[str, dict[str, str | None]] = {}
    for item in items:
        if not isinstance(item, dict):
            raise YouTubeOAuthError("Google returned an invalid YouTube channel entry.")
        channel_id = item.get("id")
        snippet = item.get("snippet")
        if not isinstance(channel_id, str) or not channel_id.strip() or len(channel_id) > 128:
            raise YouTubeOAuthError("Google returned a channel without a valid channel ID.")
        if not isinstance(snippet, dict):
            raise YouTubeOAuthError("Google returned invalid channel metadata.")
        title = snippet.get("title")
        custom_url = snippet.get("customUrl")
        thumbnails = snippet.get("thumbnails")
        thumbnail_url = None
        if isinstance(thumbnails, dict):
            for size in ("default", "medium", "high"):
                image = thumbnails.get(size)
                candidate = image.get("url") if isinstance(image, dict) else None
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    thumbnail_url = candidate[:2048]
                    break
        channels.setdefault(
            channel_id,
            {
                "channel_id": channel_id,
                "title": (
                    title[:255]
                    if isinstance(title, str) and title.strip()
                    else "YouTube channel"
                ),
                "custom_url": custom_url[:255] if isinstance(custom_url, str) else None,
                "thumbnail_url": thumbnail_url,
            },
        )
    return list(channels.values())


async def _fetch_all_channels(
    client: httpx.AsyncClient, access_token: str
) -> list[dict[str, str | None]]:
    channels: list[dict[str, str | None]] = []
    seen: set[str] = set()
    page_token: str | None = None
    for _ in range(20):
        params: dict[str, str | int] = {"part": "snippet", "mine": "true", "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        response = await client.get(
            YOUTUBE_CHANNELS_URL,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise YouTubeOAuthError("Google returned an invalid YouTube channel response.")
        page_channels = _normalize_channel_items(payload.get("items", []))
        for channel in page_channels:
            channel_id = str(channel["channel_id"])
            if channel_id not in seen:
                seen.add(channel_id)
                channels.append(channel)
        page_token = payload.get("nextPageToken")
        if not page_token:
            return channels
        if not isinstance(page_token, str) or len(page_token) > 2048:
            raise YouTubeOAuthError("Google returned an invalid YouTube pagination token.")
    raise YouTubeOAuthError("Google returned too many YouTube channel pages to process safely.")


async def _upsert_authorized_channel(
    session: AsyncSession,
    *,
    organization_id: UUID,
    user_id: UUID,
    channel_data: dict[str, str | None],
    granted_scopes: set[str],
    encrypted_token: str,
    key_version: int,
    existing_channel: YouTubeChannel | None,
) -> YouTubeChannel:
    channel_id = str(channel_data["channel_id"])
    scopes = " ".join(sorted(granted_scopes))
    if existing_channel is None:
        channel = YouTubeChannel(
            organization_id=organization_id,
            channel_id=channel_id,
            title=str(channel_data["title"] or "YouTube channel"),
            custom_url=channel_data["custom_url"],
            thumbnail_url=channel_data["thumbnail_url"],
            encrypted_refresh_token=encrypted_token,
            key_version=key_version,
            granted_scopes=scopes,
            connected_by_user_id=user_id,
            status="active",
        )
        session.add(channel)
    else:
        channel = existing_channel
        if getattr(channel, "auto_publish_enabled", False) and (
            getattr(channel, "connected_by_user_id", None) != user_id
            or not channel_can_publish(granted_scopes)
        ):
            channel.auto_publish_enabled = False
            session.add(
                YouTubePublishingConsentEvent(
                    organization_id=organization_id,
                    youtube_channel_id=channel.id,
                    actor_user_id=user_id,
                    action="disabled",
                    reason=(
                        "oauth_connection_owner_changed"
                        if getattr(channel, "connected_by_user_id", None) != user_id
                        else "youtube_upload_scope_removed"
                    ),
                    granted_scopes_snapshot=scopes,
                )
            )
        channel.title = str(channel_data["title"] or channel.title)
        channel.custom_url = channel_data["custom_url"]
        channel.thumbnail_url = channel_data["thumbnail_url"]
        channel.encrypted_refresh_token = encrypted_token
        channel.key_version = key_version
        channel.granted_scopes = scopes
        channel.connected_by_user_id = user_id
        channel.status = "active"
    return channel


async def complete_authorization(
    session: AsyncSession, settings: Settings, state: str, code: str
) -> YouTubeChannel | YouTubeOAuthAttempt:
    client_id, client_secret, redirect_uri = _credentials(settings)
    organization_id, user_id, requested_scopes, target_channel_id = await _consume_state(
        session, state
    )
    await _validate_authorization_actor(session, organization_id, user_id)
    attempt = await session.scalar(
        select(YouTubeOAuthAttempt).where(YouTubeOAuthAttempt.state_hash == hash_token(state))
    )
    if attempt is None:
        raise YouTubeOAuthError("OAuth attempt could not be recovered.")
    async with httpx.AsyncClient(timeout=15.0) as client:
        token_response = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        token_response.raise_for_status()
        token_data = token_response.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        granted_scopes = validate_granted_scopes(token_data.get("scope"), requested_scopes)
        if not access_token:
            raise YouTubeOAuthError("Google did not return an access token.")
        channels = await _fetch_all_channels(client, access_token)

    if not channels:
        raise YouTubeOAuthError("No YouTube channel was found for this Google account.")

    if target_channel_id is None:
        # Persist only encrypted, short-lived pending consent data. No channel
        # connection is created until the initiating user explicitly selects one.
        if refresh_token:
            key_version, encryption_key = get_active_encryption_key(settings)
            pending_encrypted = encrypt_refresh_token(
                refresh_token,
                encryption_key,
                organization_id,
                f"oauth-pending:{attempt.id}",
                key_version,
            )
        else:
            key_version, pending_encrypted = None, None
        attempt.pending_channels = channels
        attempt.pending_encrypted_refresh_token = pending_encrypted
        attempt.pending_key_version = key_version
        attempt.pending_granted_scopes = " ".join(sorted(granted_scopes))
        attempt.pending_selection_expires_at = datetime.now(UTC) + timedelta(
            seconds=settings.youtube_oauth_state_ttl_seconds
        )
        await session.commit()
        await session.refresh(attempt)
        return attempt

    target_result = await session.execute(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == target_channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    target_channel = target_result.scalar_one_or_none()
    target_metadata = next(
        (channel for channel in channels if channel["channel_id"] == target_channel.channel_id),
        None,
    ) if target_channel is not None else None
    channel = validate_target_channel(
        target_channel_id,
        target_channel,
        str(target_metadata["channel_id"]) if target_metadata else "",
    )
    if channel is None or target_metadata is None:
        raise YouTubeOAuthError("The selected YouTube channel was not returned by Google.")
    if refresh_token:
        key_version, encryption_key = get_active_encryption_key(settings)
        encrypted_token = encrypt_refresh_token(
            refresh_token, encryption_key, organization_id, channel.channel_id, key_version
        )
    elif channel.encrypted_refresh_token:
        encrypted_token = channel.encrypted_refresh_token
        key_version = channel.key_version
    else:
        raise YouTubeOAuthError(
            "Google did not issue a refresh token. Re-authorize with consent "
            "to grant offline access."
        )
    await _upsert_authorized_channel(
        session,
        organization_id=organization_id,
        user_id=user_id,
        channel_data=target_metadata,
        granted_scopes=granted_scopes,
        encrypted_token=encrypted_token,
        key_version=key_version,
        existing_channel=channel,
    )
    await session.commit()
    await session.refresh(channel)
    return channel


async def get_pending_channel_selection(
    session: AsyncSession,
    organization_id: UUID,
    user_id: UUID,
    attempt_id: UUID,
) -> dict:
    attempt = await session.scalar(
        select(YouTubeOAuthAttempt).where(
            YouTubeOAuthAttempt.id == attempt_id,
            YouTubeOAuthAttempt.organization_id == organization_id,
            YouTubeOAuthAttempt.user_id == user_id,
        )
    )
    now = datetime.now(UTC)
    if (
        attempt is None
        or attempt.selection_completed_at is not None
        or not attempt.pending_channels
        or attempt.pending_selection_expires_at is None
        or attempt.pending_selection_expires_at <= now
    ):
        raise YouTubeOAuthError("The pending YouTube channel selection is unavailable or expired.")
    await _validate_authorization_actor(session, organization_id, user_id)
    return {
        "attempt_id": attempt.id,
        "organization_id": organization_id,
        "expires_at": attempt.pending_selection_expires_at,
        "channels": attempt.pending_channels,
    }


async def select_pending_channel(
    session: AsyncSession,
    settings: Settings,
    organization_id: UUID,
    user_id: UUID,
    attempt_id: UUID,
    channel_id: str,
) -> YouTubeChannel:
    attempt = await session.scalar(
        select(YouTubeOAuthAttempt)
        .where(
            YouTubeOAuthAttempt.id == attempt_id,
            YouTubeOAuthAttempt.organization_id == organization_id,
            YouTubeOAuthAttempt.user_id == user_id,
        )
        .with_for_update()
    )
    now = datetime.now(UTC)
    if (
        attempt is None
        or attempt.selection_completed_at is not None
        or not attempt.pending_channels
        or attempt.pending_selection_expires_at is None
        or attempt.pending_selection_expires_at <= now
    ):
        raise YouTubeOAuthError("The pending YouTube channel selection is unavailable or expired.")
    await _validate_authorization_actor(session, organization_id, user_id)
    selected = next(
        (item for item in attempt.pending_channels if item.get("channel_id") == channel_id),
        None,
    )
    if selected is None:
        raise YouTubeChannelNotFoundError(
            "The selected channel was not part of this authorization."
        )
    existing_result = await session.execute(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.channel_id == channel_id,
        )
        .with_for_update()
    )
    existing = existing_result.scalar_one_or_none()
    if attempt.pending_encrypted_refresh_token and attempt.pending_key_version:
        keys = get_encryption_keys(settings)
        refresh_token = decrypt_refresh_token(
            attempt.pending_encrypted_refresh_token,
            keys,
            organization_id,
            f"oauth-pending:{attempt.id}",
            attempt.pending_key_version,
        )
        key_version, encryption_key = get_active_encryption_key(settings)
        encrypted_token = encrypt_refresh_token(
            refresh_token, encryption_key, organization_id, channel_id, key_version
        )
    elif existing is not None and existing.encrypted_refresh_token:
        encrypted_token = existing.encrypted_refresh_token
        key_version = existing.key_version
    else:
        raise YouTubeOAuthError(
            "Google did not issue a refresh token for this new channel. Re-authorize with consent."
        )
    scopes = set((attempt.pending_granted_scopes or "").split())
    if not scopes:
        raise YouTubeOAuthError("The pending YouTube permission grant is invalid.")
    channel = await _upsert_authorized_channel(
        session,
        organization_id=organization_id,
        user_id=user_id,
        channel_data=selected,
        granted_scopes=scopes,
        encrypted_token=encrypted_token,
        key_version=key_version,
        existing_channel=existing,
    )
    attempt.pending_channels = None
    attempt.pending_encrypted_refresh_token = None
    attempt.pending_key_version = None
    attempt.pending_granted_scopes = None
    attempt.pending_selection_expires_at = None
    attempt.selection_completed_at = now
    await session.commit()
    await session.refresh(channel)
    return channel


async def list_channels(
    session: AsyncSession, organization_id: UUID
) -> list[YouTubeChannel]:
    result = await session.execute(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .order_by(YouTubeChannel.connected_at.desc())
    )
    return list(result.scalars().all())


async def disconnect_channel(
    session: AsyncSession,
    settings: Settings,
    organization_id: UUID,
    channel_id: UUID,
    actor_user_id: UUID | None = None,
) -> None:
    result = await session.execute(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Channel not found.",
        )

    schedule = await session.scalar(
        select(YouTubeChannelSchedule)
        .where(
            YouTubeChannelSchedule.organization_id == organization_id,
            YouTubeChannelSchedule.youtube_channel_id == channel_id,
        )
        .with_for_update()
    )
    if schedule is not None:
        schedule.enabled = False
        schedule.next_run_at = None

    refresh_token = decrypt_refresh_token(
        channel.encrypted_refresh_token,
        get_encryption_keys(settings),
        organization_id,
        channel.channel_id,
        channel.key_version,
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(GOOGLE_REVOKE_URL, data={"token": refresh_token})
        if response.status_code not in (200, 400):
            response.raise_for_status()

    if channel.auto_publish_enabled:
        session.add(
            YouTubePublishingConsentEvent(
                organization_id=organization_id,
                youtube_channel_id=channel.id,
                actor_user_id=actor_user_id,
                action="disabled",
                reason="channel_disconnected",
                granted_scopes_snapshot=channel.granted_scopes,
            )
        )
        channel.auto_publish_enabled = False
        channel.auto_publish_consent_event_id = None

    channel.encrypted_refresh_token = ""
    channel.status = "disconnected"
    await session.commit()
