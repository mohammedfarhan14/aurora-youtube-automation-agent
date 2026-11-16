from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest

from app.core.config import get_settings
from app.core.security import create_access_token, decode_access_token


def test_create_access_token():
    user_id = uuid4()

    token = create_access_token(user_id)

    assert isinstance(token, str)

    payload = decode_access_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["type"] == "access"
    assert "iat" in payload
    assert "exp" in payload


def test_decode_invalid_token():
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token("invalid.token.value")


def test_token_contains_expected_expiration():
    user_id = uuid4()

    token = create_access_token(user_id)
    payload = decode_access_token(token)

    settings = get_settings()

    assert payload["exp"] - payload["iat"] == (settings.access_token_expire_minutes * 60)


def test_expired_token_is_rejected():
    settings = get_settings()
    now = datetime.now(UTC)

    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iat": now - timedelta(hours=2),
            "exp": now - timedelta(hours=1),
            "type": "access",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_wrong_signing_key_is_rejected():
    settings = get_settings()

    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iat": datetime.now(UTC),
            "exp": datetime.now(UTC) + timedelta(minutes=30),
            "type": "access",
        },
        "this-is-the-wrong-signing-key-32-bytes-minimum",
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token)


def test_non_access_token_is_rejected():
    settings = get_settings()
    now = datetime.now(UTC)

    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=30),
            "type": "refresh",
        },
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(jwt.InvalidTokenError, match="Invalid token type"):
        decode_access_token(token)
