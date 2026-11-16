from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from app.core.passwords import verify_password
from app.database.engine import get_session_factory
from app.models.identity import UserCredential
from app.models.user import User
from app.schemas.auth import RegisterRequest
from app.services.auth import (
    EmailAlreadyRegisteredError,
    register_user,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_register_user_creates_user_and_credential():
    email = f"registration-{uuid4().hex}@example.com"
    password = "Aurora-Secure-Password-2026"

    payload = RegisterRequest(
        email=email.upper(),
        display_name="Farhan",
        password=password,
    )

    factory = get_session_factory()

    try:
        async with factory() as session:
            user = await register_user(session, payload)

            assert user.email == email
            assert user.display_name == "Farhan"
            assert user.is_active is True

            result = await session.execute(
                select(UserCredential).where(UserCredential.user_id == user.id)
            )
            credential = result.scalar_one()

            assert credential.password_hash != password
            assert credential.password_hash.startswith("$argon2id$")
            assert verify_password(password, credential.password_hash)

    finally:
        async with factory() as cleanup_session:
            async with cleanup_session.begin():
                await cleanup_session.execute(delete(User).where(User.email == email))


@pytest.mark.anyio
async def test_duplicate_email_registration_is_rejected():
    email = f"duplicate-{uuid4().hex}@example.com"

    factory = get_session_factory()

    try:
        async with factory() as session:
            first_user = await register_user(
                session,
                RegisterRequest(
                    email=email,
                    display_name="First User",
                    password="Aurora-Secure-Password-2026",
                ),
            )

            assert first_user.email == email

        async with factory() as session:
            with pytest.raises(EmailAlreadyRegisteredError):
                await register_user(
                    session,
                    RegisterRequest(
                        email=email.upper(),
                        display_name="Second User",
                        password="Another-Secure-Password-2026",
                    ),
                )

        async with factory() as session:
            result = await session.execute(select(User).where(User.email == email))
            users = result.scalars().all()

            assert len(users) == 1
            assert users[0].display_name == "First User"

    finally:
        async with factory() as cleanup_session:
            async with cleanup_session.begin():
                await cleanup_session.execute(delete(User).where(User.email == email))
