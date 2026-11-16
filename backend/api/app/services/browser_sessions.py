import hmac
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tokens import hash_token
from app.models.browser_session import BrowserSession


def _secure_cookie(settings: Settings) -> bool:
    return urlsplit(settings.frontend_url).scheme == "https"


def session_cookie_name(settings: Settings) -> str:
    prefix = "__Host-" if _secure_cookie(settings) else ""
    return f"{prefix}aurora_session"


def csrf_cookie_name(settings: Settings) -> str:
    prefix = "__Host-" if _secure_cookie(settings) else ""
    return f"{prefix}aurora_csrf"


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=csrf_cookie_name(settings),
        value=token,
        max_age=settings.browser_session_absolute_timeout_hours * 3600,
        secure=_secure_cookie(settings),
        httponly=False,
        samesite="lax",
        path="/",
    )


def set_browser_session_cookies(
    response: Response,
    session_token: str,
    csrf_token: str,
    settings: Settings,
) -> None:
    response.set_cookie(
        key=session_cookie_name(settings),
        value=session_token,
        max_age=settings.browser_session_absolute_timeout_hours * 3600,
        secure=_secure_cookie(settings),
        httponly=True,
        samesite="lax",
        path="/",
    )
    set_csrf_cookie(response, csrf_token, settings)


def clear_browser_session_cookies(response: Response, settings: Settings) -> None:
    secure = _secure_cookie(settings)
    response.delete_cookie(
        key=session_cookie_name(settings),
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        key=csrf_cookie_name(settings),
        path="/",
        secure=secure,
        httponly=False,
        samesite="lax",
    )


def validate_browser_csrf(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    expected = urlsplit(settings.frontend_url)
    received = urlsplit(origin or "")

    if (
        not origin
        or received.scheme != expected.scheme
        or received.netloc != expected.netloc
        or received.path
        or received.query
        or received.fragment
        or received.username is not None
        or received.password is not None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Request origin is not allowed.",
        )

    cookie_token = request.cookies.get(csrf_cookie_name(settings))
    header_token = request.headers.get("x-csrf-token")
    if (
        not cookie_token
        or not header_token
        or len(cookie_token) > 256
        or not hmac.compare_digest(cookie_token, header_token)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="CSRF validation failed.",
        )


def get_or_create_csrf_token(
    request: Request,
    response: Response,
    settings: Settings,
) -> str:
    token = request.cookies.get(csrf_cookie_name(settings))
    if token and len(token) <= 256:
        return token

    token = generate_csrf_token()
    set_csrf_cookie(response, token, settings)
    return token


async def create_browser_session(
    session: AsyncSession,
    user_id,
    settings: Settings,
) -> str:
    now = datetime.now(UTC)
    session_token = secrets.token_urlsafe(32)
    absolute_expiry = now + timedelta(
        hours=settings.browser_session_absolute_timeout_hours
    )
    idle_expiry = min(
        now + timedelta(minutes=settings.browser_session_idle_timeout_minutes),
        absolute_expiry,
    )
    session.add(
        BrowserSession(
            user_id=user_id,
            token_hash=hash_token(session_token),
            last_seen_at=now,
            idle_expires_at=idle_expiry,
            absolute_expires_at=absolute_expiry,
        )
    )
    await session.commit()
    return session_token


async def authenticate_browser_session(
    session: AsyncSession,
    session_token: str,
    settings: Settings,
) -> BrowserSession | None:
    browser_session = await session.scalar(
        select(BrowserSession).where(
            BrowserSession.token_hash == hash_token(session_token)
        )
    )
    if browser_session is None or browser_session.revoked_at is not None:
        return None

    now = datetime.now(UTC)
    if (
        now >= browser_session.idle_expires_at
        or now >= browser_session.absolute_expires_at
    ):
        browser_session.revoked_at = now
        await session.commit()
        return None

    # Refresh at most every five minutes, but never let the write throttle
    # consume most or all of a shorter configured idle-timeout window.
    refresh_interval = min(
        timedelta(minutes=5),
        timedelta(minutes=settings.browser_session_idle_timeout_minutes) / 2,
    )
    if now - browser_session.last_seen_at >= refresh_interval:
        browser_session.last_seen_at = now
        browser_session.idle_expires_at = min(
            now + timedelta(minutes=settings.browser_session_idle_timeout_minutes),
            browser_session.absolute_expires_at,
        )
        await session.commit()

    return browser_session


async def revoke_browser_session(
    session: AsyncSession,
    browser_session: BrowserSession,
) -> None:
    if browser_session.revoked_at is None:
        browser_session.revoked_at = datetime.now(UTC)
        await session.commit()
