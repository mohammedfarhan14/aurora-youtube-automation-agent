import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.database.engine import get_session_factory
from app.models.email_verification import EmailVerificationToken
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.auth import register_user
from app.services.email_verification import (
    EMAIL_VERIFICATION_COOLDOWN,
    EMAIL_VERIFICATION_EXPIRY,
    EMAIL_VERIFICATION_MAX_ATTEMPTS,
    EMAIL_VERIFICATION_WINDOW,
    EmailAlreadyVerifiedError,
    EmailVerificationRateLimitError,
    InvalidEmailVerificationTokenError,
    issue_email_verification,
    verify_email,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def create_test_user() -> User:
    email = f"verification-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        return await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Email Verification Test",
                password="Aurora-Secure-Password-2026",
            ),
        )


async def cleanup_test_user(user_id):
    async with get_session_factory()() as session:
        async with session.begin():
            await session.execute(
                delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id)
            )
            await session.execute(delete(User).where(User.id == user_id))


@pytest.mark.anyio
async def test_issue_email_verification_creates_hashed_token():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                issued = await issue_email_verification(session, user.id)

        async with get_session_factory()() as session:
            token_record = await session.scalar(
                select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
            )

            assert token_record is not None
            assert token_record.token_hash != issued.token
            assert len(token_record.token_hash) == 64
            assert token_record.consumed_at is None

            now = datetime.now(UTC)

            assert issued.expires_at > now
            assert issued.expires_at <= now + EMAIL_VERIFICATION_EXPIRY

    finally:
        await cleanup_test_user(user.id)



@pytest.mark.anyio
async def test_issue_email_verification_replaces_previous_token():
    user = await create_test_user()

    try:
        # First issuance.
        async with get_session_factory()() as session:
            async with session.begin():
                first = await issue_email_verification(session, user.id)

        # Move the first token's timestamp six minutes into the past.
        # This allows the next issuance to pass the five-minute cooldown.
        async with get_session_factory()() as session:
            async with session.begin():
                first_record = await session.scalar(
                    select(EmailVerificationToken).where(
                        EmailVerificationToken.user_id == user.id
                    )
                )
                assert first_record is not None
                first_record.created_at = (
                    datetime.now(UTC)
                    - EMAIL_VERIFICATION_COOLDOWN
                    - timedelta(minutes=1)
                )

        # Second issuance.
        async with get_session_factory()() as session:
            async with session.begin():
                second = await issue_email_verification(session, user.id)

        assert first.token != second.token

        # Confirm the first token was invalidated and the second remains active.
        async with get_session_factory()() as session:
            tokens = list(
                await session.scalars(
                    select(EmailVerificationToken)
                    .where(EmailVerificationToken.user_id == user.id)
                    .order_by(EmailVerificationToken.created_at)
                )
            )

            assert len(tokens) == 2
            assert tokens[0].consumed_at is not None
            assert tokens[1].consumed_at is None

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_issue_email_verification_enforces_cooldown():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                await issue_email_verification(session, user.id)

        async with get_session_factory()() as session:
            with pytest.raises(EmailVerificationRateLimitError) as exc_info:
                async with session.begin():
                    await issue_email_verification(session, user.id)

        assert exc_info.value.retry_after_seconds > 0
        assert (
            exc_info.value.retry_after_seconds
            <= EMAIL_VERIFICATION_COOLDOWN.total_seconds()
        )

    finally:
        await cleanup_test_user(user.id)




@pytest.mark.anyio
async def test_issue_email_verification_enforces_daily_limit():
    user = await create_test_user()

    try:
        for attempt in range(EMAIL_VERIFICATION_MAX_ATTEMPTS):
            async with get_session_factory()() as session:
                async with session.begin():
                    await issue_email_verification(session, user.id)

            # Give each attempt a distinct timestamp, spaced six minutes
            # apart, while keeping all attempts inside the 24-hour window.
            async with get_session_factory()() as session:
                async with session.begin():
                    token = await session.scalar(
                        select(EmailVerificationToken)
                        .where(EmailVerificationToken.user_id == user.id)
                        .order_by(EmailVerificationToken.created_at.desc())
                    )
                    assert token is not None
                    token.created_at = (
                        datetime.now(UTC)
                        - EMAIL_VERIFICATION_COOLDOWN
                        * (EMAIL_VERIFICATION_MAX_ATTEMPTS - attempt)
                        - timedelta(minutes=1)
                    )

        async with get_session_factory()() as session:
            with pytest.raises(EmailVerificationRateLimitError) as exc_info:
                async with session.begin():
                    await issue_email_verification(session, user.id)

        assert exc_info.value.retry_after_seconds > 0
        assert (
            exc_info.value.retry_after_seconds
            <= EMAIL_VERIFICATION_WINDOW.total_seconds()
        )

    finally:
        await cleanup_test_user(user.id)



@pytest.mark.anyio
async def test_verify_email_marks_user_as_verified():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                issued = await issue_email_verification(session, user.id)

        async with get_session_factory()() as session:
            async with session.begin():
                verified_user = await verify_email(session, issued.token)

                assert verified_user.id == user.id
                assert verified_user.email_verified_at is not None

        async with get_session_factory()() as session:
            persisted_user = await session.get(User, user.id)

            assert persisted_user is not None
            assert persisted_user.email_verified_at is not None

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_verify_email_rejects_reuse_of_consumed_token():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                issued = await issue_email_verification(session, user.id)

        async with get_session_factory()() as session:
            async with session.begin():
                await verify_email(session, issued.token)

        async with get_session_factory()() as session:
            with pytest.raises(InvalidEmailVerificationTokenError):
                async with session.begin():
                    await verify_email(session, issued.token)

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_verify_email_rejects_expired_token():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                issued = await issue_email_verification(session, user.id)

        async with get_session_factory()() as session:
            async with session.begin():
                token_record = await session.scalar(
                    select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
                )

                assert token_record is not None

                now = datetime.now(UTC)
                token_record.created_at = now - timedelta(minutes=2)
                token_record.expires_at = now - timedelta(minutes=1)

        async with get_session_factory()() as session:
            with pytest.raises(InvalidEmailVerificationTokenError):
                async with session.begin():
                    await verify_email(session, issued.token)

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_verify_email_rejects_unknown_token():
    user = await create_test_user()

    try:
        invalid_token = "invalid-token-" + uuid4().hex

        async with get_session_factory()() as session:
            with pytest.raises(InvalidEmailVerificationTokenError):
                async with session.begin():
                    await verify_email(session, invalid_token)

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_issue_email_verification_rejects_already_verified_user():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                current_user = await session.get(User, user.id)
                current_user.email_verified_at = datetime.now(UTC)

        async with get_session_factory()() as session:
            with pytest.raises(EmailAlreadyVerifiedError):
                async with session.begin():
                    await issue_email_verification(session, user.id)

    finally:
        await cleanup_test_user(user.id)


@pytest.mark.anyio
async def test_concurrent_verification_allows_only_one_success():
    user = await create_test_user()

    try:
        async with get_session_factory()() as session:
            async with session.begin():
                issued = await issue_email_verification(session, user.id)

        async def attempt_verification():
            async with get_session_factory()() as session:
                try:
                    async with session.begin():
                        await verify_email(session, issued.token)
                    return "success"
                except InvalidEmailVerificationTokenError:
                    return "rejected"

        results = await asyncio.gather(
            attempt_verification(),
            attempt_verification(),
        )

        assert results.count("success") == 1
        assert results.count("rejected") == 1

        async with get_session_factory()() as session:
            persisted_user = await session.get(User, user.id)
            token_record = await session.scalar(
                select(EmailVerificationToken).where(EmailVerificationToken.user_id == user.id)
            )

            assert persisted_user.email_verified_at is not None
            assert token_record.consumed_at is not None

    finally:
        await cleanup_test_user(user.id)
