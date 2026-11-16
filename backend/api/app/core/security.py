from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt

from app.core.config import get_settings


def create_access_token(user_id: UUID) -> str:
    """Create a signed JWT access token for a user."""
    settings = get_settings()

    if settings.jwt_secret_key is None:
        raise RuntimeError("JWT secret key is not configured.")

    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=settings.access_token_expire_minutes)

    payload = {
        "sub": str(user_id),
        "iat": now,
        "exp": expires_at,
        "type": "access",
    }

    return jwt.encode(
        payload,
        settings.jwt_secret_key.get_secret_value(),
        algorithm=settings.jwt_algorithm,
    )


def decode_access_token(token: str) -> dict:
    """Verify and decode a JWT access token."""
    settings = get_settings()

    if settings.jwt_secret_key is None:
        raise RuntimeError("JWT secret key is not configured.")

    payload = jwt.decode(
        token,
        settings.jwt_secret_key.get_secret_value(),
        algorithms=[settings.jwt_algorithm],
        options={"require": ["sub", "iat", "exp", "type"]},
    )

    if payload.get("type") != "access":
        raise jwt.InvalidTokenError("Invalid token type.")

    return payload
