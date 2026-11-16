from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import ceil
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tokens import generate_secure_token, hash_token
from app.models.email_verification import EmailVerificationToken
from app.models.user import User

EMAIL_VERIFICATION_EXPIRY = timedelta(hours=24)
EMAIL_VERIFICATION_COOLDOWN = timedelta(minutes=5)
EMAIL_VERIFICATION_WINDOW = timedelta(hours=24)
EMAIL_VERIFICATION_MAX_ATTEMPTS = 5


class EmailVerificationError(Exception):
    """Base exception for email verification failures."""


class UserNotFoundError(EmailVerificationError):
    """Raised when the requested user does not exist."""


class EmailAlreadyVerifiedError(EmailVerificationError):
    """Raised when the user's email is already verified."""


class InvalidEmailVerificationTokenError(EmailVerificationError):
    """Raised when a verification token is invalid, expired, or consumed."""

class EmailVerificationRateLimitError(EmailVerificationError):
    """Raised when verification email issuance is rate-limited."""

    def __init__(self, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__("Too many verification email requests.")


@dataclass(frozen=True)
class IssuedEmailVerification:
    """Contains the raw token for delivery and its expiration."""

    token: str
    expires_at: datetime


async def issue_email_verification(
    session: AsyncSession,
    user_id: UUID,
) -> IssuedEmailVerification:
    """Issue a new verification token and invalidate previous tokens.

    The caller must manage and commit the database transaction.
    """

    if not session.in_transaction():
        raise RuntimeError("An active database transaction is required.")

    # Lock the user first to serialize concurrent issuance requests.
    user = await session.scalar(
        select(User)
        .where(User.id == user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or not user.is_active:
        raise UserNotFoundError("User not found.")

    if user.email_verified_at is not None:
        raise EmailAlreadyVerifiedError("Email address is already verified.")

    now = datetime.now(UTC)
    window_start = now - EMAIL_VERIFICATION_WINDOW

    # Read issuance history while holding the user lock.
    # The user lock serializes concurrent requests for this account.
    attempts_result = await session.execute(
        select(
            func.count(EmailVerificationToken.id),
            func.min(EmailVerificationToken.created_at),
            func.max(EmailVerificationToken.created_at),
        ).where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.created_at >= window_start,
        )
    )

    attempt_count, oldest_attempt, latest_attempt = attempts_result.one()

    retry_after = 0

    if attempt_count >= EMAIL_VERIFICATION_MAX_ATTEMPTS:
        daily_retry_after = ceil(
            (
                oldest_attempt + EMAIL_VERIFICATION_WINDOW - now
            ).total_seconds()
        )
        retry_after = max(retry_after, daily_retry_after)

    if latest_attempt is not None:
        cooldown_retry_after = ceil(
            (
                latest_attempt + EMAIL_VERIFICATION_COOLDOWN - now
            ).total_seconds()
        )
        if cooldown_retry_after > 0:
            retry_after = max(retry_after, cooldown_retry_after)

    if retry_after > 0:
        raise EmailVerificationRateLimitError(retry_after)

    # Invalidate all previously issued, unconsumed tokens.
    existing_tokens = await session.scalars(
        select(EmailVerificationToken)
        .where(
            EmailVerificationToken.user_id == user.id,
            EmailVerificationToken.consumed_at.is_(None),
        )
        .with_for_update()
    )

    for existing_token in existing_tokens:
        existing_token.consumed_at = now

    raw_token = generate_secure_token()
    expires_at = now + EMAIL_VERIFICATION_EXPIRY

    verification_token = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=expires_at,
    )

    session.add(verification_token)
    await session.flush()

    return IssuedEmailVerification(
        token=raw_token,
        expires_at=expires_at,
    )


async def verify_email(
    session: AsyncSession,
    raw_token: str,
) -> User:
    """Verify a user's email using a valid, single-use token.

    The caller must manage and commit the database transaction.
    """

    if not session.in_transaction():
        raise RuntimeError("An active database transaction is required.")

    if not raw_token:
        raise InvalidEmailVerificationTokenError("Invalid or expired verification token.")

    token_hash = hash_token(raw_token)

    # Initial lookup identifies the user. Do not lock the token yet:
    # issuance and verification must acquire locks in the same order.
    candidate = await session.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == token_hash)
    )

    if candidate is None:
        raise InvalidEmailVerificationTokenError("Invalid or expired verification token.")

    # Lock the user first, matching the issuance lock order.
    user = await session.scalar(
        select(User)
        .where(User.id == candidate.user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if user is None or not user.is_active:
        raise InvalidEmailVerificationTokenError("Invalid or expired verification token.")

    # Re-read and lock the token after acquiring the user lock.
    verification_token = await session.scalar(
        select(EmailVerificationToken)
        .where(
            EmailVerificationToken.id == candidate.id,
            EmailVerificationToken.token_hash == token_hash,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    now = datetime.now(UTC)

    if (
        verification_token is None
        or verification_token.consumed_at is not None
        or verification_token.expires_at <= now
        or user.email_verified_at is not None
    ):
        raise InvalidEmailVerificationTokenError("Invalid or expired verification token.")

    verification_token.consumed_at = now
    user.email_verified_at = now

    await session.flush()

    return user
