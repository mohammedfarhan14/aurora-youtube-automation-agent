from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import decode_access_token
from app.database.session import get_db_session
from app.models.membership import OrganizationMember
from app.models.organization import Organization
from app.models.user import User
from app.services.browser_sessions import (
    authenticate_browser_session,
    revoke_browser_session,
    session_cookie_name,
    validate_browser_csrf,
)

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    """Authenticate either a bearer client or a first-party browser session."""

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    request.state.browser_session = None

    if credentials is not None:
        try:
            payload = decode_access_token(credentials.credentials)
            user_id = UUID(payload["sub"])
        except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
            raise unauthorized from None
    else:
        settings = get_settings()
        session_token = request.cookies.get(session_cookie_name(settings))
        if not session_token:
            raise unauthorized

        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            validate_browser_csrf(request, settings)

        browser_session = await authenticate_browser_session(
            session,
            session_token,
            settings,
        )
        if browser_session is None:
            raise unauthorized

        request.state.browser_session = browser_session
        user_id = browser_session.user_id

    user = await session.get(User, user_id)
    if user is None or not user.is_active:
        browser_session = getattr(request.state, "browser_session", None)
        if browser_session is not None:
            await revoke_browser_session(session, browser_session)
        raise unauthorized

    return user


async def get_verified_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """Require an active user with a verified email address."""

    if current_user.email_verified_at is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification is required to access this resource.",
        )

    return current_user


def require_organization_roles(*allowed_roles: str):
    """Require active organization membership with one of the specified roles."""

    valid_roles = {"owner", "admin", "editor", "viewer"}

    if not allowed_roles:
        raise ValueError("At least one organization role must be specified.")

    invalid_roles = set(allowed_roles) - valid_roles
    if invalid_roles:
        raise ValueError(f"Invalid organization roles: {sorted(invalid_roles)}")

    async def dependency(
        organization_id: UUID,
        session: AsyncSession = Depends(get_db_session),
        current_user: User = Depends(get_verified_user),
    ) -> Organization:
        result = await session.execute(
            select(OrganizationMember, Organization)
            .join(
                Organization,
                Organization.id == OrganizationMember.organization_id,
            )
            .where(
                Organization.id == organization_id,
                OrganizationMember.user_id == current_user.id,
                OrganizationMember.status == "active",
                Organization.status == "active",
            )
        )

        membership_data = result.one_or_none()

        if membership_data is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found.",
            )

        membership, organization = membership_data

        if membership.role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient organization permissions.",
            )

        return organization

    return dependency
