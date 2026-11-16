from unittest.mock import patch
from uuid import uuid4

import pytest

from app.core.passwords import DUMMY_PASSWORD_HASH
from app.core.security import decode_access_token
from app.database.engine import get_session_factory
from app.schemas.auth import LoginRequest, RegisterRequest
from app.services.auth import (
    InvalidCredentialsError,
    authenticate_user,
    register_user,
)


@pytest.mark.anyio
async def test_authenticate_user_returns_access_token():
    email = f"login-{uuid4().hex}@example.com"
    password = "SecurePassword123!"

    async with get_session_factory()() as session:
        user = await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Login Test",
                password=password,
            ),
        )

        try:
            token = await authenticate_user(
                session,
                LoginRequest(email=email, password=password),
            )

            payload = decode_access_token(token)

            assert payload["sub"] == str(user.id)
            assert payload["type"] == "access"

        finally:
            await session.delete(user)
            await session.commit()


@pytest.mark.anyio
async def test_authenticate_user_rejects_incorrect_password():
    email = f"wrong-password-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        user = await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Login Test",
                password="SecurePassword123!",
            ),
        )

        try:
            with pytest.raises(InvalidCredentialsError):
                await authenticate_user(
                    session,
                    LoginRequest(
                        email=email,
                        password="IncorrectPassword123!",
                    ),
                )

        finally:
            await session.delete(user)
            await session.commit()


@pytest.mark.anyio
async def test_authenticate_user_rejects_unknown_email():
    email = f"unknown-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        with pytest.raises(InvalidCredentialsError):
            await authenticate_user(
                session,
                LoginRequest(
                    email=email,
                    password="SecurePassword123!",
                ),
            )


@pytest.mark.anyio
async def test_authenticate_user_rejects_inactive_account():
    email = f"inactive-{uuid4().hex}@example.com"

    async with get_session_factory()() as session:
        user = await register_user(
            session,
            RegisterRequest(
                email=email,
                display_name="Inactive Test",
                password="SecurePassword123!",
            ),
        )

        try:
            user.is_active = False
            await session.commit()

            with pytest.raises(InvalidCredentialsError):
                await authenticate_user(
                    session,
                    LoginRequest(
                        email=email,
                        password="SecurePassword123!",
                    ),
                )

        finally:
            await session.delete(user)
            await session.commit()


@pytest.mark.anyio
async def test_unknown_email_performs_dummy_password_verification():
    email = f"unknown-{uuid4().hex}@example.com"

    with patch(
        "app.services.auth.verify_password",
        return_value=False,
    ) as mocked_verify:
        async with get_session_factory()() as session:
            with pytest.raises(InvalidCredentialsError):
                await authenticate_user(
                    session,
                    LoginRequest(
                        email=email,
                        password="SomePassword123!",
                    ),
                )

    mocked_verify.assert_called_once_with(
        "SomePassword123!",
        DUMMY_PASSWORD_HASH,
    )
