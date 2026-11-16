import asyncio
import hashlib
import json
from pathlib import Path
from uuid import UUID

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.dependencies import get_verified_user, require_organization_roles
from app.core.config import get_settings
from app.database.session import get_db_session
from app.media.s3_storage import S3CompatibleMediaStorage
from app.media.storage import LocalMediaStorage, MediaStorageError
from app.models.evidence_item import EvidenceItem
from app.models.media_artifact import MediaArtifact
from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.research_source import ResearchSource
from app.models.story_artifact import StoryArtifactRecord
from app.models.user import User
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_schedule import YouTubeChannelSchedule
from app.models.youtube_publishing import YouTubePublishJob
from app.schemas.production import (
    ProductionReadinessCheck,
    ProductionReadinessResponse,
    ProductionRunApprovalRequest,
    ProductionRunCancelResponse,
    ProductionRunCreateRequest,
    ProductionCleanupResponse,
    ProductionRunRejectRequest,
    ProductionRunResponse,
    StoryArtifactResponse,
    StoryEvidenceExcerptResponse,
    StorySourceEvidenceResponse,
)
from app.services.production import (
    ProductionChannelNotReadyError,
    ProductionResearchNotReadyError,
    ProductionRunConflictError,
    WorkspaceVideoProductionDisabledError,
    approve_production_run,
    create_production_run,
    list_production_runs,
    reject_production_run,
    request_production_cancellation,
)
from app.services.youtube_tokens import channel_can_read_analytics
from app.story.contracts import StoryArtifact

router = APIRouter()


def _build_media_storage(settings):
    if settings.media_storage_backend == "s3":
        return S3CompatibleMediaStorage(
            endpoint_url=settings.media_s3_endpoint_url or "",
            bucket=settings.media_s3_bucket or "",
            region=settings.media_s3_region,
            access_key_id=(
                settings.media_s3_access_key_id.get_secret_value()
                if settings.media_s3_access_key_id
                else ""
            ),
            secret_access_key=(
                settings.media_s3_secret_access_key.get_secret_value()
                if settings.media_s3_secret_access_key
                else ""
            ),
            session_token=(
                settings.media_s3_session_token.get_secret_value()
                if settings.media_s3_session_token
                else None
            ),
            max_object_bytes=settings.media_storage_max_object_bytes,
            timeout_seconds=settings.media_s3_timeout_seconds,
        )
    return LocalMediaStorage(
        settings.media_storage_root,
        max_object_bytes=settings.media_storage_max_object_bytes,
    )


async def _probe_provider_models_endpoint(
    *,
    base_url: str | None,
    api_key: str | None = None,
    expected_model: str | None = None,
    native_ollama: bool = False,
) -> tuple[str, str]:
    """Check an operator-configured OpenAI-compatible /models endpoint safely."""
    if not base_url or not base_url.strip():
        return "blocked", "No provider endpoint is configured."
    headers = {"Accept": "application/json"}
    if api_key and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(4.0),
            trust_env=False,
            follow_redirects=False,
        ) as client:
            endpoint = "/api/tags" if native_ollama else "/models"
            response = await client.get(f"{base_url.rstrip('/')}{endpoint}", headers=headers)
    except httpx.HTTPError as exc:
        return (
            "blocked",
            f"Could not connect ({type(exc).__name__}). Check the host, port, and Docker network.",
        )
    if 200 <= response.status_code < 300:
        if expected_model:
            try:
                payload = response.json()
                entries = (
                    payload.get("models", [])
                    if native_ollama
                    else payload.get("data", payload.get("models", []))
                )
                model_ids = {
                    identifier
                    for item in entries
                    if isinstance(item, dict)
                    for identifier in (item.get("id"), item.get("name"), item.get("model"))
                    if isinstance(identifier, str)
                }
            except (ValueError, AttributeError, TypeError):
                return "blocked", "The endpoint responded, but its model list was invalid."
            if expected_model not in model_ids:
                return (
                    "attention",
                    f"The endpoint responded, but configured model {expected_model} "
                    "was not found in its model list.",
                )
        return "ready", "The configured endpoint responded successfully."
    return (
        "blocked",
        f"The endpoint responded with HTTP {response.status_code}. Check its URL and credentials.",
    )


@router.get(
    "/organizations/{organization_id}/production-runs/{production_run_id}/media-artifacts",
    summary="List generated media artifacts",
)
async def list_production_media_artifacts_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    assert organization.id == organization_id
    run = await session.scalar(
        select(ProductionRun.id).where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Production run not found.")
    records = await session.scalars(
        select(MediaArtifact)
        .where(
            MediaArtifact.organization_id == organization.id,
            MediaArtifact.production_run_id == production_run_id,
        )
        .order_by(MediaArtifact.created_at, MediaArtifact.id)
    )
    return [
        {
            "id": str(item.id),
            "production_stage_id": str(item.production_stage_id),
            "artifact_kind": item.artifact_kind,
            "mime_type": item.mime_type,
            "sha256": item.sha256,
            "byte_size": item.byte_size,
            "duration_ms": item.duration_ms,
            "width": item.width,
            "height": item.height,
            "metadata": item.metadata_json,
            "created_at": item.created_at.isoformat(),
            "content_url": f"/organizations/{organization.id}/media-artifacts/{item.id}/content",
        }
        for item in records
    ]


@router.get(
    "/organizations/{organization_id}/media-artifacts/{artifact_id}/content",
    summary="Read generated media content",
)
async def get_media_artifact_content_endpoint(
    organization_id: UUID,
    artifact_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    assert organization.id == organization_id
    artifact = await session.scalar(
        select(MediaArtifact).where(
            MediaArtifact.id == artifact_id,
            MediaArtifact.organization_id == organization.id,
        )
    )
    if artifact is None:
        raise HTTPException(status_code=404, detail="Media artifact not found.")
    storage = _build_media_storage(get_settings())
    try:
        content = await storage.get(artifact.storage_key)
    except (MediaStorageError, OSError):
        raise HTTPException(
            status_code=404,
            detail="Media artifact content is unavailable.",
        ) from None
    finally:
        await storage.aclose()
    if len(content) != artifact.byte_size or hashlib.sha256(content).hexdigest() != artifact.sha256:
        raise HTTPException(status_code=409, detail="Media artifact integrity check failed.")
    return Response(
        content=content,
        media_type=artifact.mime_type,
        headers={
            "Content-Length": str(artifact.byte_size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{artifact.sha256}"',
        },
    )


@router.get(
    "/organizations/{organization_id}/production-readiness",
    response_model=ProductionReadinessResponse,
    summary="Check production configuration readiness",
)
async def production_readiness_endpoint(
    organization_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionReadinessResponse:
    """Report configured prerequisites without making provider calls or publishing."""
    assert organization.id == organization_id
    settings = get_settings()
    checks: list[ProductionReadinessCheck] = []
    live_research = settings.research_provider != "mock"
    checks.append(
        ProductionReadinessCheck(
            key="research",
            status="ready" if live_research else "attention",
            title="Live research provider",
            detail=(
                f"{settings.research_provider} · topic discovery "
                f"{settings.trend_discovery_provider}; connectivity is not probed here."
                if live_research
                else "Research is using deterministic sample results."
            ),
            action=(
                None
                if live_research
                else "Configure an approved live research provider and egress route."
            ),
        )
    )
    story_ready = bool(
        settings.story_worker_enabled
        and settings.story_worker_llm_base_url
        and settings.story_worker_model
    )
    checks.append(
        ProductionReadinessCheck(
            key="story",
            status="ready" if story_ready else "blocked",
            title="Story generation",
            detail=(
                f"{settings.story_worker_provider} · {settings.story_worker_model} "
                "is configured; process and endpoint health are not probed here."
                if story_ready
                else "Story generation needs an enabled worker, model endpoint, and model name."
            ),
            action=(
                None
                if story_ready
                else "Set STORY_WORKER_ENABLED, STORY_WORKER_LLM_BASE_URL, and STORY_WORKER_MODEL."
            ),
        )
    )
    media_ready = bool(
        settings.media_worker_enabled
        and settings.media_tts_base_url
        and settings.media_tts_model
        and settings.media_tts_voice
        and settings.media_tts_rights_basis
        and (
            settings.media_visual_provider != "comfyui"
            or (
                settings.media_comfyui_base_url
                and settings.media_comfyui_workflow_path
                and settings.media_comfyui_output_node_id
            )
        )
    )
    checks.append(
        ProductionReadinessCheck(
            key="media",
            status="ready" if media_ready else "blocked",
            title="Narration and rendering",
            detail=(
                f"{settings.media_tts_provider} · {settings.media_tts_model} · "
                f"{settings.media_tts_voice}; visuals {settings.media_visual_provider} / "
                f"{settings.media_visual_composer}. TTS health is not probed here."
                if media_ready
                else (
                    "Media needs an enabled worker and licensed TTS endpoint, model, "
                    "voice, and rights basis."
                )
            ),
            action=(
                None
                if media_ready
                else "Configure the media worker and rights-cleared TTS settings."
            ),
        )
    )

    oauth_ready = bool(settings.youtube_client_id and settings.youtube_client_secret)
    checks.append(
        ProductionReadinessCheck(
            key="youtube_oauth",
            status="ready" if oauth_ready else "blocked",
            title="YouTube OAuth",
            detail=(
                "OAuth client settings are present; Google authorization is not probed here."
                if oauth_ready
                else "YouTube OAuth client credentials are missing."
            ),
            action=(None if oauth_ready else "Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET."),
        )
    )
    channels = list(
        (
            await session.scalars(
                select(YouTubeChannel)
                .where(
                    YouTubeChannel.organization_id == organization.id,
                    YouTubeChannel.status == "active",
                )
                .order_by(YouTubeChannel.title)
            )
        ).all()
    )
    if not channels:
        checks.append(
            ProductionReadinessCheck(
                key="channel",
                status="blocked",
                title="Connected channel",
                detail="No active YouTube channel is connected to this workspace.",
                action="Connect the demo channel in Integrations.",
            )
        )
    else:
        upload_ready = any(
            "https://www.googleapis.com/auth/youtube.upload" in channel.granted_scopes.split()
            for channel in channels
        )
        checks.append(
            ProductionReadinessCheck(
                key="channel",
                status="ready" if upload_ready else "attention",
                title="YouTube upload access",
                detail=(
                    f"{len(channels)} active channel(s) connected; upload scope is available."
                    if upload_ready
                    else f"{len(channels)} active channel(s) connected with read-only access."
                ),
                action=(
                    None
                    if upload_ready
                    else "Grant upload access to the channel you will use in the demo."
                ),
            )
        )
        consented = any(channel.auto_publish_enabled for channel in channels)
        checks.append(
            ProductionReadinessCheck(
                key="publish_consent",
                status="ready" if consented else "attention",
                title="Automatic publishing consent",
                detail=(
                    "At least one channel has explicit automatic-publishing consent."
                    if consented
                    else (
                        "Automatic publishing is off; approved videos can still be "
                        "uploaded manually."
                    )
                ),
                action=(
                    None
                    if consented
                    else "Keep off for review-first demos, or enable explicit consent when ready."
                ),
            )
        )

    analytics_scope_granted = any(
        channel_can_read_analytics(channel.granted_scopes or "") for channel in channels
    )
    checks.append(
        ProductionReadinessCheck(
            key="youtube_analytics",
            status=(
                "ready"
                if analytics_scope_granted
                else "blocked" if not channels else "attention"
            ),
            title="YouTube Analytics access",
            detail=(
                "YouTube and YouTube Analytics read-only permissions are granted; open "
                "Channels → Analytics to verify live report access."
                if analytics_scope_granted
                else (
                    "The channel is missing YouTube read-only and/or YouTube Analytics "
                    "read-only permission required for reports."
                )
            ),
            action=(
                None
                if analytics_scope_granted
                else (
                    "Use Channels → Reconnect for analytics, then approve both "
                    "YouTube read-only and YouTube Analytics read-only permissions."
                )
            ),
        )
    )

    scheduled = await session.scalar(
        select(YouTubeChannelSchedule.id)
        .where(
            YouTubeChannelSchedule.organization_id == organization.id,
            YouTubeChannelSchedule.enabled.is_(True),
        )
        .limit(1)
    )
    checks.append(
        ProductionReadinessCheck(
            key="daily_schedule",
            status="ready" if scheduled else "attention",
            title="Daily schedule",
            detail=(
                "A daily channel schedule is enabled."
                if scheduled
                else "No daily production schedule is enabled."
            ),
            action=(
                None
                if scheduled
                else "Set a channel profile, timezone, and daily schedule to demo automation."
            ),
        )
    )

    if settings.media_visual_provider != "comfyui":
        checks.append(
            ProductionReadinessCheck(
                key="comfyui_connectivity",
                status="attention",
                title="ComfyUI scene-image generation",
                detail=(
                    "ComfyUI is not configured. Aurora currently uses sourced stills "
                    "and HTML scenes with FFmpeg camera motion; generated ComfyUI motion "
                    "clips are not available."
                ),
                action=(
                    "Set MEDIA_VISUAL_PROVIDER=comfyui, configure an API-format "
                    "workflow and output node, and start the ComfyUI server."
                ),
            )
        )

    states = {check.status for check in checks}
    if "blocked" in states:
        summary = "blocked"
    elif "attention" in states:
        summary = "attention"
    else:
        summary = "ready"
    return ProductionReadinessResponse(
        checks=checks,
        summary=summary,
        tts_provider=settings.media_tts_provider,
        tts_voice_options=list(settings.media_tts_voices),
    )


@router.post(
    "/organizations/{organization_id}/production-readiness/verify",
    response_model=ProductionReadinessResponse,
    summary="Verify local provider and media-tool connectivity",
)
async def verify_production_readiness_endpoint(
    organization_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionReadinessResponse:
    """Make bounded, non-generative provider health checks; never publish content."""
    if organization.id != organization_id:
        raise HTTPException(status_code=404, detail="Organization not found.")
    settings = get_settings()
    checks = list(
        (
            await production_readiness_endpoint(
                organization_id=organization_id,
                organization=organization,
                session=session,
            )
        ).checks
    )

    story_key = (
        settings.story_worker_llm_api_key.get_secret_value()
        if settings.story_worker_llm_api_key is not None
        else None
    )
    tts_key = (
        settings.media_tts_api_key.get_secret_value()
        if settings.media_tts_api_key is not None
        else None
    )
    (story_status, story_detail), (tts_status, tts_detail) = await asyncio.gather(
        _probe_provider_models_endpoint(
            base_url=settings.story_worker_llm_base_url,
            api_key=story_key,
            expected_model=settings.story_worker_model,
            native_ollama=settings.story_worker_provider == "ollama",
        ),
        _probe_provider_models_endpoint(
            base_url=settings.media_tts_base_url,
            api_key=tts_key,
            expected_model=settings.media_tts_model,
        ),
    )
    if story_status == "ready":
        story_detail = (
            f"{settings.story_worker_provider} · {settings.story_worker_model}: "
            "the configured LLM endpoint responded successfully."
        )
    elif story_status == "attention":
        story_detail = f"{settings.story_worker_provider} · {story_detail}"
    if not settings.story_worker_enabled and story_status == "ready":
        story_detail += " The endpoint is reachable, but the story worker is disabled."
    checks.append(
        ProductionReadinessCheck(
            key="story_connectivity",
            status=story_status,
            title="LLM endpoint connection",
            detail=story_detail,
            action=(
                "Start the configured model service or correct STORY_WORKER_LLM_BASE_URL."
                if story_status != "ready"
                else None
            ),
        )
    )

    if tts_status == "ready":
        tts_detail = (
            f"{settings.media_tts_provider} · {settings.media_tts_model} · "
            f"voice {settings.media_tts_voice}: the configured narration endpoint "
            "responded successfully."
        )
    elif tts_status == "attention":
        tts_detail = f"{settings.media_tts_provider} · {tts_detail}"
    if not settings.media_worker_enabled and tts_status == "ready":
        tts_detail += " The media worker is disabled."
    checks.append(
        ProductionReadinessCheck(
            key="tts_connectivity",
            status=tts_status,
            title="Narration endpoint connection",
            detail=tts_detail,
            action=(
                "Start the selected TTS service or correct MEDIA_TTS_BASE_URL."
                if tts_status != "ready"
                else None
            ),
        )
    )

    if settings.media_visual_provider == "comfyui":
        comfy_status = "blocked"
        workflow_path = settings.media_comfyui_workflow_path
        workflow_ready = bool(
            workflow_path
            and Path(workflow_path).is_file()
            and settings.media_comfyui_output_node_id
        )
        comfy_detail = (
            "ComfyUI workflow file or output node is missing."
            if not workflow_ready
            else "ComfyUI endpoint is not reachable through the configured media proxy."
        )
        if settings.media_comfyui_base_url and workflow_ready:
            headers = (
                {"X-API-Key": settings.media_comfyui_api_key.get_secret_value()}
                if settings.media_comfyui_api_key is not None
                else {}
            )
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(5.0),
                    trust_env=False,
                    follow_redirects=False,
                    proxy=settings.media_visual_proxy_url,
                ) as client:
                    response = await client.get(
                        f"{settings.media_comfyui_base_url.rstrip('/')}/system_stats",
                        headers=headers,
                    )
                if response.is_success:
                    comfy_status = "ready"
                    comfy_detail = "ComfyUI responded through the restricted media egress proxy."
                else:
                    comfy_detail = (
                        f"ComfyUI responded with HTTP {response.status_code}; "
                        "check its API key and endpoint."
                    )
            except httpx.HTTPError as exc:
                comfy_detail = (
                    f"ComfyUI check failed ({type(exc).__name__}); "
                    "verify its service and proxy route."
                )
        checks.append(
            ProductionReadinessCheck(
                key="comfyui_connectivity",
                status=comfy_status,
                title="ComfyUI image workflow",
                detail=comfy_detail,
                action=(
                    None
                    if comfy_status == "ready"
                    else (
                        "Start ComfyUI and check its API key, workflow file, "
                        "endpoint, and media proxy allowlist."
                    )
                ),
            )
        )
    states = {check.status for check in checks}
    if "blocked" in states:
        summary = "blocked"
    elif "attention" in states:
        summary = "attention"
    else:
        summary = "ready"
    return ProductionReadinessResponse(
        checks=checks,
        summary=summary,
        tts_provider=settings.media_tts_provider,
        tts_voice_options=list(settings.media_tts_voices),
    )


@router.post(
    "/organizations/{organization_id}/production-runs",
    response_model=ProductionRunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a production run",
)
async def create_production_run_endpoint(
    payload: ProductionRunCreateRequest,
    organization_id: UUID,
    idempotency_key: str = Header(
        min_length=1,
        max_length=128,
        alias="Idempotency-Key",
    ),
    organization: Organization = Depends(require_organization_roles("owner", "admin", "editor")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionRunResponse:
    """Queue a production run only from completed research in this tenant."""

    assert organization.id == organization_id
    if not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank.")
    try:
        run = await create_production_run(
            session,
            organization_id=organization.id,
            requested_by_user_id=current_user.id,
            research_run_id=payload.research_run_id,
            youtube_channel_id=payload.youtube_channel_id,
            idempotency_key=idempotency_key,
            tts_override={
                key: value
                for key, value in {
                    "tts_provider": payload.tts_provider_override,
                    "tts_voice": payload.tts_voice_override,
                    "tts_speed": payload.tts_speed_override,
                    "tts_audio_format": payload.tts_audio_format_override,
                }.items()
                if value is not None
            }
            or None,
        )
        await session.commit()
        result = await session.scalar(
            select(ProductionRun)
            .options(selectinload(ProductionRun.stages))
            .where(
                ProductionRun.id == run.id,
                ProductionRun.organization_id == organization.id,
            )
        )
    except ProductionResearchNotReadyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductionChannelNotReadyError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkspaceVideoProductionDisabledError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProductionRunConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IntegrityError as exc:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail="A production run with this idempotency key already exists.",
        ) from exc
    except Exception:
        await session.rollback()
        raise

    if result is None:
        raise HTTPException(status_code=500, detail="Production run could not be loaded.")
    return ProductionRunResponse.model_validate(result)


@router.get(
    "/organizations/{organization_id}/production-runs",
    response_model=list[ProductionRunResponse],
    summary="List production runs",
)
async def list_production_runs_endpoint(
    organization_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[ProductionRunResponse]:
    assert organization.id == organization_id
    runs = await list_production_runs(
        session,
        organization_id=organization.id,
        limit=limit,
        offset=offset,
    )
    return [ProductionRunResponse.model_validate(run) for run in runs]


@router.post(
    "/organizations/{organization_id}/production-runs/cleanup",
    response_model=ProductionCleanupResponse,
    summary="Remove safe-to-delete failed production history",
)
async def cleanup_production_runs_endpoint(
    organization_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionCleanupResponse:
    """Remove only failed, cancelled, or rejected runs with no upload job."""
    assert organization.id == organization_id
    runs = list((await session.scalars(
        select(ProductionRun)
        .options(selectinload(ProductionRun.stages))
        .where(
            ProductionRun.organization_id == organization.id,
            ProductionRun.status.in_(("failed", "cancelled", "rejected")),
        )
    )).all())
    skipped: dict[str, int] = {}
    media_keys: list[tuple[str, str]] = []
    deleted_count = 0
    for run in runs:
        job_count = await session.scalar(
            select(func.count(YouTubePublishJob.id)).where(
                YouTubePublishJob.organization_id == organization.id,
                YouTubePublishJob.production_run_id == run.id,
            )
        )
        if job_count:
            skipped[run.status] = skipped.get(run.status, 0) + 1
            continue
        artifacts = await session.scalars(
            select(MediaArtifact).where(
                MediaArtifact.organization_id == organization.id,
                MediaArtifact.production_run_id == run.id,
            )
        )
        media_keys.extend((artifact.storage_backend, artifact.storage_key) for artifact in artifacts)
        await session.delete(run)
        deleted_count += 1
    await session.commit()

    storage = _build_media_storage(get_settings())
    try:
        for backend, key in media_keys:
            if backend == storage.backend:
                try:
                    await storage.delete(key)
                except (MediaStorageError, OSError):
                    # The database cleanup is safe to complete; orphan cleanup
                    # can retry a transient object-store failure later.
                    continue
    finally:
        await storage.aclose()
    return ProductionCleanupResponse(
        deleted_count=deleted_count,
        skipped_count=sum(skipped.values()),
        skipped_statuses=skipped,
    )


@router.get(
    "/organizations/{organization_id}/production-runs/{production_run_id}",
    response_model=ProductionRunResponse,
    summary="Get a production run",
)
async def get_production_run_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionRunResponse:
    assert organization.id == organization_id
    run = await session.scalar(
        select(ProductionRun)
        .options(selectinload(ProductionRun.stages))
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Production run not found.")
    return ProductionRunResponse.model_validate(run)


@router.post(
    "/organizations/{organization_id}/production-runs/{production_run_id}/cancel",
    response_model=ProductionRunCancelResponse,
    summary="Request production cancellation",
)
async def cancel_production_run_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    organization: Organization = Depends(require_organization_roles("owner", "admin", "editor")),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionRunCancelResponse:
    assert organization.id == organization_id
    run = await request_production_cancellation(
        session,
        organization_id=organization.id,
        production_run_id=production_run_id,
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Production run not found.")
    await session.commit()
    return ProductionRunCancelResponse(
        id=run.id,
        status=run.status,
        cancel_requested_at=run.cancel_requested_at,
    )


@router.post(
    "/organizations/{organization_id}/production-runs/{production_run_id}/approve",
    response_model=ProductionRunResponse,
    summary="Approve a completed production run",
)
async def approve_production_run_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    payload: ProductionRunApprovalRequest = Body(default=ProductionRunApprovalRequest()),
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionRunResponse:
    """Require an explicit owner/admin approval; this does not publish the video."""

    assert organization.id == organization_id
    try:
        run = await approve_production_run(
            session,
            organization_id=organization.id,
            production_run_id=production_run_id,
            approved_by_user_id=current_user.id,
            allow_editorial_warnings=payload.allow_editorial_warnings,
            warning_override_reason=payload.warning_override_reason,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Production run not found.")
        await session.commit()
        result = await session.scalar(
            select(ProductionRun)
            .options(selectinload(ProductionRun.stages))
            .where(
                ProductionRun.id == production_run_id,
                ProductionRun.organization_id == organization.id,
            )
        )
    except ProductionRunConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        await session.rollback()
        raise
    if result is None:
        raise HTTPException(status_code=500, detail="Production run could not be loaded.")
    return ProductionRunResponse.model_validate(result)


@router.post(
    "/organizations/{organization_id}/production-runs/{production_run_id}/reject",
    response_model=ProductionRunResponse,
    summary="Reject a completed production video",
)
async def reject_production_run_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    payload: ProductionRunRejectRequest,
    organization: Organization = Depends(require_organization_roles("owner", "admin")),
    current_user: User = Depends(get_verified_user),
    session: AsyncSession = Depends(get_db_session),
) -> ProductionRunResponse:
    """Keep the rendered artifacts while permanently preventing their upload."""
    assert organization.id == organization_id
    try:
        run = await reject_production_run(
            session,
            organization_id=organization.id,
            production_run_id=production_run_id,
            rejected_by_user_id=current_user.id,
            reason=payload.reason,
        )
        if run is None:
            raise HTTPException(status_code=404, detail="Production run not found.")
        await session.commit()
        result = await session.scalar(
            select(ProductionRun)
            .options(selectinload(ProductionRun.stages))
            .where(
                ProductionRun.id == production_run_id,
                ProductionRun.organization_id == organization.id,
            )
        )
    except ProductionRunConflictError as exc:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        await session.rollback()
        raise
    if result is None:
        raise HTTPException(status_code=500, detail="Production run could not be loaded.")
    return ProductionRunResponse.model_validate(result)


@router.get(
    "/organizations/{organization_id}/production-runs/{production_run_id}/story-artifacts",
    response_model=list[StoryArtifactResponse],
    summary="List versioned story artifacts for editorial review",
)
async def list_production_story_artifacts_endpoint(
    organization_id: UUID,
    production_run_id: UUID,
    organization: Organization = Depends(
        require_organization_roles("owner", "admin", "editor", "viewer")
    ),
    session: AsyncSession = Depends(get_db_session),
) -> list[StoryArtifactResponse]:
    """Return integrity-checked, tenant-scoped story versions for review."""

    assert organization.id == organization_id
    run = await session.scalar(
        select(ProductionRun).where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Production run not found.")

    records = await session.scalars(
        select(StoryArtifactRecord)
        .where(
            StoryArtifactRecord.organization_id == organization.id,
            StoryArtifactRecord.production_run_id == production_run_id,
        )
        .order_by(
            StoryArtifactRecord.artifact_type,
            StoryArtifactRecord.version.desc(),
        )
        .limit(40)
    )
    validated_records: list[tuple[StoryArtifactRecord, StoryArtifact]] = []
    source_ids: set[UUID] = set()
    evidence_ids: set[UUID] = set()
    for record in records:
        canonical = json.dumps(
            record.payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        if hashlib.sha256(canonical).hexdigest() != record.content_hash:
            raise HTTPException(
                status_code=409,
                detail="A stored story artifact failed its integrity check.",
            )
        try:
            payload = StoryArtifact.model_validate_json(
                json.dumps(record.payload, ensure_ascii=False)
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=409,
                detail="A stored story artifact no longer matches its schema.",
            ) from exc
        validated_records.append((record, payload))
        for reference in payload.source_references:
            source_ids.add(reference.source_id)
            evidence_ids.update(reference.evidence_ids)
        for claim in payload.factual_claims:
            for assessment in claim.evidence:
                source_ids.add(assessment.source_id)
                evidence_ids.add(assessment.evidence_id)

    source_evidence: dict[UUID, dict] = {}
    if source_ids:
        evidence_rows = await session.execute(
            select(ResearchSource, EvidenceItem)
            .outerjoin(
                EvidenceItem,
                (EvidenceItem.research_source_id == ResearchSource.id)
                & EvidenceItem.id.in_(evidence_ids),
            )
            .where(
                ResearchSource.id.in_(source_ids),
                ResearchSource.research_run_id == run.research_run_id,
            )
            .order_by(ResearchSource.title, EvidenceItem.id)
        )
        for source, evidence in evidence_rows:
            entry = source_evidence.setdefault(
                source.id,
                {
                    "id": source.id,
                    "canonical_url": source.canonical_url,
                    "title": source.title,
                    "publisher": source.publisher,
                    "source_type": source.source_type,
                    "evidence": [],
                },
            )
            if evidence is not None:
                entry["evidence"].append(
                    StoryEvidenceExcerptResponse(
                        id=evidence.id,
                        excerpt=evidence.excerpt,
                        locator=evidence.locator,
                    )
                )

    resolved_source_ids = set(source_evidence)
    resolved_evidence_ids = {
        evidence.id for source in source_evidence.values() for evidence in source["evidence"]
    }
    if source_ids - resolved_source_ids or evidence_ids - resolved_evidence_ids:
        raise HTTPException(
            status_code=409,
            detail="A story artifact references source evidence outside its research run.",
        )

    responses: list[StoryArtifactResponse] = []
    for record, payload in validated_records:
        artifact_evidence_ids: dict[UUID, set[UUID]] = {}
        for reference in payload.source_references:
            artifact_evidence_ids.setdefault(reference.source_id, set()).update(
                reference.evidence_ids
            )
        for claim in payload.factual_claims:
            for assessment in claim.evidence:
                artifact_evidence_ids.setdefault(assessment.source_id, set()).add(
                    assessment.evidence_id
                )
        context: list[StorySourceEvidenceResponse] = []
        for source_id, allowed_evidence_ids in artifact_evidence_ids.items():
            source = source_evidence.get(source_id)
            if source is None:
                continue
            context.append(
                StorySourceEvidenceResponse(
                    id=source["id"],
                    canonical_url=source["canonical_url"],
                    title=source["title"],
                    publisher=source["publisher"],
                    source_type=source["source_type"],
                    evidence=[
                        evidence
                        for evidence in source["evidence"]
                        if evidence.id in allowed_evidence_ids
                    ],
                )
            )
        responses.append(
            StoryArtifactResponse(
                id=record.id,
                production_run_id=record.production_run_id,
                artifact_type=record.artifact_type,
                schema_version=record.schema_version,
                version=record.version,
                content_hash=record.content_hash,
                created_at=record.created_at,
                payload=payload,
                evidence_context=context,
            )
        )
    return responses
