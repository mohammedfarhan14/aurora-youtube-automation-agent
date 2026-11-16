from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_current_user
from app.core.config import get_settings
from app.database.session import get_db_session
from app.models.user import User
from app.schemas.auth import (
    BrowserSessionLoginResponse,
    EmailVerificationRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services.auth import (
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
    authenticate_user,
    authenticate_user_record,
    register_user_with_email_verification,
)
from app.services.browser_sessions import (
    authenticate_browser_session,
    clear_browser_session_cookies,
    create_browser_session,
    generate_csrf_token,
    get_or_create_csrf_token,
    revoke_browser_session,
    session_cookie_name,
    set_browser_session_cookies,
    validate_browser_csrf,
)
from app.services.email_outbox import enqueue_verification_email
from app.services.email_verification import (
    EmailAlreadyVerifiedError,
    EmailVerificationRateLimitError,
    InvalidEmailVerificationTokenError,
    UserNotFoundError,
    issue_email_verification,
    verify_email,
)
from app.services.rate_limit import (
    limit_login,
    limit_registration,
    limit_verification_resend,
)

router = APIRouter()


@router.get(
    "/csrf",
    status_code=status.HTTP_200_OK,
    summary="Get a browser CSRF token",
)
async def get_csrf_token(
    request: Request,
    response: Response,
) -> dict[str, str]:
    """Bootstrap the host-only double-submit CSRF cookie for browser requests."""

    token = get_or_create_csrf_token(request, response, get_settings())
    response.headers["Cache-Control"] = "no-store"
    return {"csrf_token": token}


@router.post(
    "/browser-session/login",
    response_model=BrowserSessionLoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Create a secure browser session",
)
async def browser_session_login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
) -> BrowserSessionLoginResponse:
    settings = get_settings()
    validate_browser_csrf(request, settings)
    await limit_login(request, payload.email)

    try:
        user = await authenticate_user_record(session, payload)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        ) from exc

    existing_session_token = request.cookies.get(session_cookie_name(settings))
    if existing_session_token:
        existing_session = await authenticate_browser_session(
            session,
            existing_session_token,
            settings,
        )
        if existing_session is not None:
            await revoke_browser_session(session, existing_session)

    session_token = await create_browser_session(session, user.id, settings)
    csrf_token = generate_csrf_token()
    set_browser_session_cookies(response, session_token, csrf_token, settings)
    response.headers["Cache-Control"] = "no-store"
    return BrowserSessionLoginResponse(
        user=UserResponse.model_validate(user),
        csrf_token=csrf_token,
    )


@router.get(
    "/browser-session",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Restore the current browser session",
)
async def get_browser_session(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    if getattr(request.state, "browser_session", None) is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A browser session is required.",
        )
    response.headers["Cache-Control"] = "no-store"
    return UserResponse.model_validate(current_user)


@router.post(
    "/browser-session/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current browser session",
)
async def browser_session_logout(
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> None:
    del current_user
    browser_session = getattr(request.state, "browser_session", None)
    if browser_session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="A browser session is required.",
        )

    await revoke_browser_session(session, browser_session)
    clear_browser_session_cookies(response, get_settings())
    response.headers["Cache-Control"] = "no-store"


@router.post(
    "/register",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user",
)
async def register(
    payload: RegisterRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> UserResponse:
    await limit_registration(request)
    try:
        user = await register_user_with_email_verification(
            session,
            payload,
        )

    except EmailVerificationRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification email requests. Please try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    except EmailAlreadyRegisteredError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is currently unavailable.",
        ) from exc

    return UserResponse.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    status_code=status.HTTP_200_OK,
    summary="Authenticate a user",
)
async def login(
    payload: LoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> TokenResponse:
    await limit_login(request, payload.email)
    try:
        access_token = await authenticate_user(session, payload)
    except InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    return TokenResponse(access_token=access_token)


@router.post(
    "/email-verification/resend",
    status_code=status.HTTP_200_OK,
    summary="Resend an email verification link",
)
async def resend_verification_email(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    await limit_verification_resend(request)
    user_id = current_user.id
    await session.rollback()

    try:
        async with session.begin():
            issued_verification = await issue_email_verification(
                session,
                user_id,
            )
            user = await session.get(User, user_id)

            if user is None:
                raise UserNotFoundError("User not found.")

            await enqueue_verification_email(
                session,
                user,
                issued_verification,
            )

    except EmailVerificationRateLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many verification email requests. Please try again later.",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc

    except EmailAlreadyVerifiedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email address is already verified.",
        ) from exc

    except UserNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        ) from exc

    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Email delivery is currently unavailable.",
        ) from exc

    return {"message": "A new verification email has been queued."}


@router.get(
    "/me",
    response_model=UserResponse,
    status_code=status.HTTP_200_OK,
    summary="Get the authenticated user's profile",
)
async def get_me(
    current_user: User = Depends(get_current_user),
) -> UserResponse:
    """Return the profile of the currently authenticated user."""

    return UserResponse.model_validate(current_user)


@router.post(
    "/email-verification/verify",
    status_code=status.HTTP_200_OK,
    summary="Verify a user's email address",
)
async def verify_email_address(
    payload: EmailVerificationRequest,
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, str]:
    try:
        async with session.begin():
            user = await verify_email(session, payload.token)
    except InvalidEmailVerificationTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired verification token.",
        ) from exc

    return {
        "message": "Email address verified successfully.",
        "email": user.email,
    }
