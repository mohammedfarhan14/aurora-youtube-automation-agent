import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media_artifact import MediaArtifact
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage
from app.models.youtube import YouTubeChannel
from app.models.youtube_publishing import (
    YouTubePublishEvent,
    YouTubePublishingConsentEvent,
    YouTubePublishJob,
)
from app.services.youtube_oauth import channel_can_publish


class YouTubePublishingError(RuntimeError):
    """Base class for rejected or conflicting YouTube publishing intents."""


class YouTubePublishingNotReadyError(YouTubePublishingError):
    """The production run, channel or media artifact is not ready for upload."""


class YouTubePublishingConflictError(YouTubePublishingError):
    """A conflicting or duplicate publishing intent already exists."""


UNVERIFIED_LICENSE_LINE = (
    "License: not verified — editorial warning accepted by the channel owner."
)


def _credit_lines(
    credits: object,
    *,
    allow_unverified_rights: bool = False,
) -> list[str]:
    if not isinstance(credits, list) or not credits:
        raise YouTubePublishingNotReadyError(
            "A verified attribution manifest is required before YouTube upload."
        )

    lines: list[str] = []
    for credit in credits:
        if not isinstance(credit, dict):
            raise YouTubePublishingNotReadyError(
                "The rendered attribution manifest contains an invalid entry."
            )
        attribution = credit.get("attribution")
        if not isinstance(attribution, str) or not attribution.strip():
            raise YouTubePublishingNotReadyError(
                "Every rendered source or media asset must have attribution text."
            )
        lines.append(attribution.strip())

        license_ids = credit.get("license_ids")
        license_urls = credit.get("license_urls")
        if isinstance(license_ids, list) or isinstance(license_urls, list):
            valid_license_refs = (
                isinstance(license_ids, list)
                and bool(license_ids)
                and isinstance(license_urls, list)
                and len(license_ids) == len(license_urls)
                and all(
                    isinstance(license_id, str)
                    and bool(license_id.strip())
                    and isinstance(license_url, str)
                    and license_url.startswith("https://")
                    for license_id, license_url in zip(license_ids, license_urls, strict=True)
                )
            )
            if not valid_license_refs:
                if not allow_unverified_rights:
                    raise YouTubePublishingNotReadyError(
                        "A rendered source credit is missing its license references."
                    )
                lines.append(UNVERIFIED_LICENSE_LINE)
            else:
                lines.extend(
                    f"License: {license_id} — {license_url}"
                    for license_id, license_url in zip(license_ids, license_urls, strict=True)
                )
        else:
            license_id = credit.get("license_id")
            license_uri = credit.get("license_uri")
            valid_license_ref = (
                isinstance(license_id, str)
                and bool(license_id.strip())
                and isinstance(license_uri, str)
                and license_uri.startswith("https://")
            )
            if not valid_license_ref:
                if not allow_unverified_rights:
                    raise YouTubePublishingNotReadyError(
                        "A rendered media credit is missing its license reference."
                    )
                lines.append(UNVERIFIED_LICENSE_LINE)
            else:
                lines.append(f"License: {license_id} — {license_uri}")

        source_uri = credit.get("source_uri")
        if isinstance(source_uri, str) and source_uri:
            if not source_uri.startswith("https://"):
                raise YouTubePublishingNotReadyError(
                    "Every rendered source credit must use a safe HTTPS source URL."
                )
            lines.append(f"Source: {source_uri}")
        modification_note = credit.get("modification_note")
        if isinstance(modification_note, str) and modification_note:
            lines.append(modification_note)
        lines.append("")

    return lines


def build_youtube_description(
    base_description: str,
    credits: object,
    *,
    allow_unverified_rights: bool = False,
) -> str:
    """Append immutable render credits; never allow client input to replace them."""
    credit_lines = _credit_lines(credits, allow_unverified_rights=allow_unverified_rights)
    prefix = base_description.strip()
    section = "Sources and credits\n" + "\n".join(credit_lines).strip()
    description = f"{prefix}\n\n{section}" if prefix else section
    if len(description) > 5000:
        raise YouTubePublishingNotReadyError(
            "The description and mandatory attribution credits exceed "
            "YouTube's 5,000-character limit."
        )
    return description


async def get_current_automatic_publishing_consent(
    session: AsyncSession,
    *,
    channel: YouTubeChannel,
) -> YouTubePublishingConsentEvent | None:
    """Return the latest enabled consent event only while it matches current channel state."""
    if (
        channel.status != "active"
        or not channel.auto_publish_enabled
        or channel.auto_publish_consent_granted_by_user_id is None
        or channel.auto_publish_consent_granted_at is None
        or not channel_can_publish(set((channel.granted_scopes or "").split()))
    ):
        return None
    if channel.auto_publish_consent_event_id is None:
        return None
    event = await session.get(
        YouTubePublishingConsentEvent, channel.auto_publish_consent_event_id
    )
    if (
        event is None
        or event.organization_id != channel.organization_id
        or event.youtube_channel_id != channel.id
        or event.action != "enabled"
        or event.actor_user_id != channel.auto_publish_consent_granted_by_user_id
    ):
        return None
    return event


async def create_youtube_publish_job(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    requested_by_user_id: UUID,
    idempotency_key: str,
    title: str,
    description: str,
    tags: list[str],
    privacy_status: str,
    publish_at: datetime | None,
) -> YouTubePublishJob:
    """Persist an upload intent only after rechecking approval, channel and QA state."""
    request_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "title": title.strip(),
                "description": description.strip(),
                "tags": tags,
                "privacy_status": privacy_status,
                "publish_at": publish_at.isoformat() if publish_at else None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    existing = await session.scalar(
        select(YouTubePublishJob).where(
            YouTubePublishJob.organization_id == organization_id,
            YouTubePublishJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if (
            existing.production_run_id != production_run_id
            or existing.metadata_snapshot.get("request_sha256") != request_fingerprint
        ):
            raise YouTubePublishingConflictError(
                "This idempotency key is already bound to a different publishing request."
            )
        return existing

    run = await session.scalar(
        select(ProductionRun)
        .where(
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
        )
        .with_for_update()
    )
    if run is None:
        raise YouTubePublishingNotReadyError("Production run not found.")
    if (
        run.status != "approved"
        or run.approved_at is None
        or run.youtube_channel_id is None
        or run.approval_mode not in {"human", "automatic_consent"}
    ):
        raise YouTubePublishingNotReadyError(
            "A channel-bound production run with explicit human approval or current "
            "channel-owner automatic-publishing consent is required."
        )

    channel = await session.scalar(
        select(YouTubeChannel)
        .where(
            YouTubeChannel.id == run.youtube_channel_id,
            YouTubeChannel.organization_id == organization_id,
            YouTubeChannel.status == "active",
        )
        .with_for_update()
    )
    if channel is None:
        raise YouTubePublishingNotReadyError(
            "The production run's connected YouTube channel is unavailable."
        )
    if not channel_can_publish(set(channel.granted_scopes.split())):
        raise YouTubePublishingNotReadyError(
            "The selected channel must grant YouTube upload permission and video-update permission "
            "so Aurora can upload the approved video and apply its selected visibility."
        )

    if run.approval_mode == "human":
        if run.approved_by_user_id is None or run.approval_consent_event_id is not None:
            raise YouTubePublishingNotReadyError(
                "Human approval metadata is incomplete or inconsistent."
            )
    else:
        consent_event = await get_current_automatic_publishing_consent(
            session, channel=channel
        )
        if (
            run.approved_by_user_id is not None
            or run.approval_consent_event_id is None
            or consent_event is None
            or consent_event.id != run.approval_consent_event_id
            or consent_event.actor_user_id != requested_by_user_id
        ):
            raise YouTubePublishingNotReadyError(
                "Current channel-owner automatic-publishing consent is required."
            )

        if publish_at is None:
            if privacy_status != channel.auto_publish_privacy_status:
                raise YouTubePublishingNotReadyError(
                    "Immediate automatic publishing must use the channel's configured "
                    "privacy policy."
                )
        else:
            now = await session.scalar(select(func.clock_timestamp()))
            if now is None:
                raise RuntimeError("Could not obtain the database clock.")
            if (
                publish_at.tzinfo is None
                or publish_at <= now
                or privacy_status != "private"
                or channel.auto_publish_privacy_status != "public"
            ):
                raise YouTubePublishingNotReadyError(
                    "Scheduled automatic publishing requires a future timezone-aware "
                    "publish time, private upload visibility and a public channel policy."
                )

    stages = await session.scalars(
        select(ProductionStage).where(
            ProductionStage.production_run_id == run.id,
            ProductionStage.stage_key.in_(("rendering", "quality_check")),
            ProductionStage.status == "completed",
        )
    )
    stage_by_key = {stage.stage_key: stage for stage in stages.all()}
    rendering = stage_by_key.get("rendering")
    quality = stage_by_key.get("quality_check")
    if rendering is None or quality is None:
        raise YouTubePublishingNotReadyError(
            "Completed rendering and final quality-check stages are required."
        )

    quality_manifest = quality.output_manifest
    if not isinstance(quality_manifest, dict):
        raise YouTubePublishingNotReadyError("The final quality manifest is invalid.")
    quality_report = quality_manifest.get("quality_report")
    contains_synthetic_media = quality_manifest.get("contains_synthetic_media")
    media_manifest_flags = quality_manifest.get("media_manifest_flags", [])
    warning_override = run.approval_warning_override
    warning_override_valid = bool(
        isinstance(warning_override, dict)
        and run.approval_mode == "human"
        and run.approved_by_user_id is not None
        and warning_override.get("accepted_by_user_id") == str(run.approved_by_user_id)
        and isinstance(warning_override.get("reason"), str)
        and len(warning_override["reason"].strip()) >= 10
        and warning_override.get("claim_flags") == quality_manifest.get("claim_flags")
        and warning_override.get("rights_flags") == quality_manifest.get("rights_flags")
        and warning_override.get("publication_blocked")
        is quality_manifest.get("publication_blocked")
        and bool(quality_manifest.get("claim_flags") or quality_manifest.get("rights_flags"))
    )
    if (
        not isinstance(contains_synthetic_media, bool)
        or not isinstance(media_manifest_flags, list)
        or media_manifest_flags
        or not isinstance(quality_report, dict)
        or quality_report.get("is_valid") is not True
        or quality_report.get("issues")
        or (
            not warning_override_valid
            and (
                quality_manifest.get("publication_blocked") is not False
                or quality_manifest.get("claim_flags")
                or quality_manifest.get("rights_flags")
            )
        )
    ):
        raise YouTubePublishingNotReadyError(
            "The final factual, technical or rights quality gate blocks publication."
        )

    rendering_manifest = rendering.output_manifest
    if not isinstance(rendering_manifest, dict):
        raise YouTubePublishingNotReadyError("The render manifest is invalid.")
    thumbnail_snapshot = None
    thumbnail_id = rendering_manifest.get("thumbnail_artifact_id")
    if thumbnail_id is not None:
        try:
            thumbnail_uuid = UUID(thumbnail_id) if isinstance(thumbnail_id, str) else None
        except ValueError:
            thumbnail_uuid = None
        thumbnail = (
            await session.scalar(
                select(MediaArtifact).where(
                    MediaArtifact.id == thumbnail_uuid,
                    MediaArtifact.organization_id == organization_id,
                    MediaArtifact.production_run_id == run.id,
                    MediaArtifact.artifact_kind == "thumbnail",
                )
            )
            if thumbnail_uuid is not None
            else None
        )
        if (
            thumbnail is None
            or thumbnail.mime_type not in {"image/jpeg", "image/png"}
            or not 0 < thumbnail.byte_size <= 50_000_000
            or not isinstance(thumbnail.metadata_json, dict)
            or thumbnail.metadata_json.get("generated_by") != "aurora-thumbnail.v1"
        ):
            raise YouTubePublishingNotReadyError(
                "The rendered custom thumbnail is missing or invalid."
            )
        thumbnail_snapshot = {
            "artifact_id": str(thumbnail.id),
            "storage_backend": thumbnail.storage_backend,
            "storage_key": thumbnail.storage_key,
            "mime_type": thumbnail.mime_type,
            "sha256": thumbnail.sha256,
            "byte_size": thumbnail.byte_size,
            "uploaded": False,
        }
    video_artifact_id = quality_manifest.get("video_artifact_id")
    if (
        not isinstance(video_artifact_id, str)
        or rendering_manifest.get("video_artifact_id") != video_artifact_id
    ):
        raise YouTubePublishingNotReadyError(
            "The quality report does not reference the exact completed render artifact."
        )

    artifact = await session.scalar(
        select(MediaArtifact).where(
            MediaArtifact.id == UUID(video_artifact_id),
            MediaArtifact.organization_id == organization_id,
            MediaArtifact.production_run_id == run.id,
            MediaArtifact.artifact_kind == "video",
        )
    )
    if artifact is None or artifact.mime_type != "video/mp4":
        raise YouTubePublishingNotReadyError(
            "A tenant-bound rendered MP4 artifact is required."
        )
    metadata = artifact.metadata_json
    if (
        not isinstance(metadata, dict)
        or metadata.get("rights_manifest_version") != "1.0.0"
        or metadata.get("attribution_credits") != quality_manifest.get("attribution_credits")
        or metadata.get("source_rights") != quality_manifest.get("source_rights")
        or metadata.get("contains_synthetic_media") is not contains_synthetic_media
    ):
        raise YouTubePublishingNotReadyError(
            "Rendered rights and attribution metadata do not match the verified quality manifest."
        )

    source_rights = quality_manifest.get("source_rights")
    if not isinstance(source_rights, list) or not source_rights:
        raise YouTubePublishingNotReadyError(
            "The verified source-rights manifest is missing."
        )
    for source in source_rights:
        if not isinstance(source, dict):
            raise YouTubePublishingNotReadyError(
                "The verified source-rights manifest contains an invalid entry."
            )
        canonical_url = source.get("canonical_url")
        license_ids = source.get("license_ids")
        license_urls = source.get("license_urls")
        if (
            not isinstance(canonical_url, str)
            or not canonical_url.startswith("https://")
            or not isinstance(source.get("attribution_text"), str)
            or not source["attribution_text"].strip()
        ):
            raise YouTubePublishingNotReadyError(
                "Every research source must have a safe URL and attribution text."
            )
        valid_license_refs = (
            isinstance(license_ids, list)
            and bool(license_ids)
            and isinstance(license_urls, list)
            and len(license_ids) == len(license_urls)
            and all(
                isinstance(license_id, str)
                and bool(license_id.strip())
                and isinstance(license_url, str)
                and license_url.startswith("https://")
                for license_id, license_url in zip(license_ids, license_urls, strict=True)
            )
        )
        if (
            not warning_override_valid
            and (
                source.get("status") != "cleared"
                or source.get("scope") in {None, "unknown"}
                or not valid_license_refs
            )
        ):
            raise YouTubePublishingNotReadyError(
                "Every research source must have cleared, attributable reuse rights."
            )

    final_description = build_youtube_description(
        description,
        quality_manifest.get("attribution_credits"),
        allow_unverified_rights=warning_override_valid,
    )
    normalized_title = title.strip()
    if not normalized_title or len(normalized_title) > 100:
        raise YouTubePublishingNotReadyError("YouTube title must contain 1–100 characters.")
    if len(tags) > 30 or sum(len(tag) for tag in tags) + len(tags) > 500:
        raise YouTubePublishingNotReadyError("YouTube tags exceed the supported metadata limit.")
    if publish_at is not None:
        if publish_at.tzinfo is None or publish_at <= datetime.now(UTC):
            raise YouTubePublishingNotReadyError(
                "Scheduled publication must use a future timezone-aware timestamp."
            )
        if privacy_status != "private":
            raise YouTubePublishingNotReadyError(
                "YouTube scheduled publication must use private visibility."
            )

    existing_run_job = await session.scalar(
        select(YouTubePublishJob)
        .where(YouTubePublishJob.production_run_id == run.id)
        .order_by(YouTubePublishJob.created_at.desc(), YouTubePublishJob.id.desc())
        .limit(1)
    )
    if existing_run_job is not None:
        has_external_progress = (
            existing_run_job.bytes_uploaded > 0
            or existing_run_job.youtube_video_id is not None
            or existing_run_job.upload_session_url_encrypted is not None
        )
        # A claimed job may still have a provider request in flight even when
        # no response has been persisted yet. Do not permit a replacement job
        # until that worker has relinquished its lease.
        replaceable_cancelled_job = (
            existing_run_job.status in {"cancelled", "failed"}
            and not has_external_progress
            and existing_run_job.lease_token is None
        )
        if not replaceable_cancelled_job:
            raise YouTubePublishingConflictError(
                "A prior YouTube publishing job exists for this production run; "
                "active or partially uploaded jobs require reconciliation."
            )

    job = YouTubePublishJob(
        organization_id=organization_id,
        production_run_id=run.id,
        youtube_channel_id=channel.id,
        media_artifact_id=artifact.id,
        requested_by_user_id=requested_by_user_id,
        approved_by_user_id=run.approved_by_user_id,
        approved_at=run.approved_at,
        approval_mode=run.approval_mode,
        approval_consent_event_id=run.approval_consent_event_id,
        idempotency_key=idempotency_key,
        status="queued",
        privacy_status=privacy_status,
        publish_at=publish_at,
        title=normalized_title,
        description=final_description,
        tags=tags,
        metadata_snapshot={
            "attribution_credits": quality_manifest["attribution_credits"],
            "source_rights": source_rights,
            "rights_manifest_version": metadata["rights_manifest_version"],
            "contains_synthetic_media": contains_synthetic_media,
            "quality_report": quality_report,
            "manual_warning_override": warning_override if warning_override_valid else None,
            "approval_mode": run.approval_mode,
            "approval_consent_event_id": (
                str(run.approval_consent_event_id)
                if run.approval_consent_event_id else None
            ),
            "auto_publish_privacy_status": (
                channel.auto_publish_privacy_status
                if run.approval_mode == "automatic_consent" else None
            ),
            "editorial_profile": {
                "channel_profile_id": (
                    str(run.channel_profile_id) if run.channel_profile_id else None
                ),
                "channel_profile_version": run.channel_profile_version,
                "channel_profile_snapshot": run.channel_profile_snapshot,
            },
            "production": {
                "production_run_id": str(run.id),
                "research_run_id": str(run.research_run_id),
                "scheduled_local_date": (
                    run.scheduled_local_date.isoformat()
                    if run.scheduled_local_date else None
                ),
                "scheduled_for": (
                    run.scheduled_for.isoformat() if run.scheduled_for else None
                ),
            },
            "media_artifact": {
                "artifact_id": str(artifact.id),
                "artifact_kind": artifact.artifact_kind,
                "mime_type": artifact.mime_type,
                "storage_backend": artifact.storage_backend,
                "storage_key": artifact.storage_key,
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "metadata": metadata,
            },
            "thumbnail_artifact": thumbnail_snapshot,
            "request_sha256": request_fingerprint,
        },
        storage_backend=artifact.storage_backend,
        storage_key=artifact.storage_key,
        mime_type=artifact.mime_type,
        sha256=artifact.sha256,
        byte_size=artifact.byte_size,
    )
    session.add(job)
    await session.flush()
    session.add(
        YouTubePublishEvent(
            organization_id=organization_id,
            publish_job_id=job.id,
            actor_user_id=requested_by_user_id,
            event_type="publish_intent_created",
            event_metadata={
                "production_run_id": str(run.id),
                "youtube_channel_id": str(channel.id),
                "media_artifact_id": str(artifact.id),
                "channel_profile_id": (
                    str(run.channel_profile_id) if run.channel_profile_id else None
                ),
                "channel_profile_version": run.channel_profile_version,
                "media_sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "privacy_status": privacy_status,
                "publish_at": publish_at.isoformat() if publish_at else None,
                "approval_mode": run.approval_mode,
                "approved_by_user_id": (
                    str(run.approved_by_user_id) if run.approved_by_user_id else None
                ),
                "approval_consent_event_id": (
                    str(run.approval_consent_event_id)
                    if run.approval_consent_event_id else None
                ),
                "approved_at": run.approved_at.isoformat(),
            },
        )
    )
    await session.flush()
    return job


class YouTubePublishLeaseLostError(RuntimeError):
    """A worker no longer owns the durable YouTube publishing lease."""


async def claim_next_youtube_publish_job(
    session_factory,
    *,
    worker_id: str,
    lease_seconds: int,
) -> tuple[UUID, UUID] | None:
    """Claim one due upload/poll job with PostgreSQL SKIP LOCKED semantics."""
    async with session_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("Could not obtain the database clock.")
        job = await session.scalar(
            select(YouTubePublishJob)
            .where(
                YouTubePublishJob.status.in_(
                    ("queued", "initiating", "uploading", "processing")
                )
                | (
                    (YouTubePublishJob.status == "uploaded")
                    & YouTubePublishJob.publish_at.is_not(None)
                    & (YouTubePublishJob.publish_at <= now)
                ),
                YouTubePublishJob.next_attempt_at <= now,
                or_(
                    YouTubePublishJob.lease_expires_at.is_(None),
                    YouTubePublishJob.lease_expires_at <= now,
                ),
            )
            .order_by(
                YouTubePublishJob.next_attempt_at.asc(),
                YouTubePublishJob.created_at.asc(),
                YouTubePublishJob.id.asc(),
            )
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return None
        lease_token = uuid4()
        job.lease_token = lease_token
        job.lease_expires_at = now + timedelta(seconds=lease_seconds)
        session.add(
            YouTubePublishEvent(
                organization_id=job.organization_id,
                publish_job_id=job.id,
                actor_user_id=None,
                event_type="worker_claimed",
                event_metadata={"worker_id": worker_id},
            )
        )
        await session.commit()
        return job.id, lease_token


async def renew_youtube_publish_lease(
    session_factory,
    *,
    job_id: UUID,
    lease_token: UUID,
    lease_seconds: int,
) -> bool:
    async with session_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("Could not obtain the database clock.")
        result = await session.execute(
            update(YouTubePublishJob)
            .where(
                YouTubePublishJob.id == job_id,
                YouTubePublishJob.lease_token == lease_token,
                YouTubePublishJob.lease_expires_at > now,
            )
            .values(lease_expires_at=now + timedelta(seconds=lease_seconds))
            .returning(YouTubePublishJob.id)
        )
        await session.commit()
        return result.scalar_one_or_none() is not None


async def update_youtube_publish_job_under_lease(
    session_factory,
    *,
    job_id: UUID,
    lease_token: UUID,
    values: dict,
    event_type: str,
    event_metadata: dict,
    release_lease: bool = True,
) -> None:
    """Persist a sanitized state transition only while the worker owns its lease."""
    async with session_factory() as session:
        now = await session.scalar(select(func.clock_timestamp()))
        if now is None:
            raise RuntimeError("Could not obtain the database clock.")
        job = await session.scalar(
            select(YouTubePublishJob)
            .where(
                YouTubePublishJob.id == job_id,
                YouTubePublishJob.lease_token == lease_token,
                YouTubePublishJob.lease_expires_at > now,
            )
            .with_for_update()
        )
        if job is None or job.status not in {
            "queued",
            "initiating",
            "uploading",
            "processing",
            "uploaded",
        }:
            # Consent revocation can move a leased job to reconciliation_required
            # while a provider request is in flight. Never let a late worker
            # response overwrite that terminal safety decision.
            raise YouTubePublishLeaseLostError(
                "YouTube publishing worker no longer owns an executable job."
            )
        for field_name, value in values.items():
            if field_name in {"id", "organization_id", "lease_token", "lease_expires_at"}:
                raise ValueError("Publishing worker attempted to update a protected field.")
            setattr(job, field_name, value)
        if release_lease:
            job.lease_token = None
            job.lease_expires_at = None
        session.add(
            YouTubePublishEvent(
                organization_id=job.organization_id,
                publish_job_id=job.id,
                actor_user_id=None,
                event_type=event_type,
                event_metadata=event_metadata,
            )
        )
        await session.commit()
