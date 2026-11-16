import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.core.config import Settings, get_settings
from app.core.passwords import hash_password
from app.core.tokens import hash_token
from app.database.engine import get_session_factory
from app.main import app
from app.models.browser_session import BrowserSession
from app.models.identity import UserCredential
from app.models.user import User
from app.services.browser_sessions import (
    authenticate_browser_session,
    csrf_cookie_name,
    session_cookie_name,
    set_browser_session_cookies,
)


def _create_user(email: str) -> None:
    async def create() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                user = User(email=email, display_name="Browser Session Test")
                session.add(user)
                await session.flush()
                session.add(
                    UserCredential(
                        user_id=user.id,
                        password_hash=hash_password("SecurePassword123!"),
                    )
                )

    asyncio.run(create())


def _delete_user(email: str) -> None:
    async def delete() -> None:
        from sqlalchemy import delete

        async with get_session_factory()() as session:
            await session.execute(delete(User).where(User.email == email))
            await session.commit()

    asyncio.run(delete())


def test_browser_session_login_restore_and_logout() -> None:
    email = f"browser-session-{uuid4().hex}@example.com"
    _create_user(email)
    settings = get_settings()

    try:
        with TestClient(app) as client:
            csrf_response = client.get("/api/v1/auth/csrf")
            assert csrf_response.status_code == 200
            csrf_token = csrf_response.json()["csrf_token"]
            assert client.cookies.get(csrf_cookie_name(settings)) == csrf_token

            login_response = client.post(
                "/api/v1/auth/browser-session/login",
                headers={
                    "Origin": settings.frontend_url,
                    "X-CSRF-Token": csrf_token,
                },
                json={"email": email, "password": "SecurePassword123!"},
            )

            assert login_response.status_code == 200
            assert "access_token" not in login_response.json()
            assert login_response.headers["cache-control"] == "no-store"

            first_session_token = client.cookies.get(session_cookie_name(settings))
            assert first_session_token
            assert "HttpOnly" in login_response.headers.get("set-cookie", "")

            second_login = client.post(
                "/api/v1/auth/browser-session/login",
                headers={
                    "Origin": settings.frontend_url,
                    "X-CSRF-Token": login_response.json()["csrf_token"],
                },
                json={"email": email, "password": "SecurePassword123!"},
            )
            assert second_login.status_code == 200
            assert second_login.json()["csrf_token"] == client.cookies.get(
                csrf_cookie_name(settings)
            )
            second_session_token = client.cookies.get(session_cookie_name(settings))
            assert second_session_token
            assert second_session_token != first_session_token

            async def stored_session_state() -> tuple[str | None, bool]:
                async with get_session_factory()() as session:
                    old_record = await session.scalar(
                        select(BrowserSession).where(
                            BrowserSession.token_hash == hash_token(first_session_token)
                        )
                    )
                    new_record = await session.scalar(
                        select(BrowserSession)
                        .where(BrowserSession.token_hash == hash_token(second_session_token))
                    )
                    return (
                        new_record.token_hash if new_record is not None else None,
                        old_record.revoked_at is not None if old_record is not None else False,
                    )

            token_hash, old_session_revoked = asyncio.run(stored_session_state())
            assert token_hash is not None
            assert token_hash != second_session_token
            assert len(token_hash) == 64
            assert old_session_revoked

            restore_response = client.get("/api/v1/auth/browser-session")
            assert restore_response.status_code == 200
            assert restore_response.json()["email"] == email

            rotated_csrf = client.cookies.get(csrf_cookie_name(settings))
            logout_response = client.post(
                "/api/v1/auth/browser-session/logout",
                headers={
                    "Origin": settings.frontend_url,
                    "X-CSRF-Token": rotated_csrf,
                },
            )
            assert logout_response.status_code == 204
            assert client.get("/api/v1/auth/browser-session").status_code == 401
    finally:
        _delete_user(email)


def test_browser_session_login_rejects_cross_origin_request() -> None:
    with TestClient(app) as client:
        csrf_response = client.get("/api/v1/auth/csrf")
        csrf_token = csrf_response.json()["csrf_token"]
        response = client.post(
            "/api/v1/auth/browser-session/login",
            headers={
                "Origin": "https://attacker.example",
                "X-CSRF-Token": csrf_token,
            },
            json={"email": "not-a-real-user@example.com", "password": "wrong"},
        )

    assert response.status_code == 403


def test_browser_session_login_requires_csrf_header() -> None:
    settings = get_settings()
    with TestClient(app) as client:
        client.get("/api/v1/auth/csrf")
        response = client.post(
            "/api/v1/auth/browser-session/login",
            headers={"Origin": settings.frontend_url},
            json={"email": "not-a-real-user@example.com", "password": "wrong"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "CSRF validation failed."


def test_idle_expired_browser_session_is_revoked() -> None:
    email = f"browser-session-expiry-{uuid4().hex}@example.com"
    _create_user(email)
    settings = get_settings()

    try:
        with TestClient(app) as client:
            csrf_response = client.get("/api/v1/auth/csrf")
            login_response = client.post(
                "/api/v1/auth/browser-session/login",
                headers={
                    "Origin": settings.frontend_url,
                    "X-CSRF-Token": csrf_response.json()["csrf_token"],
                },
                json={"email": email, "password": "SecurePassword123!"},
            )
            assert login_response.status_code == 200
            raw_session_token = client.cookies.get(session_cookie_name(settings))
            assert raw_session_token

            async def expire_session() -> None:
                async with get_session_factory()() as session:
                    await session.execute(
                        update(BrowserSession)
                        .where(
                            BrowserSession.token_hash == hash_token(raw_session_token)
                        )
                        .values(
                            idle_expires_at=datetime.now(UTC) - timedelta(seconds=1)
                        )
                    )
                    await session.commit()

            asyncio.run(expire_session())
            response = client.get("/api/v1/auth/browser-session")
            assert response.status_code == 401

            async def is_revoked() -> bool:
                async with get_session_factory()() as session:
                    record = await session.scalar(
                        select(BrowserSession).where(
                            BrowserSession.token_hash == hash_token(raw_session_token)
                        )
                    )
                    return record is not None and record.revoked_at is not None

            assert asyncio.run(is_revoked())
    finally:
        _delete_user(email)



def test_production_browser_cookies_are_secure_host_only_and_http_only() -> None:
    settings = Settings.model_construct(frontend_url="https://app.example.com")
    response = Response()

    set_browser_session_cookies(
        response,
        "opaque-session-secret",
        "csrf-token",
        settings,
    )

    cookies = response.headers.getlist("set-cookie")
    session_cookie = next(
        cookie for cookie in cookies if cookie.startswith("__Host-aurora_session=")
    )
    csrf_cookie = next(
        cookie for cookie in cookies if cookie.startswith("__Host-aurora_csrf=")
    )

    assert "Secure" in session_cookie
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie
    assert "Path=/" in session_cookie
    assert "Domain=" not in session_cookie

    assert "Secure" in csrf_cookie
    assert "HttpOnly" not in csrf_cookie
    assert "SameSite=lax" in csrf_cookie
    assert "Path=/" in csrf_cookie
    assert "Domain=" not in csrf_cookie


def test_active_browser_session_refreshes_before_short_idle_timeout() -> None:
    email = f"browser-session-short-idle-{uuid4().hex}@example.com"
    _create_user(email)
    settings = Settings(
        _env_file=None,
        browser_session_idle_timeout_minutes=1,
        browser_session_absolute_timeout_hours=1,
    )
    raw_session_token = f"short-idle-session-{uuid4().hex}"
    original_idle_expiry = datetime.now(UTC) + timedelta(seconds=20)

    async def create_recent_session() -> None:
        async with get_session_factory()() as session:
            user = await session.scalar(select(User).where(User.email == email))
            assert user is not None
            session.add(
                BrowserSession(
                    user_id=user.id,
                    token_hash=hash_token(raw_session_token),
                    last_seen_at=datetime.now(UTC) - timedelta(seconds=40),
                    idle_expires_at=original_idle_expiry,
                    absolute_expires_at=datetime.now(UTC) + timedelta(hours=1),
                )
            )
            await session.commit()

    async def authenticate_and_read_expiry() -> tuple[datetime, datetime, datetime]:
        async with get_session_factory()() as session:
            browser_session = await authenticate_browser_session(
                session,
                raw_session_token,
                settings,
            )
            assert browser_session is not None
            return (
                browser_session.last_seen_at,
                browser_session.idle_expires_at,
                browser_session.absolute_expires_at,
            )

    try:
        asyncio.run(create_recent_session())
        last_seen_at, idle_expires_at, absolute_expires_at = asyncio.run(
            authenticate_and_read_expiry()
        )

        assert last_seen_at > datetime.now(UTC) - timedelta(seconds=10)
        assert idle_expires_at > original_idle_expiry
        assert idle_expires_at <= absolute_expires_at
    finally:
        _delete_user(email)
