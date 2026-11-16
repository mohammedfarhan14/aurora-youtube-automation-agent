import logging
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import get_verified_user, require_organization_roles
from app.core.config import get_settings
from app.database.session import get_db_session
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.user import User
from app.models.youtube import YouTubeChannel, YouTubeOAuthAttempt
from app.models.youtube_publishing import (
    YouTubePublishEvent,
    YouTubePublishingConsentEvent,
    YouTubePublishJob,
)
from app.schemas.youtube import (
    YouTubeAuthorizationResponse,
    YouTubeChannelResponse,
    YouTubeChannelSelectionRequest,
    YouTubePendingSelectionResponse,
    YouTubePublishEventResponse,
    YouTubePublishingConsentEventResponse,
    YouTubePublishingSettingsRequest,
    YouTubePublishingSettingsResponse,
    YouTubePublishJobCreateRequest,
    YouTubePublishJobResponse,
)
from app.schemas.youtube_channel_settings import (
    YouTubeChannelProfileRequest,
    YouTubeChannelProfileResponse,
    YouTubeChannelScheduleRequest,
    YouTubeChannelScheduleResponse,
)
from app.services.youtube_channel_settings import (
    YouTubeChannelNotFoundError as ChannelSettingsNotFoundError,
)
from app.services.youtube_channel_settings import (
    YouTubeChannelSettingsError,
    create_channel_profile_version,
    get_channel_schedule,
    get_latest_channel_profile,
    save_channel_schedule,
)
from app.services.youtube_oauth import (
    YOUTUBE_CONNECT_SCOPES,
    YOUTUBE_PUBLISH_SCOPES,
    YouTubeChannelNotFoundError,
    YouTubeOAuthError,
    _consume_state,
    channel_can_publish,
    complete_authorization,
    create_authorization_url,
    disconnect_channel,
    get_pending_channel_selection,
    list_channels,
    select_pending_channel,
)
from app.services.youtube_publishing import (
    YouTubePublishingConflictError,
    YouTubePublishingNotReadyError,
    create_youtube_publish_job,
)

router = APIRouter()
callback_router = APIRouter()
logger = logging.getLogger(__name__)


async def _cancel_automatic_publish_jobs(
    session: AsyncSession,
    *,
    organization_id: UUID,
    youtube_channel_id: UUID,
    actor_user_id: UUID,
    reason: str,
) -> int:
    """Cancel uncompleted consent-based jobs and return their runs to human review."""
    jobs = list(
        (
            await session.scalars(
                select(YouTubePublishJob)
                .where(
                    YouTubePublishJob.organization_id == organization_id,
                    YouTubePublishJob.youtube_channel_id == youtube_channel_id,
                    YouTubePublishJob.approval_mode == "automatic_consent",
                    YouTubePublishJob.status.in_(
                        {
                            "queued",
                            "initiating",
                            "uploading",
                            "processing",
                            "uploaded",
                            "failed",
                            "reconciliation_required",
                        }
                    ),
                )
                .order_by(YouTubePublishJob.created_at, YouTubePublishJob.id)
                .with_for_update()
            )
        ).all()
    )
    now = await session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise RuntimeError("Could not obtain the database clock.")
    for job in jobs:
        has_external_progress = (
            job.bytes_uploaded > 0
            or job.youtube_video_id is not None
            or job.upload_session_url_encrypted is not None
        )
        # A claimed worker may have an external request in flight before its
        # response is persisted. Preserve that uncertainty for reconciliation.
        requires_reconciliation = (
            job.status == "reconciliation_required"
            or has_external_progress
            or job.lease_token is not None
        )
        job.status = "reconciliation_required" if requires_reconciliation else "cancelled"
        job.failure_code = (
            f"{reason}_reconciliation_required" if requires_reconciliation else reason
        )
        job.failure_message = (
            "Automatic publishing was stopped because the channel's owner consent "
            "or publishing policy changed. Provider-side progress must be reconciled "
            "before another upload can be created."
            if requires_reconciliation
            else (
                "Automatic publishing was stopped before a provider upload began "
                "because the channel's owner consent or publishing policy changed."
            )
        )
        run = await session.scalar(
            select(ProductionRun)
            .where(
                ProductionRun.id == job.production_run_id,
                ProductionRun.organization_id == organization_id,
            )
            .with_for_update()
        )
        if (
            run is not None
            and run.status == "approved"
            and run.approval_mode == "automatic_consent"
            and run.approval_consent_event_id == job.approval_consent_event_id
        ):
            run.status = "awaiting_review"
            run.approval_mode = "none"
            run.approved_by_user_id = None
            run.approved_at = None
            run.approval_consent_event_id = None
        session.add(
            YouTubePublishEvent(
                organization_id=organization_id,
                publish_job_id=job.id,
                actor_user_id=actor_user_id,
                event_type="automatic_publishing_stopped",
                event_metadata={"reason": reason, "stopped_at": now.isoformat()},
            )
        )
    return len(jobs)


@router.post(
    "/organizations/{organization_id}/youtube/authorize",
    response_model=YouTubeAuthorizationResponse,
    summary="Start YouTube channel authorization",
)
async def authorize_youtube(
    organization_id: UUID,
    purpose: Literal["connect", "publish"] = Query(default="connect"),
    channel_record_id: UUID | None = Query(default=None),
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeAuthorizationResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if purpose == "publish" and channel_record_id is None:
        raise HTTPException(
            status_code=422,
            detail="channel_record_id is required when enabling publishing.",
        )
    if purpose == "connect" and channel_record_id is not None:
        raise HTTPException(
            status_code=422,
            detail="channel_record_id is only valid when enabling publishing.",
        )
    try:
        url = await create_authorization_url(
            session,
            get_settings(),
            organization_id,
            current_user.id,
            purpose,
            target_channel_id=channel_record_id,
        )
    except YouTubeChannelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except YouTubeOAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    requested_scopes = (
        YOUTUBE_CONNECT_SCOPES if purpose == "connect" else YOUTUBE_PUBLISH_SCOPES
    )
    return YouTubeAuthorizationResponse(
        authorization_url=url,
        purpose=purpose,
        requested_scopes=list(requested_scopes),
    )


@callback_router.get("/callback", include_in_schema=False)
async def youtube_callback(
    state: str = Query(min_length=20, max_length=256),
    code: str | None = Query(default=None, max_length=4096),
    error: str | None = Query(default=None, max_length=128),
    session: AsyncSession = Depends(get_db_session),
):
    settings = get_settings()
    result = "error"
    try:
        if error:
            await _consume_state(session, state)
            raise YouTubeOAuthError("Google authorization was not granted.")
        if not code:
            raise YouTubeOAuthError("Google did not return an authorization code.")
        completed = await complete_authorization(session, settings, state, code)
        if isinstance(completed, YouTubeOAuthAttempt):
            result = "select"
            selection_query = {
                "attempt_id": str(completed.id),
                "organization_id": str(completed.organization_id),
            }
        else:
            result = "connected"
            selection_query = {}
    except (YouTubeOAuthError, httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, YouTubeOAuthError):
            logger.warning("YouTube OAuth callback failed: %s", exc)
        elif isinstance(exc, httpx.HTTPStatusError):
            logger.warning(
                "YouTube OAuth callback provider returned HTTP status %s.",
                exc.response.status_code,
            )
        else:
            logger.warning(
                "YouTube OAuth callback failed with %s.", type(exc).__name__
            )
        await session.rollback()

    separator = "&" if "?" in settings.youtube_frontend_result_url else "?"
    redirect_params = {"youtube": result}
    if result == "select":
        redirect_params.update(selection_query)
    return RedirectResponse(
        url=f"{settings.youtube_frontend_result_url}{separator}{urlencode(redirect_params)}",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


@router.get(
    "/organizations/{organization_id}/youtube-pending-selections/{attempt_id}",
    response_model=YouTubePendingSelectionResponse,
    summary="List channels awaiting explicit selection",
)
async def get_youtube_pending_selection(
    organization_id: UUID,
    attempt_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePendingSelectionResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        result = await get_pending_channel_selection(
            session, organization_id, current_user.id, attempt_id
        )
    except YouTubeOAuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return YouTubePendingSelectionResponse.model_validate(result)


@router.post(
    "/organizations/{organization_id}/youtube-pending-selections/{attempt_id}",
    response_model=YouTubeChannelResponse,
    summary="Connect the explicitly selected YouTube channel",
)
async def post_youtube_pending_selection(
    organization_id: UUID,
    attempt_id: UUID,
    selection: YouTubeChannelSelectionRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeChannelResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        channel = await select_pending_channel(
            session,
            get_settings(),
            organization_id,
            current_user.id,
            attempt_id,
            selection.channel_id,
        )
        granted_scopes = sorted(set(channel.granted_scopes.split()))
        return YouTubeChannelResponse(
            id=channel.id,
            channel_id=channel.channel_id,
            title=channel.title,
            custom_url=channel.custom_url,
            thumbnail_url=channel.thumbnail_url,
            connected_at=channel.connected_at,
            granted_scopes=granted_scopes,
            can_publish=channel_can_publish(set(granted_scopes)),
        )
    except YouTubeChannelNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except YouTubeOAuthError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, httpx.HTTPError) as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503, detail="Unable to complete channel selection."
        ) from exc


@router.get(
    "/organizations/{organization_id}/youtube-channels",
    response_model=list[YouTubeChannelResponse],
    summary="List connected YouTube channels",
)
async def get_youtube_channels(
    organization_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[YouTubeChannelResponse]:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    channels = await list_channels(session, organization_id)
    responses = []
    for channel in channels:
        granted_scopes = sorted(set(channel.granted_scopes.split()))
        responses.append(
            YouTubeChannelResponse(
                id=channel.id,
                channel_id=channel.channel_id,
                title=channel.title,
                custom_url=channel.custom_url,
                thumbnail_url=channel.thumbnail_url,
                connected_at=channel.connected_at,
                granted_scopes=granted_scopes,
                can_publish=channel_can_publish(set(granted_scopes)),
            )
        )
    return responses



@router.get(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/publishing-settings",
    response_model=YouTubePublishingSettingsResponse,
    summary="Get channel automatic-publishing consent",
)
async def get_youtube_publishing_settings_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePublishingSettingsResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    channel = await session.scalar(
        select(YouTubeChannel).where(
            YouTubeChannel.id == channel_record_id,
            YouTubeChannel.organization_id == organization.id,
            YouTubeChannel.status == "active",
        )
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="YouTube channel not found.")
    granted_scopes = set(channel.granted_scopes.split())
    return YouTubePublishingSettingsResponse(
        organization_id=organization.id,
        youtube_channel_id=channel.id,
        connected_by_user_id=channel.connected_by_user_id,
        can_publish=channel_can_publish(granted_scopes),
        can_manage_auto_publish=(
            channel.connected_by_user_id == current_user.id
            and channel_can_publish(granted_scopes)
        ),
        auto_publish_enabled=channel.auto_publish_enabled,
        auto_publish_privacy_status=channel.auto_publish_privacy_status,
        automatic_execution_available=get_settings().youtube_publish_worker_enabled,
        consent_granted_by_user_id=channel.auto_publish_consent_granted_by_user_id,
        consent_granted_at=channel.auto_publish_consent_granted_at,
    )


@router.put(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/publishing-settings",
    response_model=YouTubePublishingSettingsResponse,
    summary="Enable or revoke channel automatic-publishing consent",
)
async def put_youtube_publishing_settings_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    payload: YouTubePublishingSettingsRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePublishingSettingsResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == channel_record_id,
            YouTubeChannel.organization_id == organization.id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        raise HTTPException(status_code=404, detail="YouTube channel not found.")

    granted_scopes = set(channel.granted_scopes.split())
    can_publish = channel_can_publish(granted_scopes)
    cancel_reason: str | None = None
    was_enabled = channel.auto_publish_enabled
    requested_privacy_status = (
        payload.privacy_status or channel.auto_publish_privacy_status
    )
    policy_changed = channel.auto_publish_privacy_status != requested_privacy_status
    if policy_changed and channel.connected_by_user_id != current_user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the channel owner can change its automatic-publishing privacy policy.",
        )
    if payload.auto_publish_enabled:
        if channel.connected_by_user_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Only the user who connected this channel can grant "
                    "automatic-publishing consent."
                ),
            )
        if not can_publish:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Grant YouTube upload permission for this exact channel "
                    "before enabling automatic publishing."
                ),
            )
        if not was_enabled or policy_changed:
            if was_enabled and policy_changed:
                cancel_reason = "automatic_publish_privacy_policy_changed"
            granted_at = await session.scalar(select(func.clock_timestamp()))
            if granted_at is None:
                raise RuntimeError("Could not obtain the database clock.")
            channel.auto_publish_enabled = True
            channel.auto_publish_privacy_status = requested_privacy_status
            channel.auto_publish_consent_granted_by_user_id = current_user.id
            channel.auto_publish_consent_granted_at = granted_at
            consent_event = YouTubePublishingConsentEvent(
                id=uuid4(),
                organization_id=organization.id,
                youtube_channel_id=channel.id,
                actor_user_id=current_user.id,
                action="enabled",
                reason=(
                    "explicit_channel_owner_consent"
                    if not was_enabled
                    else "automatic_publish_privacy_policy_updated"
                ),
                granted_scopes_snapshot=channel.granted_scopes,
            )
            channel.auto_publish_consent_event_id = consent_event.id
            session.add(consent_event)
    else:
        if channel.auto_publish_enabled:
            channel.auto_publish_enabled = False
            channel.auto_publish_consent_event_id = None
            session.add(
                YouTubePublishingConsentEvent(
                    organization_id=organization.id,
                    youtube_channel_id=channel.id,
                    actor_user_id=current_user.id,
                    action="disabled",
                    reason=(
                        "channel_owner_revoked"
                        if channel.connected_by_user_id == current_user.id
                        else "workspace_admin_revoked"
                    ),
                    granted_scopes_snapshot=channel.granted_scopes,
                )
            )
            cancel_reason = "automatic_publishing_consent_revoked"
        channel.auto_publish_privacy_status = requested_privacy_status

    organization_id_value = organization.id
    channel_id_value = channel.id
    actor_user_id_value = current_user.id
    await session.commit()
    if cancel_reason is not None:
        await _cancel_automatic_publish_jobs(
            session,
            organization_id=organization_id_value,
            youtube_channel_id=channel_id_value,
            actor_user_id=actor_user_id_value,
            reason=cancel_reason,
        )
        await session.commit()
    await session.refresh(channel)
    return YouTubePublishingSettingsResponse(
        organization_id=organization.id,
        youtube_channel_id=channel.id,
        connected_by_user_id=channel.connected_by_user_id,
        can_publish=can_publish,
        can_manage_auto_publish=(
            channel.connected_by_user_id == current_user.id and can_publish
        ),
        auto_publish_enabled=channel.auto_publish_enabled,
        auto_publish_privacy_status=channel.auto_publish_privacy_status,
        automatic_execution_available=get_settings().youtube_publish_worker_enabled,
        consent_granted_by_user_id=channel.auto_publish_consent_granted_by_user_id,
        consent_granted_at=channel.auto_publish_consent_granted_at,
    )




@router.get(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/publishing-consent-events",
    response_model=list[YouTubePublishingConsentEventResponse],
    summary="List channel automatic-publishing consent history",
)
async def list_youtube_publishing_consent_events_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[YouTubePublishingConsentEventResponse]:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    channel_exists = await session.scalar(
        select(YouTubeChannel.id).where(
            YouTubeChannel.id == channel_record_id,
            YouTubeChannel.organization_id == organization.id,
        )
    )
    if channel_exists is None:
        raise HTTPException(status_code=404, detail="YouTube channel not found.")
    events = await session.scalars(
        select(YouTubePublishingConsentEvent)
        .where(
            YouTubePublishingConsentEvent.organization_id == organization.id,
            YouTubePublishingConsentEvent.youtube_channel_id == channel_record_id,
        )
        .order_by(
            YouTubePublishingConsentEvent.created_at.desc(),
            YouTubePublishingConsentEvent.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        YouTubePublishingConsentEventResponse.model_validate(event)
        for event in events.all()
    ]


@router.post(
    "/organizations/{organization_id}/production-runs/{production_run_id}/youtube-publish-jobs",
    response_model=YouTubePublishJobResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Record a controlled YouTube publishing intent",
)
async def create_youtube_publish_job_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    payload: YouTubePublishJobCreateRequest,
    idempotency_key: str = Header(
        min_length=1,
        max_length=160,
        alias="Idempotency-Key",
    ),
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePublishJobResponse:
    """Persist an upload intent only after approval, channel and rights gates pass."""
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    try:
        job = await create_youtube_publish_job(
            session,
            organization_id=organization.id,
            production_run_id=production_run_id,
            requested_by_user_id=current_user.id,
            idempotency_key=idempotency_key,
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            privacy_status=payload.privacy_status,
            publish_at=payload.publish_at,
        )
        await session.commit()
        await session.refresh(job)
    except YouTubePublishingNotReadyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except YouTubePublishingConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A publishing intent already exists for this production run or idempotency key.",
        ) from exc
    except Exception:
        await session.rollback()
        raise
    return YouTubePublishJobResponse.model_validate(job)


@router.get(
    "/organizations/{organization_id}/youtube-publish-jobs",
    response_model=list[YouTubePublishJobResponse],
    summary="List YouTube publishing jobs",
)
async def list_youtube_publish_jobs_endpoint(
    organization_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[YouTubePublishJobResponse]:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    result = await session.scalars(
        select(YouTubePublishJob)
        .where(YouTubePublishJob.organization_id == organization.id)
        .order_by(YouTubePublishJob.created_at.desc(), YouTubePublishJob.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return [
        YouTubePublishJobResponse.model_validate(job)
        for job in result.all()
    ]


@router.get(
    "/organizations/{organization_id}/youtube-publish-jobs/{publish_job_id}",
    response_model=YouTubePublishJobResponse,
    summary="Get a YouTube publishing job",
)
async def get_youtube_publish_job_endpoint(
    organization_id: UUID,
    publish_job_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePublishJobResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    job = await session.scalar(
        select(YouTubePublishJob).where(
            YouTubePublishJob.id == publish_job_id,
            YouTubePublishJob.organization_id == organization.id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="YouTube publishing job not found.")
    return YouTubePublishJobResponse.model_validate(job)


@router.get(
    "/organizations/{organization_id}/youtube-publish-jobs/{publish_job_id}/events",
    response_model=list[YouTubePublishEventResponse],
    summary="List YouTube publishing audit events",
)
async def list_youtube_publish_events_endpoint(
    organization_id: UUID,
    publish_job_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[YouTubePublishEventResponse]:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    job_exists = await session.scalar(
        select(YouTubePublishJob.id).where(
            YouTubePublishJob.id == publish_job_id,
            YouTubePublishJob.organization_id == organization.id,
        )
    )
    if job_exists is None:
        raise HTTPException(status_code=404, detail="YouTube publishing job not found.")
    events = await session.scalars(
        select(YouTubePublishEvent)
        .where(
            YouTubePublishEvent.publish_job_id == publish_job_id,
            YouTubePublishEvent.organization_id == organization.id,
        )
        .order_by(
            YouTubePublishEvent.created_at.desc(),
            YouTubePublishEvent.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return [
        YouTubePublishEventResponse.model_validate(event)
        for event in events.all()
    ]


@router.post(
    "/organizations/{organization_id}/youtube-publish-jobs/{publish_job_id}/reconcile",
    response_model=YouTubePublishJobResponse,
    summary="Resume reconciliation for an existing YouTube video",
)
async def reconcile_youtube_publish_job_endpoint(
    organization_id: UUID,
    publish_job_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubePublishJobResponse:
    """Resume a provider-side video instead of creating a duplicate upload.

    A video ID means YouTube has already accepted the upload. This action marks
    the optional custom thumbnail as skipped and lets the publisher poll the
    existing video until its approved visibility is observed.
    """
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    job = await session.scalar(
        select(YouTubePublishJob)
        .where(
            YouTubePublishJob.id == publish_job_id,
            YouTubePublishJob.organization_id == organization.id,
        )
        .with_for_update()
    )
    if job is None:
        raise HTTPException(status_code=404, detail="YouTube publishing job not found.")
    if job.status != "reconciliation_required" or not job.youtube_video_id:
        raise HTTPException(
            status_code=409,
            detail=(
                "Only a reconciliation-required job with an existing YouTube video "
                "can be resumed."
            ),
        )

    snapshot = dict(job.metadata_snapshot or {})
    thumbnail = snapshot.get("thumbnail_artifact")
    if isinstance(thumbnail, dict) and not thumbnail.get("uploaded"):
        skipped_thumbnail = dict(thumbnail)
        skipped_thumbnail["upload_skipped"] = True
        skipped_thumbnail["upload_error"] = (
            "Skipped after operator reconciliation; the video remains publishable."
        )
        snapshot["thumbnail_artifact"] = skipped_thumbnail
        job.metadata_snapshot = snapshot

    job.status = "processing"
    job.failure_code = None
    job.failure_message = None
    job.next_attempt_at = func.clock_timestamp()
    job.lease_token = None
    job.lease_expires_at = None
    session.add(
        YouTubePublishEvent(
            organization_id=organization.id,
            publish_job_id=job.id,
            actor_user_id=current_user.id,
            event_type="upload_reconciliation_resumed",
            event_metadata={
                "youtube_video_id": job.youtube_video_id,
                "thumbnail_skipped": isinstance(thumbnail, dict),
            },
        )
    )
    await session.commit()
    await session.refresh(job)
    return YouTubePublishJobResponse.model_validate(job)


@router.delete(
    "/organizations/{organization_id}/youtube-channels/{channel_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Disconnect a YouTube channel",
)
async def delete_youtube_channel(
    organization_id: UUID,
    channel_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        await disconnect_channel(
            session,
            get_settings(),
            organization_id,
            channel_id,
            actor_user_id=current_user.id,
        )
    except (YouTubeOAuthError, httpx.HTTPError, ValueError) as exc:
        await session.rollback()
        raise HTTPException(
            status_code=503,
            detail="Unable to revoke YouTube credentials.",
        ) from exc


@router.get(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/profile",
    response_model=YouTubeChannelProfileResponse,
    summary="Get the current immutable editorial profile version",
)
async def get_youtube_channel_profile_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeChannelProfileResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    profile = await get_latest_channel_profile(
        session,
        organization_id=organization_id,
        youtube_channel_id=channel_record_id,
    )
    if profile is None:
        raise HTTPException(status_code=404, detail="No editorial profile is configured.")
    return YouTubeChannelProfileResponse.model_validate(profile)


@router.put(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/profile",
    response_model=YouTubeChannelProfileResponse,
    summary="Create a new immutable editorial profile version",
)
async def put_youtube_channel_profile_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    payload: YouTubeChannelProfileRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeChannelProfileResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        profile = await create_channel_profile_version(
            session,
            organization_id=organization_id,
            youtube_channel_id=channel_record_id,
            created_by_user_id=current_user.id,
            profile=payload.model_dump(mode="json"),
        )
        await session.commit()
        await session.refresh(profile)
        return YouTubeChannelProfileResponse.model_validate(profile)
    except ChannelSettingsNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/schedule",
    response_model=YouTubeChannelScheduleResponse,
    summary="Get a channel's daily publishing schedule",
)
async def get_youtube_channel_schedule_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeChannelScheduleResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    schedule = await get_channel_schedule(
        session,
        organization_id=organization_id,
        youtube_channel_id=channel_record_id,
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="No daily schedule is configured.")
    return YouTubeChannelScheduleResponse.model_validate(schedule)


@router.put(
    "/organizations/{organization_id}/youtube-channels/{channel_record_id}/schedule",
    response_model=YouTubeChannelScheduleResponse,
    summary="Create or update a channel's daily publishing schedule",
)
async def put_youtube_channel_schedule_endpoint(
    organization_id: UUID,
    channel_record_id: UUID,
    payload: YouTubeChannelScheduleRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> YouTubeChannelScheduleResponse:
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        schedule = await save_channel_schedule(
            session,
            organization_id=organization_id,
            youtube_channel_id=channel_record_id,
            updated_by_user_id=current_user.id,
            enabled=payload.enabled,
            timezone_name=payload.timezone,
            posting_time=payload.posting_time,
            catch_up_policy=payload.catch_up_policy,
        )
        await session.commit()
        await session.refresh(schedule)
        return YouTubeChannelScheduleResponse.model_validate(schedule)
    except ChannelSettingsNotFoundError as exc:
        await session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except YouTubeChannelSettingsError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
