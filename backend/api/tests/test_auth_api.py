from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.core.tokens import generate_secure_token, hash_token
from app.database.engine import get_session_factory
from app.main import app
from app.models.email_outbox import EmailOutbox
from app.models.email_verification import EmailVerificationToken
from app.models.user import User
from app.services.email_outbox_crypto import decrypt_payload

client = TestClient(app)

def get_verification_email(
    email: str,
    *,
    exclude_id: UUID | None = None,
) -> tuple[EmailOutbox, str]:
    """Retrieve and decrypt a user's latest queued verification email."""

    async def fetch_email():
        async with get_session_factory()() as session:
            query = select(EmailOutbox).where(
                EmailOutbox.recipient == email,
                EmailOutbox.message_type == "email_verification",
            )

            if exclude_id is not None:
                query = query.where(EmailOutbox.id != exclude_id)

            outbox_message = await session.scalar(
                query.order_by(EmailOutbox.created_at.desc())
            )

            assert outbox_message is not None

            settings = get_settings()
            assert settings.email_outbox_encryption_key is not None

            payload = decrypt_payload(
                outbox_message.encrypted_payload,
                {
                    outbox_message.key_version:
                        settings.email_outbox_encryption_key.get_secret_value()
                },
            )

            return outbox_message, payload

    import asyncio

    return asyncio.run(fetch_email())

def age_verification_token(email: str, minutes: int = 6) -> None:
    """Move a user's latest verification token into the past for testing."""

    async def update_token():
        async with get_session_factory()() as session:
            async with session.begin():
                user = await session.scalar(
                    select(User).where(User.email == email)
                )
                assert user is not None

                token = await session.scalar(
                    select(EmailVerificationToken)
                    .where(EmailVerificationToken.user_id == user.id)
                    .order_by(EmailVerificationToken.created_at.desc())
                )
                assert token is not None

                token.created_at = datetime.now(UTC) - timedelta(minutes=minutes)

    import asyncio

    asyncio.run(update_token())

@pytest.fixture
def unique_email() -> str:
    return f"test-{uuid4().hex}@example.com"


@pytest.fixture
def cleanup_user():
    created_emails: list[str] = []

    yield created_emails

    async def delete_users():
        from sqlalchemy import delete

        from app.database.engine import get_session_factory

        async with get_session_factory()() as session:
            await session.execute(
                delete(EmailOutbox).where(
                    EmailOutbox.recipient.in_(created_emails)
                )
            )
            await session.execute(delete(User).where(User.email.in_(created_emails)))
            await session.commit()

    import asyncio

    asyncio.run(delete_users())


def test_register_user_returns_201(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Test User",
            "password": "SecurePassword123!",
        },
    )

    assert response.status_code == 201

    data = response.json()

    assert data["email"] == unique_email
    assert data["display_name"] == "Test User"
    assert data["is_active"] is True
    assert data["email_verified"] is False
    assert "password" not in data
    assert "password_hash" not in data
    assert "token" not in data

    outbox_message, email_body = get_verification_email(unique_email)

    assert outbox_message.status == "pending"
    assert outbox_message.recipient == unique_email
    assert outbox_message.subject == "Verify your Aurora Studio email address"
    assert outbox_message.message_type == "email_verification"

    assert "Please verify your Aurora Studio email address" in email_body
    assert "Hello Test User" in email_body
    assert "http://localhost:5173/verify-email?token=" in email_body

    raw_token = extract_verification_token(email_body)

    async def verify_stored_token():
        async with get_session_factory()() as session:
            token_record = await session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.token_hash == hash_token(raw_token)
                )
            )

            assert token_record is not None
            assert str(token_record.user_id) == data["id"]

    import asyncio

    asyncio.run(verify_stored_token())


def test_register_duplicate_email_returns_409(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    payload = {
        "email": unique_email,
        "display_name": "Test User",
        "password": "SecurePassword123!",
    }

    first_response = client.post("/api/v1/auth/register", json=payload)
    second_response = client.post("/api/v1/auth/register", json=payload)

    assert first_response.status_code == 201
    assert second_response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {
            "email": "not-an-email",
            "display_name": "Test User",
            "password": "SecurePassword123!",
        },
        {
            "email": "valid@example.com",
            "display_name": "Test User",
            "password": "short",
        },
        {
            "email": "valid@example.com",
            "display_name": "   ",
            "password": "SecurePassword123!",
        },
    ],
)
def test_invalid_registration_returns_422(payload):
    response = client.post("/api/v1/auth/register", json=payload)

    assert response.status_code == 422


def test_login_returns_access_token(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Login API Test",
            "password": password,
        },
    )
    assert registration.status_code == 201

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": password,
        },
    )

    assert response.status_code == 200

    data = response.json()

    assert data["token_type"] == "bearer"
    assert isinstance(data["access_token"], str)

    payload = decode_access_token(data["access_token"])
    assert payload["sub"] == registration.json()["id"]


def test_login_rejects_incorrect_password(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Login API Test",
            "password": "SecurePassword123!",
        },
    )

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": "WrongPassword123!",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."
    assert response.headers["www-authenticate"] == "Bearer"


def test_login_rejects_unknown_email():
    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": f"unknown-{uuid4().hex}@example.com",
            "password": "SecurePassword123!",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


def test_login_rejects_inactive_account(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Inactive API Test",
            "password": "SecurePassword123!",
        },
    )

    async def deactivate_user():
        async with get_session_factory()() as session:
            user = await session.scalar(select(User).where(User.email == unique_email))

            assert user is not None

            user.is_active = False
            await session.commit()

    import asyncio

    asyncio.run(deactivate_user())

    response = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": "SecurePassword123!",
        },
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password."


def test_me_rejects_missing_token():
    response = client.get("/api/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_rejects_malformed_token():
    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_rejects_expired_token():
    settings = get_settings()
    now = datetime.now(UTC)

    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iat": now - timedelta(minutes=10),
            "exp": now - timedelta(minutes=5),
            "type": "access",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."


def test_me_returns_authenticated_user_profile(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    password = "SecurePassword123!"
    display_name = "Authenticated API Test"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": display_name,
            "password": password,
        },
    )

    assert registration.status_code == 201

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": password,
        },
    )

    assert login.status_code == 200

    token = login.json()["access_token"]

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200

    data = response.json()

    assert data["id"] == registration.json()["id"]
    assert data["email"] == unique_email
    assert data["display_name"] == display_name
    assert data["is_active"] is True
    assert data["email_verified"] is False

    assert "password" not in data
    assert "password_hash" not in data


def test_me_rejects_inactive_user_with_valid_token(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Inactive API Test",
            "password": password,
        },
    )

    assert registration.status_code == 201

    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": password,
        },
    )

    assert login.status_code == 200

    token = login.json()["access_token"]

    import asyncio

    async def deactivate_user():
        async with get_session_factory()() as session:
            user = await session.scalar(select(User).where(User.email == unique_email))

            assert user is not None

            user.is_active = False
            await session.commit()

    asyncio.run(deactivate_user())

    response = client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."



def test_register_persists_user_token_and_outbox_atomically(
    unique_email,
    cleanup_user,
):
    cleanup_user.append(unique_email)

    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Outbox Transaction Test",
            "password": "SecurePassword123!",
        },
    )

    assert response.status_code == 201

    async def check_persisted_registration():
        async with get_session_factory()() as session:
            user = await session.scalar(
                select(User).where(User.email == unique_email)
            )
            assert user is not None

            verification_token = await session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.user_id == user.id
                )
            )
            assert verification_token is not None
            assert verification_token.consumed_at is None

            outbox_message = await session.scalar(
                select(EmailOutbox).where(
                    EmailOutbox.user_id == user.id,
                    EmailOutbox.message_type == "email_verification",
                )
            )
            assert outbox_message is not None
            assert outbox_message.status == "pending"
            assert outbox_message.encrypted_payload

    import asyncio

    asyncio.run(check_persisted_registration())


def extract_verification_token(email_body: str) -> str:
    verification_url = next(
        line.strip()
        for line in email_body.splitlines()
        if "http://localhost:5173/verify-email?token=" in line
    )

    return parse_qs(urlparse(verification_url).query)["token"][0]


def test_verify_email_successfully(unique_email, cleanup_user):
    cleanup_user.append(unique_email)

    registration_response = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Verification Test",
            "password": "SecurePassword123!",
        },
    )

    assert registration_response.status_code == 201

    _, email_body = get_verification_email(unique_email)
    raw_token = extract_verification_token(email_body)

    response = client.post(
        "/api/v1/auth/email-verification/verify",
        json={"token": raw_token},
    )

    assert response.status_code == 200
    assert response.json()["email"] == unique_email
    assert response.json()["message"] == "Email address verified successfully."

    import asyncio

    async def check_verification_state():
        async with get_session_factory()() as session:
            user = await session.scalar(
                select(User).where(User.email == unique_email)
            )
            assert user is not None
            assert user.email_verified_at is not None

            token_record = await session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.token_hash == hash_token(raw_token)
                )
            )
            assert token_record is not None
            assert token_record.consumed_at is not None

    asyncio.run(check_verification_state())


    login = client.post(
        "/api/v1/auth/login",
        json={
            "email": unique_email,
            "password": "SecurePassword123!",
        },
    )

    assert login.status_code == 200

    profile = client.get(
        "/api/v1/auth/me",
        headers={
            "Authorization": f"Bearer {login.json()['access_token']}"
        },
    )

    assert profile.status_code == 200
    assert profile.json()["email_verified"] is True


def test_verify_email_rejects_invalid_token():
    response = client.post(
        "/api/v1/auth/email-verification/verify",
        json={"token": generate_secure_token()},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid or expired verification token."



def test_resend_verification_email_invalidates_old_token(
    unique_email,
    cleanup_user,
):
    cleanup_user.append(unique_email)
    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Resend Verification Test",
            "password": password,
        },
    )

    assert registration.status_code == 201

    initial_outbox, initial_email_body = get_verification_email(unique_email)
    old_token = extract_verification_token(initial_email_body)

    login = client.post(
        "/api/v1/auth/login",
        json={"email": unique_email, "password": password},
    )

    assert login.status_code == 200

    age_verification_token(unique_email)

    access_token = login.json()["access_token"]

    headers = {"Authorization": f"Bearer {access_token}"}

    resend = client.post(
        "/api/v1/auth/email-verification/resend",
        headers=headers,
    )

    assert resend.status_code == 200
    assert resend.json()["message"] == "A new verification email has been queued."

    _, new_email_body = get_verification_email(
        unique_email,
        exclude_id=initial_outbox.id,
    )
    new_token = extract_verification_token(new_email_body)

    assert new_token != old_token

    old_token_response = client.post(
        "/api/v1/auth/email-verification/verify",
        json={"token": old_token},
    )

    assert old_token_response.status_code == 400

    new_token_response = client.post(
        "/api/v1/auth/email-verification/verify",
        json={"token": new_token},
    )

    assert new_token_response.status_code == 200



def test_resend_verification_email_enforces_cooldown(
    unique_email,
    cleanup_user,
):
    cleanup_user.append(unique_email)
    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Resend Rate Limit Test",
            "password": password,
        },
    )

    assert registration.status_code == 201


    login = client.post(
        "/api/v1/auth/login",
        json={"email": unique_email, "password": password},
    )

    assert login.status_code == 200

    headers = {
        "Authorization": f"Bearer {login.json()['access_token']}"
    }

    response = client.post(
        "/api/v1/auth/email-verification/resend",
        headers=headers,
    )

    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert int(response.headers["Retry-After"]) > 0
    assert response.json()["detail"] == (
        "Too many verification email requests. Please try again later."
    )



def test_resend_verification_email_requires_authentication():
    response = client.post("/api/v1/auth/email-verification/resend")

    assert response.status_code == 401
    assert response.json()["detail"] == "Could not validate credentials."


def test_resend_verification_email_rejects_already_verified_user(
    unique_email,
    cleanup_user,
):
    cleanup_user.append(unique_email)
    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Already Verified Test",
            "password": password,
        },
    )

    assert registration.status_code == 201

    _, email_body = get_verification_email(unique_email)
    raw_token = extract_verification_token(email_body)

    verification = client.post(
        "/api/v1/auth/email-verification/verify",
        json={"token": raw_token},
    )

    assert verification.status_code == 200

    login = client.post(
        "/api/v1/auth/login",
        json={"email": unique_email, "password": password},
    )

    assert login.status_code == 200

    response = client.post(
        "/api/v1/auth/email-verification/resend",
        headers={
            "Authorization": f"Bearer {login.json()['access_token']}"
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Email address is already verified."



def test_resend_verification_email_queues_new_message(
    unique_email,
    cleanup_user,
):
    cleanup_user.append(unique_email)
    password = "SecurePassword123!"

    registration = client.post(
        "/api/v1/auth/register",
        json={
            "email": unique_email,
            "display_name": "Resend Outbox Test",
            "password": password,
        },
    )

    assert registration.status_code == 201

    login = client.post(
        "/api/v1/auth/login",
        json={"email": unique_email, "password": password},
    )

    assert login.status_code == 200

    age_verification_token(unique_email)

    response = client.post(
        "/api/v1/auth/email-verification/resend",
        headers={
            "Authorization": f"Bearer {login.json()['access_token']}"
        },
    )

    assert response.status_code == 200
    assert response.json()["message"] == (
        "A new verification email has been queued."
    )

    outbox_message, email_body = get_verification_email(unique_email)

    assert outbox_message.status == "pending"

    raw_token = extract_verification_token(email_body)

    import asyncio

    async def check_persisted_token():
        async with get_session_factory()() as session:
            token_record = await session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.token_hash == hash_token(raw_token)
                )
            )

            assert token_record is not None
            assert token_record.consumed_at is None

    asyncio.run(check_persisted_token())