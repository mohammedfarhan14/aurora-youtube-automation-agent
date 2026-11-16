from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.passwords import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    verify_password,
)
from app.core.security import create_access_token
from app.models.identity import UserCredential
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.email_outbox import enqueue_verification_email
from app.services.email_verification import issue_email_verification


class EmailAlreadyRegisteredError(Exception):
    """Raised when an email address is already registered."""


class InvalidCredentialsError(Exception):
    """Raised when login credentials are invalid."""


async def _create_user_and_credential(
    session: AsyncSession,
    payload: RegisterRequest,
) -> User:
    """Create a user and password credential inside an active transaction."""

    email = str(payload.email).strip().lower()
    display_name = payload.display_name.strip()

    if not display_name:
        raise ValueError("Display name cannot be empty.")

    user = User(
        email=email,
        display_name=display_name,
    )

    session.add(user)
    await session.flush()

    credential = UserCredential(
        user_id=user.id,
        password_hash=hash_password(payload.password),
    )

    session.add(credential)
    await session.flush()

    return user


def _handle_registration_integrity_error(exc: IntegrityError) -> None:
    """Translate the unique-email constraint into a domain exception."""

    original_error = exc.orig
    diagnostic = getattr(original_error, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)

    if constraint_name == "uq_users_email":
        raise EmailAlreadyRegisteredError(
            "An account with this email already exists."
        ) from exc

    raise exc


async def register_user(
    session: AsyncSession,
    payload: RegisterRequest,
) -> User:
    """Register a user and their password credential atomically."""

    try:
        async with session.begin():
            user = await _create_user_and_credential(session, payload)

    except IntegrityError as exc:
        _handle_registration_integrity_error(exc)

    return user


async def register_user_with_email_verification(
    session: AsyncSession,
    payload: RegisterRequest,
) -> User:
    """Register a user and queue their verification email atomically."""

    try:
        async with session.begin():
            user = await _create_user_and_credential(session, payload)

            issued_verification = await issue_email_verification(
                session,
                user.id,
            )

            await enqueue_verification_email(
                session,
                user,
                issued_verification,
            )

    except IntegrityError as exc:
        _handle_registration_integrity_error(exc)

    return user


async def authenticate_user_record(
    session: AsyncSession,
    payload: LoginRequest,
) -> User:
    """Authenticate credentials and return the active user record."""

    email = str(payload.email).strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    credential = (
        await session.get(UserCredential, user.id)
        if user is not None
        else None
    )
    password_hash = (
        credential.password_hash
        if credential is not None
        else DUMMY_PASSWORD_HASH
    )
    password_valid = verify_password(payload.password, password_hash)

    if user is None or not user.is_active or credential is None or not password_valid:
        raise InvalidCredentialsError("Invalid email or password.")

    return user


async def authenticate_user(
    session: AsyncSession,
    payload: LoginRequest,
) -> str:
    """Authenticate a user and return an access token."""

    user = await authenticate_user_record(session, payload)
    return create_access_token(user.id)
