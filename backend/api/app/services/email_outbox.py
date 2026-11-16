from urllib.parse import urlencode
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.email_outbox import EmailOutbox
from app.models.user import User
from app.services.email import EmailMessage
from app.services.email_outbox_crypto import (
    encrypt_payload,
    get_active_encryption_key,
)
from app.services.email_verification import IssuedEmailVerification


async def enqueue_email_message(
    session: AsyncSession,
    message: EmailMessage,
    *,
    message_type: str,
    user_id: UUID | None = None,
    settings: Settings | None = None,
) -> EmailOutbox:
    """Encrypt and enqueue an email inside the caller's transaction."""

    if not session.in_transaction():
        raise RuntimeError("An active database transaction is required.")

    if not message_type.strip():
        raise ValueError("Email message type cannot be empty.")

    settings = settings or get_settings()

    active_key_version, encryption_key = get_active_encryption_key(settings)

    encrypted_payload = encrypt_payload(
        message.text_body,
        encryption_key,
        key_version=active_key_version,
    )

    outbox_message = EmailOutbox(
        user_id=user_id,
        recipient=message.to_email,
        subject=message.subject,
        message_type=message_type,
        encrypted_payload=encrypted_payload,
        key_version=active_key_version,
    )

    session.add(outbox_message)
    await session.flush()

    return outbox_message


async def enqueue_verification_email(
    session: AsyncSession,
    user: User,
    issued_verification: IssuedEmailVerification,
    *,
    settings: Settings | None = None,
) -> EmailOutbox:
    """Build and enqueue an encrypted email verification message."""

    settings = settings or get_settings()

    verification_query = urlencode(
        {"token": issued_verification.token}
    )
    verification_url = (
        f"{settings.frontend_url.rstrip('/')}/verify-email?"
        f"{verification_query}"
    )

    message = EmailMessage(
        to_email=user.email,
        subject="Verify your Aurora Studio email address",
        text_body=(
            f"Hello {user.display_name},\n\n"
            "Please verify your Aurora Studio email address "
            "using the link below:\n\n"
            f"{verification_url}\n\n"
            f"This link expires at "
            f"{issued_verification.expires_at.isoformat()}.\n\n"
            "If you did not create this account, you can ignore this email."
        ),
    )

    return await enqueue_email_message(
        session,
        message,
        message_type="email_verification",
        user_id=user.id,
        settings=settings,
    )