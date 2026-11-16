import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select

import app.services.encryption_key_rotation as rotation_module
from app.core.config import Settings
from app.database.engine import get_session_factory
from app.models.email_outbox import EmailOutbox
from app.models.organization import Organization
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.services.email_outbox_crypto import decrypt_payload, encrypt_payload
from app.services.encryption_key_rotation import (
    _rotate_domain,
    rotate_email_outbox_batch,
    rotate_youtube_tokens_batch,
)
from app.services.youtube_crypto import decrypt_refresh_token, encrypt_refresh_token

OLD_EMAIL_KEY = "a" * 64
NEW_EMAIL_KEY = "b" * 64
OLD_YOUTUBE_KEY = "c" * 64
NEW_YOUTUBE_KEY = "d" * 64


def rotation_settings() -> Settings:
    return Settings(
        _env_file=None,
        email_outbox_encryption_keyring=json.dumps({"1": OLD_EMAIL_KEY, "2": NEW_EMAIL_KEY}),
        email_outbox_active_key_version=2,
        integration_token_encryption_keyring=json.dumps(
            {"1": OLD_YOUTUBE_KEY, "2": NEW_YOUTUBE_KEY}
        ),
        integration_token_active_key_version=2,
    )


def corrupt_ciphertext(value: str) -> str:
    version, nonce, ciphertext = value.split(":")
    first = "A" if ciphertext[0] != "A" else "B"
    return f"{version}:{nonce}:{first}{ciphertext[1:]}"


async def create_email_rows(bodies: list[str]) -> list[tuple[UUID, str]]:
    now = datetime.now(UTC)
    rows = [
        EmailOutbox(
            recipient=f"rotation-{uuid4().hex}@example.com",
            subject="Rotation integration test",
            message_type="test",
            encrypted_payload=encrypt_payload(body, OLD_EMAIL_KEY, key_version=1),
            key_version=1,
            status="pending",
            attempts=0,
            available_at=now,
            created_at=now + timedelta(microseconds=index),
        )
        for index, body in enumerate(bodies)
    ]
    async with get_session_factory()() as session:
        async with session.begin():
            session.add_all(rows)
            await session.flush()
            return [(row.id, row.encrypted_payload) for row in rows]


async def cleanup_email_rows(ids: list[UUID]) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(delete(EmailOutbox).where(EmailOutbox.id.in_(ids)))


async def read_email_rows(ids: list[UUID]) -> list[EmailOutbox]:
    async with get_session_factory()() as session:
        result = await session.scalars(
            select(EmailOutbox).where(EmailOutbox.id.in_(ids)).order_by(EmailOutbox.created_at)
        )
        return list(result.all())


async def create_youtube_workspace() -> tuple[UUID, UUID]:
    suffix = uuid4().hex
    async with get_session_factory()() as session:
        async with session.begin():
            user = User(
                email=f"youtube-rotation-{suffix}@example.com",
                display_name="Rotation Test",
            )
            session.add(user)
            await session.flush()
            organization = Organization(
                name="Rotation Test Workspace",
                slug=f"rotation-test-{suffix}",
                created_by_user_id=user.id,
                plan="free",
                status="active",
            )
            session.add(organization)
            await session.flush()
            return user.id, organization.id


async def create_youtube_rows(
    user_id: UUID,
    organization_id: UUID,
    tokens: list[str],
) -> list[tuple[UUID, str, str]]:
    now = datetime.now(UTC)
    rows = []
    for index, token in enumerate(tokens):
        channel_id = f"UC-rotation-{uuid4().hex}"
        encrypted = (
            encrypt_refresh_token(
                token,
                OLD_YOUTUBE_KEY,
                organization_id,
                channel_id,
                key_version=1,
            )
            if token
            else ""
        )
        rows.append(
            YouTubeChannel(
                organization_id=organization_id,
                channel_id=channel_id,
                title=f"Rotation channel {index}",
                encrypted_refresh_token=encrypted,
                key_version=1,
                granted_scopes="https://www.googleapis.com/auth/youtube.readonly",
                connected_by_user_id=user_id,
                status="active",
                connected_at=now + timedelta(microseconds=index),
            )
        )
    async with get_session_factory()() as session:
        async with session.begin():
            session.add_all(rows)
            await session.flush()
            return [(row.id, row.channel_id, row.encrypted_refresh_token) for row in rows]


async def read_youtube_rows(ids: list[UUID]) -> list[YouTubeChannel]:
    async with get_session_factory()() as session:
        result = await session.scalars(
            select(YouTubeChannel)
            .where(YouTubeChannel.id.in_(ids))
            .order_by(YouTubeChannel.connected_at)
        )
        return list(result.all())


async def cleanup_youtube_workspace(user_id: UUID, organization_id: UUID) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(delete(Organization).where(Organization.id == organization_id))
            await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.anyio
async def test_email_rotation_resumes_after_batch_limit(monkeypatch) -> None:
    settings = rotation_settings()
    created = await create_email_rows(["first", "second", "third"])
    ids = [row_id for row_id, _ in created]
    monkeypatch.setattr(
        rotation_module,
        "get_settings",
        lambda: settings,
    )

    # The production rotation command intentionally scans the entire queue.
    # This orchestration test scopes its batch/count collaborators to its own
    # fixture rows; the real global batch implementation is covered separately.
    real_count_remaining = rotation_module._count_remaining

    async def rotate_fixture_batch(batch_settings, batch_size):
        keys = rotation_module.get_email_keys(batch_settings)
        active_version, active_key = rotation_module.get_active_email_key(batch_settings)
        rotated = 0
        async with get_session_factory()() as session:
            async with session.begin():
                result = await session.scalars(
                    select(EmailOutbox)
                    .where(
                        EmailOutbox.id.in_(ids),
                        EmailOutbox.key_version != active_version,
                    )
                    .order_by(EmailOutbox.created_at, EmailOutbox.id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
                for row in result:
                    rotated += int(
                        rotation_module.rotate_email_outbox_record(
                            row, keys, active_version, active_key
                        )
                    )
        return rotated

    async def count_fixture_remaining(domain, active_version):
        if domain != "email":
            return await real_count_remaining(domain, active_version)
        async with get_session_factory()() as session:
            result = await session.scalar(
                select(EmailOutbox.id)
                .where(
                    EmailOutbox.id.in_(ids),
                    EmailOutbox.key_version != active_version,
                )
                .limit(1)
            )
            return int(result is not None)

    monkeypatch.setattr(rotation_module, "rotate_email_outbox_batch", rotate_fixture_batch)
    monkeypatch.setattr(rotation_module, "_count_remaining", count_fixture_remaining)

    try:
        with pytest.raises(RuntimeError, match="batch limit"):
            await _rotate_domain("email", batch_size=1, max_batches=1)

        assert await _rotate_domain("email", batch_size=1, max_batches=5) == 2
        rows = await read_email_rows(ids)
        assert [row.key_version for row in rows] == [2, 2, 2]
        assert [
            decrypt_payload(
                row.encrypted_payload,
                {1: OLD_EMAIL_KEY, 2: NEW_EMAIL_KEY},
                expected_key_version=2,
            )
            for row in rows
        ] == ["first", "second", "third"]
    finally:
        await cleanup_email_rows(ids)


@pytest.mark.anyio
async def test_email_rotation_batch_rolls_back_all_rows_on_decryption_failure() -> None:
    settings = rotation_settings()
    created = await create_email_rows(["valid-before-corrupt", "corrupt"])
    ids = [row_id for row_id, _ in created]
    original_ciphertexts = [ciphertext for _, ciphertext in created]

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                corrupt = await session.get(EmailOutbox, ids[1])
                assert corrupt is not None
                corrupt.encrypted_payload = corrupt_ciphertext(corrupt.encrypted_payload)

        with pytest.raises(ValueError, match="authentication failed"):
            await rotate_email_outbox_batch(settings, batch_size=1000)

        rows = await read_email_rows(ids)
        assert [row.key_version for row in rows] == [1, 1]
        assert rows[0].encrypted_payload == original_ciphertexts[0]
        assert rows[1].encrypted_payload != original_ciphertexts[1]
        assert (
            decrypt_payload(
                rows[0].encrypted_payload,
                {1: OLD_EMAIL_KEY},
                expected_key_version=1,
            )
            == "valid-before-corrupt"
        )
    finally:
        await cleanup_email_rows(ids)


@pytest.mark.anyio
async def test_youtube_rotation_resumes_and_ignores_empty_disconnected_tokens() -> None:
    settings = rotation_settings()
    user_id, organization_id = await create_youtube_workspace()
    try:
        created = await create_youtube_rows(
            user_id,
            organization_id,
            ["first", "", "third"],
        )
        ids = [row_id for row_id, _, _ in created]

        assert (
            await rotate_youtube_tokens_batch(
                settings, batch_size=1, organization_id=organization_id
            )
            == 1
        )
        assert (
            await rotate_youtube_tokens_batch(
                settings, batch_size=1, organization_id=organization_id
            )
            == 1
        )
        assert (
            await rotate_youtube_tokens_batch(
                settings, batch_size=1, organization_id=organization_id
            )
            == 0
        )

        rows = await read_youtube_rows(ids)
        assert [row.key_version for row in rows] == [2, 1, 2]
        assert rows[1].encrypted_refresh_token == ""
        for row, expected in ((rows[0], "first"), (rows[2], "third")):
            assert (
                decrypt_refresh_token(
                    row.encrypted_refresh_token,
                    {1: OLD_YOUTUBE_KEY, 2: NEW_YOUTUBE_KEY},
                    organization_id,
                    row.channel_id,
                    row.key_version,
                )
                == expected
            )
    finally:
        await cleanup_youtube_workspace(user_id, organization_id)


@pytest.mark.anyio
async def test_youtube_rotation_batch_rolls_back_all_rows_on_decryption_failure() -> None:
    settings = rotation_settings()
    user_id, organization_id = await create_youtube_workspace()
    try:
        created = await create_youtube_rows(
            user_id,
            organization_id,
            ["valid-before-corrupt", "corrupt"],
        )
        ids = [row_id for row_id, _, _ in created]
        original_ciphertexts = [ciphertext for _, _, ciphertext in created]

        async with get_session_factory()() as session:
            async with session.begin():
                corrupt = await session.get(YouTubeChannel, ids[1])
                assert corrupt is not None
                corrupt.encrypted_refresh_token = corrupt_ciphertext(
                    corrupt.encrypted_refresh_token
                )

        with pytest.raises(ValueError, match="authentication failed"):
            await rotate_youtube_tokens_batch(
                settings, batch_size=1000, organization_id=organization_id
            )

        rows = await read_youtube_rows(ids)
        assert [row.key_version for row in rows] == [1, 1]
        assert rows[0].encrypted_refresh_token == original_ciphertexts[0]
        assert rows[1].encrypted_refresh_token != original_ciphertexts[1]
        assert (
            decrypt_refresh_token(
                rows[0].encrypted_refresh_token,
                {1: OLD_YOUTUBE_KEY},
                organization_id,
                rows[0].channel_id,
                1,
            )
            == "valid-before-corrupt"
        )
    finally:
        await cleanup_youtube_workspace(user_id, organization_id)


def test_key_rotation_cli_defaults_to_email_and_requires_explicit_all():
    from app.services.encryption_key_rotation import build_parser

    parser = build_parser()
    assert parser.parse_args([]).domain == "email"
    assert parser.parse_args(["--domain", "all"]).domain == "all"
