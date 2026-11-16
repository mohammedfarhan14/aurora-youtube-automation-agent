from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest

from app.core.config import Settings
from app.core.tokens import hash_token
from app.services.youtube_oauth import (
    GOOGLE_AUTH_URL,
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_PUBLISH_SCOPES,
    YOUTUBE_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    YOUTUBE_VIDEO_WRITE_SCOPE,
    YouTubeChannelNotFoundError,
    YouTubeOAuthError,
    _consume_state,
    _fetch_all_channels,
    _normalize_channel_items,
    _validate_authorization_actor,
    create_authorization_url,
    select_pending_channel,
)


@pytest.mark.anyio
async def test_consume_state_marks_attempt_used_and_returns_bound_identity():
    organization_id = uuid4()
    user_id = uuid4()
    attempt = SimpleNamespace(
        organization_id=organization_id,
        user_id=user_id,
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        requested_scopes="https://www.googleapis.com/auth/youtube.readonly",
        target_channel_id=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = attempt
    session = AsyncMock()
    session.execute.return_value = result

    (
        returned_org_id,
        returned_user_id,
        requested_scopes,
        target_channel_id,
    ) = await _consume_state(session, "one-time-state-value")

    assert (returned_org_id, returned_user_id) == (organization_id, user_id)
    assert requested_scopes == ("https://www.googleapis.com/auth/youtube.readonly",)
    assert target_channel_id is None
    assert attempt.consumed_at is not None
    session.commit.assert_awaited_once()


@pytest.mark.anyio
@pytest.mark.parametrize(
    "attempt_overrides",
    [
        {"consumed_at": datetime.now(UTC)},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
async def test_consume_state_rejects_replayed_or_expired_state(attempt_overrides):
    attempt_data = {
        "organization_id": uuid4(),
        "user_id": uuid4(),
        "consumed_at": None,
        "expires_at": datetime.now(UTC) + timedelta(minutes=5),
        "requested_scopes": "https://www.googleapis.com/auth/youtube.readonly",
        "target_channel_id": None,
    }
    attempt_data.update(attempt_overrides)
    attempt = SimpleNamespace(**attempt_data)
    result = MagicMock()
    result.scalar_one_or_none.return_value = attempt
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(YouTubeOAuthError, match="invalid, expired, or already used"):
        await _consume_state(session, "one-time-state-value")

    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_consume_state_rejects_unknown_state():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(YouTubeOAuthError, match="invalid, expired, or already used"):
        await _consume_state(session, "unknown-state-value")

    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_validate_authorization_actor_accepts_active_verified_admin():
    organization_id = uuid4()
    user_id = uuid4()
    result = MagicMock()
    result.scalar_one_or_none.return_value = user_id
    session = AsyncMock()
    session.execute.return_value = result

    await _validate_authorization_actor(session, organization_id, user_id)

    session.execute.assert_awaited_once()
    statement = session.execute.await_args.args[0]
    sql = str(statement)
    assert "organization_members.role IN" in sql
    assert "organization_members.status" in sql
    assert "organizations.status" in sql
    assert "users.email_verified_at IS NOT NULL" in sql
    assert "users.is_active IS true" in sql


@pytest.mark.anyio
async def test_validate_authorization_actor_rejects_revoked_access():
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(YouTubeOAuthError, match="no longer authorized"):
        await _validate_authorization_actor(session, uuid4(), uuid4())


@pytest.mark.anyio
async def test_authorization_url_uses_readonly_scope_and_persists_scope_intent_and_state_hash():
    organization_id = uuid4()
    user_id = uuid4()
    settings = Settings(
        _env_file=None,
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        youtube_redirect_uri="https://api.example.com/api/v1/integrations/youtube/callback",
    )
    session = AsyncMock()
    session.add = MagicMock()

    authorization_url = await create_authorization_url(
        session, settings, organization_id, user_id
    )

    assert authorization_url.startswith(f"{GOOGLE_AUTH_URL}?")
    query = parse_qs(urlparse(authorization_url).query)
    assert set(query["scope"][0].split()) == {YOUTUBE_SCOPE, YOUTUBE_ANALYTICS_SCOPE}
    assert query["access_type"] == ["offline"]
    attempt = session.add.call_args.args[0]
    assert set(attempt.requested_scopes.split()) == {YOUTUBE_SCOPE, YOUTUBE_ANALYTICS_SCOPE}
    assert attempt.target_channel_id is None
    assert attempt.organization_id == organization_id
    assert attempt.user_id == user_id
    assert len(attempt.state_hash) == 64
    assert attempt.state_hash == hash_token(query["state"][0])
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_authorization_url_requests_upload_scope_only_for_explicit_publish_consent():
    settings = Settings(
        _env_file=None,
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        youtube_redirect_uri="https://api.example.com/api/v1/integrations/youtube/callback",
    )
    session = AsyncMock()
    session.add = MagicMock()
    organization_id = uuid4()
    user_id = uuid4()
    target_channel_id = uuid4()
    session.scalar = AsyncMock(
        return_value=SimpleNamespace(
            id=target_channel_id,
            organization_id=organization_id,
            status="active",
        )
    )

    authorization_url = await create_authorization_url(
        session,
        settings,
        organization_id,
        user_id,
        purpose="publish",
        target_channel_id=target_channel_id,
    )

    query = parse_qs(urlparse(authorization_url).query)
    assert set(query["scope"][0].split()) == set(YOUTUBE_PUBLISH_SCOPES)
    attempt = session.add.call_args.args[0]
    assert set(attempt.requested_scopes.split()) == set(YOUTUBE_PUBLISH_SCOPES)
    assert attempt.target_channel_id == target_channel_id
    session.commit.assert_awaited_once()


def test_publishing_capability_requires_read_upload_and_video_update_scopes():
    from app.services.youtube_oauth import channel_can_publish

    assert not channel_can_publish(YOUTUBE_SCOPE)
    assert not channel_can_publish(YOUTUBE_UPLOAD_SCOPE)
    assert not channel_can_publish({YOUTUBE_SCOPE, YOUTUBE_UPLOAD_SCOPE})
    assert channel_can_publish(
        {YOUTUBE_SCOPE, YOUTUBE_UPLOAD_SCOPE, YOUTUBE_VIDEO_WRITE_SCOPE}
    )


def test_granted_scope_validation_rejects_missing_upload_permission():
    from app.services.youtube_oauth import (
        YOUTUBE_UPLOAD_SCOPE,
        validate_granted_scopes,
    )

    with pytest.raises(YouTubeOAuthError, match="did not grant all requested"):
        validate_granted_scopes(
            YOUTUBE_SCOPE,
            (YOUTUBE_SCOPE, YOUTUBE_UPLOAD_SCOPE, YOUTUBE_VIDEO_WRITE_SCOPE),
        )


def test_granted_scope_validation_uses_requested_scopes_when_google_omits_scope():
    from app.services.youtube_oauth import validate_granted_scopes

    assert validate_granted_scopes(None, (YOUTUBE_SCOPE,)) == {YOUTUBE_SCOPE}


def test_granted_scope_validation_rejects_malformed_provider_scope_value():
    from app.services.youtube_oauth import validate_granted_scopes

    with pytest.raises(YouTubeOAuthError, match="invalid YouTube scope grant"):
        validate_granted_scopes([YOUTUBE_SCOPE], (YOUTUBE_SCOPE,))


@pytest.mark.anyio
async def test_consume_state_rejects_unrecognized_requested_scope():
    attempt = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        requested_scopes="https://www.googleapis.com/auth/youtube",
        target_channel_id=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = attempt
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(YouTubeOAuthError, match="unsupported requested scopes"):
        await _consume_state(session, "one-time-state-value")

    session.commit.assert_not_awaited()

@pytest.mark.anyio
async def test_publish_authorization_requires_a_channel_target():
    settings = Settings(
        _env_file=None,
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        youtube_redirect_uri="https://api.example.com/api/v1/integrations/youtube/callback",
    )
    session = AsyncMock()
    session.add = MagicMock()

    with pytest.raises(YouTubeOAuthError, match="Select a connected channel"):
        await create_authorization_url(
            session,
            settings,
            uuid4(),
            uuid4(),
            purpose="publish",
        )

    session.scalar.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_publish_authorization_rejects_channel_outside_workspace():
    from app.services.youtube_oauth import YouTubeChannelNotFoundError

    settings = Settings(
        _env_file=None,
        youtube_client_id="client-id",
        youtube_client_secret="client-secret",
        youtube_redirect_uri="https://api.example.com/api/v1/integrations/youtube/callback",
    )
    session = AsyncMock()
    session.add = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(
        YouTubeChannelNotFoundError,
        match="not active in this workspace",
    ):
        await create_authorization_url(
            session,
            settings,
            uuid4(),
            uuid4(),
            purpose="publish",
            target_channel_id=uuid4(),
        )

    session.add.assert_not_called()
    session.commit.assert_not_awaited()


@pytest.mark.anyio
async def test_consume_state_rejects_publish_attempt_without_channel_target():
    attempt = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        requested_scopes=" ".join(
            (
                YOUTUBE_SCOPE,
                "https://www.googleapis.com/auth/youtube.upload",
            )
        ),
        target_channel_id=None,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = attempt
    session = AsyncMock()
    session.execute.return_value = result

    with pytest.raises(YouTubeOAuthError, match="missing its channel target"):
        await _consume_state(session, "one-time-state-value")

    session.commit.assert_not_awaited()


def test_publishing_reconsent_rejects_a_different_google_channel():
    from app.services.youtube_oauth import validate_target_channel

    target_id = uuid4()
    target = SimpleNamespace(id=target_id, channel_id="UC-selected")
    with pytest.raises(YouTubeOAuthError, match="does not match the channel selected"):
        validate_target_channel(target_id, target, "UC-another")


def test_publishing_reconsent_requires_target_channel_to_remain_active():
    from app.services.youtube_oauth import validate_target_channel

    with pytest.raises(YouTubeOAuthError, match="does not match the channel selected"):
        validate_target_channel(uuid4(), None, "UC-returned")


def test_publishing_reconsent_accepts_the_selected_channel():
    from app.services.youtube_oauth import validate_target_channel

    target_id = uuid4()
    target = SimpleNamespace(id=target_id, channel_id="UC-selected")
    assert validate_target_channel(target_id, target, "UC-selected") is target



@pytest.mark.anyio
async def test_consume_state_returns_the_bound_channel_for_publish_consent():
    target_channel_id = uuid4()
    attempt = SimpleNamespace(
        organization_id=uuid4(),
        user_id=uuid4(),
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        requested_scopes=" ".join(
            (
                YOUTUBE_SCOPE,
                "https://www.googleapis.com/auth/youtube.upload",
            )
        ),
        target_channel_id=target_channel_id,
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = attempt
    session = AsyncMock()
    session.execute.return_value = result

    _, _, requested_scopes, returned_target_id = await _consume_state(
        session,
        "one-time-state-value",
    )

    assert set(requested_scopes) == {
        YOUTUBE_SCOPE,
        "https://www.googleapis.com/auth/youtube.upload",
    }
    assert returned_target_id == target_channel_id
    session.commit.assert_awaited_once()


def test_normalize_channel_items_deduplicates_provider_duplicates_and_filters_thumbnail_urls():
    channels = _normalize_channel_items(
        [
            {
                "id": "UC-first",
                "snippet": {
                    "title": "First channel",
                    "customUrl": "@first",
                    "thumbnails": {"default": {"url": "https://img.example/first.jpg"}},
                },
            },
            {
                "id": "UC-first",
                "snippet": {"title": "Duplicate metadata"},
            },
            {
                "id": "UC-second",
                "snippet": {
                    "title": "Second channel",
                    "thumbnails": {"default": {"url": "http://unsafe.example/image.jpg"}},
                },
            },
        ]
    )

    assert [item["channel_id"] for item in channels] == ["UC-first", "UC-second"]
    assert channels[0]["title"] == "First channel"
    assert channels[0]["thumbnail_url"] == "https://img.example/first.jpg"
    assert channels[1]["thumbnail_url"] is None


@pytest.mark.parametrize(
    "items",
    [
        [{"id": "", "snippet": {"title": "Missing ID"}}],
        [{"id": "x" * 129, "snippet": {"title": "Oversized ID"}}],
        [{"id": "UC-valid", "snippet": None}],
        [None],
        {},
    ],
)
def test_normalize_channel_items_rejects_malformed_provider_entries(items):
    with pytest.raises(YouTubeOAuthError):
        _normalize_channel_items(items)


@pytest.mark.anyio
async def test_fetch_all_channels_follows_pages_and_deduplicates_channel_ids():
    first = MagicMock()
    first.raise_for_status = MagicMock()
    first.json.return_value = {
        "items": [{"id": "UC-one", "snippet": {"title": "One"}}],
        "nextPageToken": "next-page",
    }
    second = MagicMock()
    second.raise_for_status = MagicMock()
    second.json.return_value = {
        "items": [
            {"id": "UC-one", "snippet": {"title": "Duplicate"}},
            {"id": "UC-two", "snippet": {"title": "Two"}},
        ]
    }
    client = AsyncMock()
    client.get.side_effect = [first, second]

    channels = await _fetch_all_channels(client, "access-token")

    assert [item["channel_id"] for item in channels] == ["UC-one", "UC-two"]
    assert channels[0]["title"] == "One"
    assert client.get.await_count == 2
    assert client.get.await_args_list[1].kwargs["params"]["pageToken"] == "next-page"


@pytest.mark.anyio
async def test_select_pending_channel_binds_selection_to_candidate_and_clears_pending_data():
    organization_id = uuid4()
    user_id = uuid4()
    attempt_id = uuid4()
    now = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        organization_id=organization_id,
        channel_id="UC-selected",
        title="Old title",
        custom_url=None,
        thumbnail_url=None,
        encrypted_refresh_token="existing-encrypted-token",
        key_version=1,
        granted_scopes=YOUTUBE_SCOPE,
        connected_by_user_id=user_id,
        status="active",
    )
    attempt = SimpleNamespace(
        id=attempt_id,
        organization_id=organization_id,
        user_id=user_id,
        selection_completed_at=None,
        pending_channels=[
            {
                "channel_id": "UC-selected",
                "title": "Selected channel",
                "custom_url": "@selected",
                "thumbnail_url": None,
            }
        ],
        pending_selection_expires_at=now + timedelta(minutes=5),
        pending_encrypted_refresh_token=None,
        pending_key_version=None,
        pending_granted_scopes=YOUTUBE_SCOPE,
    )
    actor_result = MagicMock()
    actor_result.scalar_one_or_none.return_value = user_id
    existing_result = MagicMock()
    existing_result.scalar_one_or_none.return_value = existing
    session = AsyncMock()
    session.scalar.return_value = attempt
    session.execute.side_effect = [actor_result, existing_result]

    selected = await select_pending_channel(
        session,
        Settings(_env_file=None),
        organization_id,
        user_id,
        attempt_id,
        "UC-selected",
    )

    assert selected is existing
    assert existing.title == "Selected channel"
    assert existing.custom_url == "@selected"
    assert attempt.pending_channels is None
    assert attempt.pending_granted_scopes is None
    assert attempt.selection_completed_at is not None
    session.commit.assert_awaited_once()


@pytest.mark.anyio
async def test_select_pending_channel_rejects_channel_not_in_provider_candidates():
    organization_id = uuid4()
    user_id = uuid4()
    attempt = SimpleNamespace(
        id=uuid4(),
        organization_id=organization_id,
        user_id=user_id,
        selection_completed_at=None,
        pending_channels=[{"channel_id": "UC-allowed"}],
        pending_selection_expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    actor_result = MagicMock()
    actor_result.scalar_one_or_none.return_value = user_id
    session = AsyncMock()
    session.scalar.return_value = attempt
    session.execute.return_value = actor_result

    with pytest.raises(YouTubeChannelNotFoundError, match="not part of this authorization"):
        await select_pending_channel(
            session,
            Settings(_env_file=None),
            organization_id,
            user_id,
            attempt.id,
            "UC-not-allowed",
        )

    session.commit.assert_not_awaited()
