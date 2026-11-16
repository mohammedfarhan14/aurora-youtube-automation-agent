
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.core.config import Settings
from app.database.engine import get_session_factory
from app.models.email_outbox import EmailOutbox
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.auth import register_user
from app.services.email import EmailMessage
from app.services.email_outbox import enqueue_email_message
from app.services.email_outbox_crypto import decrypt_payload

TEST_KEY = "a" * 64


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        _env_file=None,
        email_outbox_encryption_key=TEST_KEY,
    )


async def create_test_user() -> User:
    email = f"outbox-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        return await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Email Outbox Test",
                password="Aurora-Secure-Password-2026",
            ),
        )


async def cleanup_test_data(user_id) -> None:
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                delete(EmailOutbox).where(EmailOutbox.user_id == user_id)
            )
            await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.anyio
async def test_enqueue_email_message_persists_encrypted_message(
    test_settings,
) -> None:
    user = await create_test_user()

    try:
        message = EmailMessage(
            to_email=user.email,
            subject="Verify your Aurora Studio email address",
            text_body="Sensitive verification link: https://example.com/token",
        )

        async with get_session_factory()() as session:
            async with session.begin():
                outbox_message = await enqueue_email_message(
                    session,
                    message,
                    message_type="email_verification",
                    user_id=user.id,
                    settings=test_settings,
                )

                outbox_id = outbox_message.id

        async with get_session_factory()() as session:
            persisted = await session.get(EmailOutbox, outbox_id)

            assert persisted is not None
            assert persisted.user_id == user.id
            assert persisted.recipient == user.email
            assert persisted.subject == message.subject
            assert persisted.message_type == "email_verification"
            assert persisted.status == "pending"
            assert persisted.attempts == 0
            assert persisted.key_version == 1
            assert persisted.sent_at is None
            assert persisted.locked_at is None
            assert persisted.locked_by is None

            assert message.text_body not in persisted.encrypted_payload

            decrypted = decrypt_payload(
                persisted.encrypted_payload,
                {1: TEST_KEY},
            )

            assert decrypted == message.text_body

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_enqueue_email_message_requires_active_transaction() -> None:
    message = EmailMessage(
        to_email="recipient@example.com",
        subject="Test email",
        text_body="Test body",
    )

    async with get_session_factory()() as session:
        with pytest.raises(
            RuntimeError,
            match="active database transaction",
        ):
            await enqueue_email_message(
                session,
                message,
                message_type="test",
                settings=Settings(
                    _env_file=None,
                    email_outbox_encryption_key=TEST_KEY,
                ),
            )


@pytest.mark.anyio
async def test_enqueue_email_message_requires_encryption_key(monkeypatch) -> None:
    monkeypatch.delenv("EMAIL_OUTBOX_ENCRYPTION_KEY", raising=False)
    user = await create_test_user()

    try:
        message = EmailMessage(
            to_email=user.email,
            subject="Test email",
            text_body="Test body",
        )

        async with get_session_factory()() as session:
            with pytest.raises(
                RuntimeError,
                match="encryption key is not configured",
            ):
                async with session.begin():
                    await enqueue_email_message(
                        session,
                        message,
                        message_type="test",
                        user_id=user.id,
                        settings=Settings(_env_file=None),
                    )

    finally:
        await cleanup_test_data(user.id)


@pytest.mark.anyio
async def test_enqueue_email_message_is_removed_on_transaction_rollback(
    test_settings,
) -> None:
    user = await create_test_user()
    outbox_id = None

    try:
        message = EmailMessage(
            to_email=user.email,
            subject="Rollback test",
            text_body="This message must not be persisted.",
        )

        with pytest.raises(RuntimeError, match="force rollback"):
            async with get_session_factory()() as session:
                async with session.begin():
                    outbox_message = await enqueue_email_message(
                        session,
                        message,
                        message_type="test",
                        user_id=user.id,
                        settings=test_settings,
                    )

                    outbox_id = outbox_message.id

                    raise RuntimeError("force rollback")

        assert outbox_id is not None

        async with get_session_factory()() as session:
            persisted = await session.scalar(
                select(EmailOutbox).where(EmailOutbox.id == outbox_id)
            )

            assert persisted is None

    finally:
        await cleanup_test_data(user.id)
