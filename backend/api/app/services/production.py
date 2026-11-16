"""Tenant-scoped durable production orchestration primitives."""

from collections.abc import Sequence
from copy import deepcopy
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from app.models.organization import Organization
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.research_run import ResearchRun
from app.models.youtube import YouTubeChannel
from app.models.youtube_channel_profile import YouTubeChannelProfile
from app.schemas.studio_settings import OrganizationStudioSettings
from app.services.operations_alerts import enqueue_operations_alert

PRODUCTION_RETRY_BASE_SECONDS = 5
PRODUCTION_RETRY_MAX_SECONDS = 900


PRODUCTION_STAGE_PLAN: tuple[str, ...] = (
    "story_generation",
    "claim_verification",
    "ai_direction",
    "narration",
    "visual_generation",
    "rendering",
    "quality_check",
    "awaiting_review",
)


class ProductionRunConflictError(ValueError):
    """The requested production run conflicts with existing state."""


class ProductionResearchNotReadyError(ValueError):
    """The research run is missing, belongs elsewhere, or is incomplete."""


class ProductionChannelNotReadyError(ValueError):
    """The target channel is missing, inactive, or has no editorial profile."""


class WorkspaceVideoProductionDisabledError(ValueError):
    """The organization has paused creation of new video production runs."""


async def create_production_run(
    session: AsyncSession,
    *,
    organization_id: UUID,
    requested_by_user_id: UUID,
    research_run_id: UUID,
    youtube_channel_id: UUID,
    idempotency_key: str,
    tts_override: dict | None = None,
    scheduled_local_date: date | None = None,
    scheduled_for: datetime | None = None,
) -> ProductionRun:
    """Create a durable run and its immutable initial stage plan."""

    key = idempotency_key.strip()
    if not key or len(key) > 128:
        raise ValueError("Idempotency key must contain 1 to 128 characters.")

    existing = await session.scalar(
        select(ProductionRun).where(
            ProductionRun.organization_id == organization_id,
            ProductionRun.idempotency_key == key,
        )
    )
    if existing is not None:
        existing_override = (existing.channel_profile_snapshot or {}).get("_tts_run_override", {})
        if (
            existing.research_run_id != research_run_id
            or existing.youtube_channel_id != youtube_channel_id
            or existing_override != (tts_override or {})
        ):
            raise ProductionRunConflictError(
                "Idempotency key was already used for a different run, channel or TTS override."
            )
        return existing

    organization = await session.get(Organization, organization_id)
    if organization is None:
        raise ProductionResearchNotReadyError("Workspace not found.")
    workspace_settings = OrganizationStudioSettings.model_validate(
        organization.studio_settings or {}
    )
    if not workspace_settings.video_generation_enabled:
        raise WorkspaceVideoProductionDisabledError(
            "Video generation is paused in this workspace's Studio settings."
        )

    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == youtube_channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        raise ProductionChannelNotReadyError(
            "An active YouTube channel in this workspace is required."
        )
    research_run = await session.scalar(
        select(ResearchRun).where(
            ResearchRun.id == research_run_id,
            ResearchRun.organization_id == organization_id,
        )
    )
    if research_run is None or research_run.status != "completed":
        raise ProductionResearchNotReadyError(
            "A completed research run in this organization is required."
        )

    if research_run.youtube_channel_id is not None:
        if (
            research_run.youtube_channel_id != youtube_channel_id
            or research_run.channel_profile_id is None
            or research_run.channel_profile_version is None
            or research_run.channel_profile_snapshot is None
        ):
            raise ProductionResearchNotReadyError(
                "The research run is bound to a different channel or incomplete profile snapshot."
            )
        profile = await session.scalar(
            select(YouTubeChannelProfile).where(
                YouTubeChannelProfile.id == research_run.channel_profile_id,
                YouTubeChannelProfile.organization_id == organization_id,
                YouTubeChannelProfile.youtube_channel_id == youtube_channel_id,
                YouTubeChannelProfile.version == research_run.channel_profile_version,
            )
        )
        if profile is None:
            raise ProductionResearchNotReadyError(
                "The research run's editorial profile version is unavailable."
            )
        if scheduled_local_date is None:
            scheduled_local_date = research_run.scheduled_local_date
        if scheduled_for is None:
            scheduled_for = research_run.scheduled_for
        profile_snapshot = deepcopy(research_run.channel_profile_snapshot)
    else:
        profile = await session.scalar(
            select(YouTubeChannelProfile)
            .where(
                YouTubeChannelProfile.organization_id == organization_id,
                YouTubeChannelProfile.youtube_channel_id == youtube_channel_id,
            )
            .order_by(YouTubeChannelProfile.version.desc())
            .limit(1)
        )
        if profile is None:
            raise ProductionChannelNotReadyError(
                "Configure an editorial profile for this YouTube channel before production."
            )
        profile_snapshot = deepcopy(profile.profile)

    if tts_override:
        profile_snapshot["_tts_run_override"] = deepcopy(tts_override)
    profile_snapshot["_workspace_preferences"] = workspace_settings.model_dump(mode="json")

    if (scheduled_local_date is None) != (scheduled_for is None):
        raise ValueError("Scheduled local date and timestamp must be supplied together.")
    if research_run.youtube_channel_id is not None and (
        research_run.scheduled_local_date != scheduled_local_date
        or research_run.scheduled_for != scheduled_for
    ):
        raise ProductionResearchNotReadyError(
            "The scheduled production date does not match the bound research run."
        )

    run = ProductionRun(
        organization_id=organization_id,
        research_run_id=research_run_id,
        requested_by_user_id=requested_by_user_id,
        youtube_channel_id=youtube_channel_id,
        channel_profile_id=profile.id,
        channel_profile_version=profile.version,
        channel_profile_snapshot=profile_snapshot,
        scheduled_local_date=scheduled_local_date,
        scheduled_for=scheduled_for,
        idempotency_key=key,
        status="queued",
    )
    session.add(run)
    await session.flush()

    session.add_all(
        ProductionStage(
            production_run_id=run.id,
            stage_key=stage_key,
            idempotency_key=f"{run.id}:{stage_key}",
            status="pending",
            input_manifest={},
            output_manifest={},
        )
        for stage_key in PRODUCTION_STAGE_PLAN
    )
    await session.flush()
    return run


async def get_production_run(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
) -> ProductionRun | None:
    result = await session.scalars(
        select(ProductionRun)
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
        .options(selectinload(ProductionRun.stages))
    )
    run = result.one_or_none()
    return run


async def list_production_runs(
    session: AsyncSession,
    *,
    organization_id: UUID,
    limit: int = 20,
    offset: int = 0,
) -> Sequence[ProductionRun]:
    result = await session.scalars(
        select(ProductionRun)
        .options(selectinload(ProductionRun.stages))
        .where(ProductionRun.organization_id == organization_id)
        .order_by(ProductionRun.created_at.desc(), ProductionRun.id.desc())
        .limit(limit)
        .offset(offset)
    )
    return result.all()


async def request_production_cancellation(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
) -> ProductionRun | None:
    """Record cancellation intent and atomically cancel every unstarted stage."""

    run = await session.scalar(
        select(ProductionRun).where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
    )
    if run is None:
        return None
    if run.status in {"awaiting_review", "approved", "failed"}:
        return run

    # Workers claim, complete and recover stages before locking their parent
    # run. Cancellation follows the same stage-then-run lock order. Lock every
    # stage first so a concurrent lease recovery cannot requeue a stage after
    # cancellation has already committed the run's terminal state.
    stages = list(
        (
            await session.scalars(
                select(ProductionStage)
                .where(ProductionStage.production_run_id == production_run_id)
                .order_by(ProductionStage.created_at, ProductionStage.id)
                .with_for_update()
            )
        ).all()
    )

    run = await session.scalar(
        select(ProductionRun)
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        return None
    if run.status in {"awaiting_review", "approved", "failed"}:
        return run

    now = await session.scalar(select(func.clock_timestamp()))
    if now is None:
        raise RuntimeError("Could not obtain the database clock.")

    run.cancel_requested_at = run.cancel_requested_at or now
    has_running_stage = False
    for stage in stages:
        if stage.status == "pending":
            stage.status = "cancelled"
            stage.completed_at = now
        elif stage.status == "running":
            has_running_stage = True
            # A cancelled run is excluded from new claims. Expire its active
            # lease immediately so the worker's normal recovery loop can mark
            # it terminal without waiting for the full provider timeout.
            stage.lease_expires_at = now

    if not has_running_stage:
        run.status = "cancelled"
        run.completed_at = run.completed_at or now

    await session.flush()
    return run


async def claim_production_stage(
    worker_id: str,
    *,
    lease_seconds: int = 300,
    stage_keys: Sequence[str] | None = None,
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[UUID, UUID, str, int] | None:
    """Claim the earliest runnable stage with a database lease and row lock."""

    if not worker_id.strip():
        raise ValueError("Worker ID cannot be empty.")
    if lease_seconds < 1:
        raise ValueError("Lease duration must be positive.")
    if stage_keys is not None and not stage_keys:
        raise ValueError("At least one stage key must be provided.")

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")

            predecessor = {
                key: PRODUCTION_STAGE_PLAN[index - 1] if index else None
                for index, key in enumerate(PRODUCTION_STAGE_PLAN)
            }
            candidate_query = (
                select(ProductionStage)
                .join(ProductionRun, ProductionRun.id == ProductionStage.production_run_id)
                .where(
                    ProductionStage.status == "pending",
                    ProductionStage.next_attempt_at <= now,
                    ProductionRun.status.in_({"queued", "planning", "generating", "verifying"}),
                    ProductionRun.cancel_requested_at.is_(None),
                )
            )
            if stage_keys is not None:
                candidate_query = candidate_query.where(ProductionStage.stage_key.in_(stage_keys))
            candidates = (
                candidate_query.order_by(ProductionStage.created_at, ProductionStage.id)
                .limit(50)
                .with_for_update(of=ProductionStage, skip_locked=True)
            )
            stages = list((await session.scalars(candidates)).all())
            for stage in stages:
                previous_key = predecessor.get(stage.stage_key)
                if previous_key is not None:
                    previous = await session.scalar(
                        select(ProductionStage.id).where(
                            ProductionStage.production_run_id == stage.production_run_id,
                            ProductionStage.stage_key == previous_key,
                            ProductionStage.status == "completed",
                        )
                    )
                    if previous is None:
                        continue

                run = await session.scalar(
                    select(ProductionRun)
                    .where(ProductionRun.id == stage.production_run_id)
                    .with_for_update()
                )
                if run is None or run.cancel_requested_at is not None:
                    continue

                token = uuid4()
                stage.status = "running"
                stage.attempt_count += 1
                stage.lease_token = token
                stage.lease_expires_at = now + timedelta(seconds=lease_seconds)
                stage.started_at = stage.started_at or now
                run.status = (
                    "planning"
                    if stage.stage_key in {"story_generation", "claim_verification", "ai_direction"}
                    else "generating"
                )
                run.started_at = run.started_at or now
                run.attempt_count += 1
                await session.flush()
                return run.id, stage.id, str(token), stage.attempt_count

    return None


async def renew_production_stage_lease(
    stage_id: UUID,
    lease_token: UUID,
    *,
    lease_seconds: int = 300,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    if lease_seconds < 1:
        raise ValueError("Lease duration must be positive.")
    async with session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(ProductionStage)
                .where(
                    ProductionStage.id == stage_id,
                    ProductionStage.status == "running",
                    ProductionStage.lease_token == lease_token,
                    ProductionStage.lease_expires_at > func.clock_timestamp(),
                )
                .values(lease_expires_at=func.clock_timestamp() + timedelta(seconds=lease_seconds))
            )
            return result.rowcount == 1


async def record_production_stage_failure(
    stage_id: UUID,
    lease_token: UUID,
    *,
    max_attempts: int,
    failure_code: str,
    failure_message: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Release a known failed attempt immediately and retry it with bounded backoff."""
    if max_attempts < 1:
        raise ValueError("Maximum attempts must be positive.")
    if not failure_code.strip() or len(failure_code) > 80:
        raise ValueError("Failure code must contain 1 to 80 characters.")
    if not failure_message.strip() or len(failure_message) > 1000:
        raise ValueError("Failure message must contain 1 to 1000 characters.")

    alert: tuple[UUID, UUID, str] | None = None
    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")
            stage = await session.scalar(
                select(ProductionStage)
                .where(
                    ProductionStage.id == stage_id,
                    ProductionStage.status == "running",
                    ProductionStage.lease_token == lease_token,
                )
                .with_for_update()
            )
            if stage is None:
                return False
            run = await session.scalar(
                select(ProductionRun)
                .where(ProductionRun.id == stage.production_run_id)
                .with_for_update()
            )
            if run is None:
                return False

            stage.lease_token = None
            stage.lease_expires_at = None
            stage.failure_code = failure_code
            stage.failure_message = failure_message
            if run.cancel_requested_at is not None:
                stage.status = "cancelled"
                stage.completed_at = now
                pending = await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == run.id,
                        ProductionStage.status == "pending",
                    )
                )
                for pending_stage in pending:
                    pending_stage.status = "cancelled"
                    pending_stage.completed_at = now
                run.status = "cancelled"
                run.completed_at = now
            elif stage.attempt_count >= max_attempts:
                stage.status = "failed"
                stage.completed_at = now
                run.status = "failed"
                run.failure_code = "story_stage_retry_exhausted"
                run.failure_message = "A story production stage exhausted its retry budget."
                run.completed_at = now
                run.lease_token = None
                run.lease_expires_at = None
                alert = (run.organization_id, run.id, stage.stage_key)
            else:
                stage.status = "pending"
                retry_delay = min(
                    PRODUCTION_RETRY_BASE_SECONDS * (2 ** (stage.attempt_count - 1)),
                    PRODUCTION_RETRY_MAX_SECONDS,
                )
                stage.next_attempt_at = now + timedelta(seconds=retry_delay)

    if alert is not None:
        organization_id, run_id, stage_key = alert
        await enqueue_operations_alert(
            session_factory,
            organization_id=organization_id,
            subject="Aurora Studio: story production run failed",
            body=(
                "A story production stage exhausted its retry budget.\n\n"
                f"Production run ID: {run_id}\n"
                f"Failed stage: {stage_key}\n"
                f"Failure code: {failure_code}\n\n"
                "Inspect the run and stage history in Aurora Studio before retrying."
            ),
        )
    return True


async def recover_expired_production_stage_leases(
    *,
    max_attempts: int = 5,
    batch_size: int = 50,
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """Requeue expired stages, or fail the run after the retry budget is exhausted."""

    if max_attempts < 1 or batch_size < 1:
        raise ValueError("Recovery limits must be positive.")
    recovered = 0
    alerts: list[tuple[UUID, UUID, str]] = []
    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")
            stages = list(
                (
                    await session.scalars(
                        select(ProductionStage)
                        .where(
                            ProductionStage.status == "running",
                            ProductionStage.lease_expires_at <= now,
                        )
                        .order_by(ProductionStage.lease_expires_at, ProductionStage.id)
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for stage in stages:
                run = await session.scalar(
                    select(ProductionRun)
                    .where(ProductionRun.id == stage.production_run_id)
                    .with_for_update()
                )
                stage.lease_token = None
                stage.lease_expires_at = None
                if run is not None and run.cancel_requested_at is not None:
                    stage.status = "cancelled"
                    stage.completed_at = now
                    pending = await session.scalars(
                        select(ProductionStage).where(
                            ProductionStage.production_run_id == run.id,
                            ProductionStage.status == "pending",
                        )
                    )
                    for pending_stage in pending:
                        pending_stage.status = "cancelled"
                        pending_stage.completed_at = now
                    run.status = "cancelled"
                    run.completed_at = now
                elif stage.attempt_count >= max_attempts:
                    should_alert = run is not None and run.status != "failed"
                    stage.status = "failed"
                    stage.failure_code = "lease_expired"
                    stage.failure_message = "Production stage lease expired after maximum attempts."
                    stage.completed_at = now
                    if run is not None:
                        run.status = "failed"
                        run.failure_code = "stage_lease_expired"
                        run.failure_message = "A production stage exhausted its retry budget."
                        run.completed_at = now
                        run.lease_token = None
                        run.lease_expires_at = None
                        if should_alert:
                            alerts.append((run.organization_id, run.id, stage.stage_key))
                else:
                    stage.status = "pending"
                    retry_delay = min(
                        PRODUCTION_RETRY_BASE_SECONDS * (2 ** (stage.attempt_count - 1)),
                        PRODUCTION_RETRY_MAX_SECONDS,
                    )
                    stage.next_attempt_at = now + timedelta(seconds=retry_delay)
                    stage.failure_code = "lease_expired"
                    stage.failure_message = "Stage lease expired; it was returned to the queue."
                recovered += 1

    for organization_id, run_id, stage_key in alerts:
        await enqueue_operations_alert(
            session_factory,
            organization_id=organization_id,
            subject="Aurora Studio: production run failed",
            body=(
                "A production stage exhausted its retry budget.\n\n"
                f"Production run ID: {run_id}\n"
                f"Failed stage: {stage_key}\n"
                "Failure code: stage_lease_expired\n\n"
                "Inspect the run and stage history in Aurora Studio before retrying."
            ),
        )
    return recovered


async def _try_create_consent_gated_publish_job(
    session: AsyncSession,
    *,
    run: ProductionRun,
    approved_at: datetime,
) -> bool:
    """Auto-approve and enqueue only from current channel-owner consent and final QA."""
    from app.core.config import get_settings
    from app.models.story_artifact import StoryArtifactRecord
    from app.services.youtube_publishing import (
        YouTubePublishingError,
        create_youtube_publish_job,
        get_current_automatic_publishing_consent,
    )

    if (
        not get_settings().youtube_publish_worker_enabled
        or run.youtube_channel_id is None
        or not isinstance(run.channel_profile_snapshot, dict)
    ):
        return False
    preferences = run.channel_profile_snapshot.get("_workspace_preferences", {})
    if not isinstance(preferences, dict) or not preferences.get(
        "automatic_publishing_enabled", False
    ):
        return False
    organization = await session.get(Organization, run.organization_id)
    if organization is None:
        return False
    current_preferences = OrganizationStudioSettings.model_validate(
        organization.studio_settings or {}
    )
    if not current_preferences.automatic_publishing_enabled:
        return False
    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == run.youtube_channel_id,
            YouTubeChannel.organization_id == run.organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        return False
    stage_rows = (
        await session.execute(
            select(ProductionStage.stage_key, ProductionStage.status).where(
                ProductionStage.production_run_id == run.id
            )
        )
    ).all()
    if {stage_key for stage_key, _ in stage_rows} != set(PRODUCTION_STAGE_PLAN) or any(
        status != "completed" for _, status in stage_rows
    ):
        return False
    consent_event = await get_current_automatic_publishing_consent(session, channel=channel)
    if consent_event is None or consent_event.actor_user_id is None:
        return False

    story = await session.scalar(
        select(StoryArtifactRecord)
        .where(
            StoryArtifactRecord.organization_id == run.organization_id,
            StoryArtifactRecord.production_run_id == run.id,
            StoryArtifactRecord.artifact_type == "story_verified",
        )
        .order_by(StoryArtifactRecord.version.desc(), StoryArtifactRecord.id.desc())
        .limit(1)
    )
    if story is None or not isinstance(story.payload, dict):
        return False
    title = story.payload.get("title")
    description = story.payload.get("hook")
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title.strip()) > 100
        or not isinstance(description, str)
        or not description.strip()
    ):
        return False

    tags: list[str] = []
    profile = run.channel_profile_snapshot
    topic_boundaries = profile.get("topic_boundaries")
    if not isinstance(topic_boundaries, list):
        topic_boundaries = []
    raw_tags = [profile.get("niche"), *topic_boundaries]
    for raw_tag in raw_tags:
        if not isinstance(raw_tag, str):
            continue
        tag = raw_tag.strip()
        if (
            tag
            and len(tag) <= 100
            and tag not in tags
            and len(tags) < 30
            and sum(len(item) for item in tags) + len(tag) + len(tags) + 1 <= 500
        ):
            tags.append(tag)

    publish_at = (
        run.scheduled_for
        if run.scheduled_for is not None and run.scheduled_for > approved_at
        else None
    )
    if publish_at is not None and channel.auto_publish_privacy_status != "public":
        # YouTube scheduled publication transitions a private video to public.
        # Do not silently publish an unlisted channel's video at a different time.
        return False
    publish_privacy_status = (
        "private" if publish_at is not None else channel.auto_publish_privacy_status
    )

    try:
        async with session.begin_nested():
            run.status = "approved"
            run.approval_mode = "automatic_consent"
            run.approved_by_user_id = None
            run.approved_at = approved_at
            run.approval_consent_event_id = consent_event.id
            await create_youtube_publish_job(
                session,
                organization_id=run.organization_id,
                production_run_id=run.id,
                requested_by_user_id=consent_event.actor_user_id,
                idempotency_key=f"auto-consent:{run.id}",
                title=title.strip(),
                description=description.strip(),
                tags=tags,
                privacy_status=publish_privacy_status,
                publish_at=publish_at,
            )
    except (YouTubePublishingError, IntegrityError):
        run.status = "awaiting_review"
        run.approval_mode = "none"
        run.approved_by_user_id = None
        run.approved_at = None
        run.approval_consent_event_id = None
        await session.flush()
        return False
    return True


async def complete_production_stage(
    stage_id: UUID,
    lease_token: UUID,
    *,
    output_manifest: dict,
    provider_id: str | None = None,
    model_id: str | None = None,
    prompt_version: str | None = None,
    request_id: str | None = None,
    session_factory: async_sessionmaker[AsyncSession],
) -> bool:
    """Commit stage output only while the caller owns an unexpired lease."""

    async with session_factory() as session:
        async with session.begin():
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")
            stage = await session.scalar(
                select(ProductionStage)
                .where(
                    ProductionStage.id == stage_id,
                    ProductionStage.status == "running",
                    ProductionStage.lease_token == lease_token,
                    ProductionStage.lease_expires_at > now,
                )
                .with_for_update()
            )
            if stage is None:
                return False
            stage.status = "completed"
            stage.output_manifest = output_manifest
            stage.provider_id = provider_id
            stage.model_id = model_id
            stage.prompt_version = prompt_version
            stage.request_id = request_id
            stage.lease_token = None
            stage.lease_expires_at = None
            stage.completed_at = now
            stage.failure_code = None
            stage.failure_message = None

            review_stage = None
            if stage.stage_key == "quality_check":
                review_stage = await session.scalar(
                    select(ProductionStage)
                    .where(
                        ProductionStage.production_run_id == stage.production_run_id,
                        ProductionStage.stage_key == "awaiting_review",
                        ProductionStage.status == "pending",
                    )
                    .with_for_update()
                )
                if review_stage is None:
                    raise RuntimeError("The production run is missing its pending review stage.")

            run = await session.scalar(
                select(ProductionRun)
                .where(ProductionRun.id == stage.production_run_id)
                .with_for_update()
            )
            if run is None:
                raise RuntimeError("Production run disappeared while completing a stage.")
            if run.cancel_requested_at is not None:
                pending = await session.scalars(
                    select(ProductionStage).where(
                        ProductionStage.production_run_id == run.id,
                        ProductionStage.status == "pending",
                    )
                )
                for pending_stage in pending:
                    pending_stage.status = "cancelled"
                    pending_stage.completed_at = now
                run.status = "cancelled"
                run.completed_at = now
            elif stage.stage_key == "quality_check":
                # Close the review marker in the same transaction as the final
                # quality stage. No no-op worker should be required to expose a
                # finished artifact for human review.
                assert review_stage is not None
                review_stage.status = "completed"
                review_stage.output_manifest = {"transition": "awaiting_review"}
                review_stage.completed_at = now
                run.status = "awaiting_review"
                run.completed_at = now
                await _try_create_consent_gated_publish_job(
                    session,
                    run=run,
                    approved_at=now,
                )
            elif stage.stage_key == "awaiting_review":
                # Backward-compatible fallback for runs created before the
                # quality-check transition was made explicit.
                run.status = "awaiting_review"
                run.completed_at = now
            return True


async def approve_production_run(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    approved_by_user_id: UUID,
    allow_editorial_warnings: bool = False,
    warning_override_reason: str | None = None,
) -> ProductionRun | None:
    """Record the authorized approver and timestamp after all stages finish."""

    run = await session.scalar(
        select(ProductionRun)
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
        .with_for_update()
    )
    if run is None:
        return None
    if run.status != "awaiting_review":
        raise ProductionRunConflictError(
            "A production run can be approved only after it reaches awaiting_review."
        )
    incomplete_stage = await session.scalar(
        select(ProductionStage.id).where(
            ProductionStage.production_run_id == run.id,
            ProductionStage.status != "completed",
        )
    )
    if incomplete_stage is not None:
        raise ProductionRunConflictError(
            "Every production stage must be completed before approval."
        )
    quality_stage = await session.scalar(
        select(ProductionStage).where(
            ProductionStage.production_run_id == run.id,
            ProductionStage.stage_key == "quality_check",
            ProductionStage.status == "completed",
        )
    )
    if quality_stage is None:
        raise ProductionRunConflictError("A completed media quality-check stage is required.")
    quality_manifest = quality_stage.output_manifest
    if not isinstance(quality_manifest, dict):
        raise ProductionRunConflictError("Media quality-check manifest is invalid.")
    quality_report = quality_manifest.get("quality_report")
    if not isinstance(quality_report, dict) or quality_report.get("is_valid") is not True:
        raise ProductionRunConflictError("A valid media quality report is required for approval.")
    if not isinstance(quality_manifest.get("publication_blocked"), bool):
        raise ProductionRunConflictError("Media factuality and rights status is missing.")
    # The first renderer versions only wrote ``publication_blocked`` and the
    # quality report.  Treat omitted optional flag lists as empty for those
    # historical artifacts; a present value still has to be a list so a
    # malformed manifest cannot silently pass the gate.
    claim_flags = quality_manifest.get("claim_flags", [])
    rights_flags = quality_manifest.get("rights_flags", [])
    media_manifest_flags = quality_manifest.get("media_manifest_flags", [])
    quality_issues = quality_report.get("issues", [])
    if (
        not isinstance(claim_flags, list)
        or not isinstance(rights_flags, list)
        or not isinstance(media_manifest_flags, list)
        or not isinstance(quality_issues, list)
        or media_manifest_flags
        or quality_issues
    ):
        raise ProductionRunConflictError(
            "This render has technical or media-manifest failures. Fix and regenerate "
            "it before uploading; an editorial warning override cannot bypass these checks."
        )
    has_editorial_warnings = bool(
        quality_manifest["publication_blocked"] or claim_flags or rights_flags
    )
    if has_editorial_warnings:
        if not claim_flags and not rights_flags:
            raise ProductionRunConflictError(
                "The run is blocked without an explicit factual or rights warning to review. "
                "This warning override is unavailable."
            )
        reason = (warning_override_reason or "").strip()
        if not allow_editorial_warnings or len(reason) < 10:
            raise ProductionRunConflictError(
                "Publishing is blocked by factual or source-rights warnings. Review the "
                "warning list and provide an explicit reason to use the audited upload override."
            )
        run.approval_warning_override = {
            "reason": reason,
            "claim_flags": claim_flags,
            "rights_flags": rights_flags,
            "publication_blocked": quality_manifest["publication_blocked"],
            "accepted_by_user_id": str(approved_by_user_id),
        }
    else:
        if quality_manifest["publication_blocked"] is not False:
            raise ProductionRunConflictError(
                "The publication gate reported a blocked state without reviewable warnings."
            )
        run.approval_warning_override = None
    approved_at = await session.scalar(select(func.clock_timestamp()))
    if approved_at is None:
        raise RuntimeError("Could not obtain the database clock.")
    run.status = "approved"
    run.approval_mode = "human"
    run.approval_consent_event_id = None
    run.approved_by_user_id = approved_by_user_id
    run.approved_at = approved_at
    run.completed_at = approved_at
    await session.flush()
    return run


async def reject_production_run(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    rejected_by_user_id: UUID,
    reason: str,
) -> ProductionRun | None:
    """Record an explicit editorial rejection without deleting the rendered work."""
    run = await session.scalar(
        select(ProductionRun)
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
        .with_for_update()
    )
    if run is None:
        return None
    if run.status != "awaiting_review":
        raise ProductionRunConflictError(
            "A video can be rejected only while it is awaiting editorial review."
        )
    rejected_at = await session.scalar(select(func.clock_timestamp()))
    if rejected_at is None:
        raise RuntimeError("Could not obtain the database clock.")
    run.status = "rejected"
    run.rejected_by_user_id = rejected_by_user_id
    run.rejected_at = rejected_at
    run.rejection_reason = reason.strip() or "Rejected by editor."
    run.completed_at = rejected_at
    await session.flush()
    return run
