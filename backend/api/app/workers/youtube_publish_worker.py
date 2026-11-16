"""Durable YouTube resumable-upload and processing-state worker."""

import argparse
import asyncio
import hashlib
import ipaddress
import logging
import signal
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from app.core.config import Settings, get_settings
from app.database.engine import dispose_engine, get_session_factory
from app.media.s3_storage import S3CompatibleMediaStorage
from app.media.storage import LocalMediaStorage, MediaStorage, MediaStorageError
from app.services.operations_alerts import enqueue_operations_alert
from app.services.youtube_crypto import (
    decrypt_refresh_token,
    decrypt_upload_session_url,
    encrypt_upload_session_url,
    get_active_encryption_key,
    get_encryption_keys,
)
from app.services.youtube_oauth import YOUTUBE_VIDEO_WRITE_SCOPE, channel_can_publish
from app.services.youtube_publish_validation import validate_youtube_publish_job_snapshot
from app.services.youtube_publishing import (
    YouTubePublishLeaseLostError,
    claim_next_youtube_publish_job,
    renew_youtube_publish_lease,
    update_youtube_publish_job_under_lease,
)
from app.services.youtube_tokens import (
    YouTubeAccessToken,
    YouTubeReauthorizationRequired,
    YouTubeTokenError,
    YouTubeTokenTransientError,
    refresh_youtube_access_token,
)
from app.services.youtube_upload import (
    YouTubeResumableUploadClient,
    YouTubeUploadPermanentError,
    YouTubeUploadSessionExpired,
    YouTubeUploadTransientError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishJobContext:
    id: UUID
    organization_id: UUID
    production_run_id: UUID
    youtube_channel_id: UUID
    status: str
    privacy_status: str
    publish_at: datetime | None
    title: str
    description: str
    tags: list[str]
    storage_backend: str
    storage_key: str
    mime_type: str
    sha256: str
    byte_size: int
    bytes_uploaded: int
    upload_session_url_encrypted: str | None
    upload_session_key_version: int | None
    upload_session_expires_at: datetime | None
    youtube_video_id: str | None
    attempt_count: int
    encrypted_refresh_token: str
    channel_key_version: int
    external_channel_id: str
    granted_scopes: str
    channel_status: str
    contains_synthetic_media: bool
    metadata_snapshot: dict
    thumbnail_artifact: dict | None


async def _load_job_context(
    session_factory,
    *,
    job_id: UUID,
    lease_token: UUID,
) -> PublishJobContext:
    from sqlalchemy import func, select

    from app.models.youtube import YouTubeChannel
    from app.models.youtube_publishing import YouTubePublishJob

    async with session_factory() as session:
        row = await session.execute(
            select(YouTubePublishJob, YouTubeChannel)
            .join(
                YouTubeChannel,
                YouTubeChannel.id == YouTubePublishJob.youtube_channel_id,
            )
            .where(
                YouTubePublishJob.id == job_id,
                YouTubePublishJob.lease_token == lease_token,
                YouTubePublishJob.lease_expires_at > func.clock_timestamp(),
                YouTubeChannel.organization_id == YouTubePublishJob.organization_id,
            )
        )
        result = row.one_or_none()
        if result is None:
            raise YouTubePublishLeaseLostError(
                "Publishing job or connected channel is no longer available."
            )
        job, channel = result
        contains_synthetic_media = job.metadata_snapshot.get("contains_synthetic_media")
        if not isinstance(contains_synthetic_media, bool):
            raise YouTubeUploadPermanentError(
                "The approved synthetic-media disclosure snapshot is invalid."
            )
        thumbnail_artifact = job.metadata_snapshot.get("thumbnail_artifact")
        if thumbnail_artifact is not None and (
            not isinstance(thumbnail_artifact, dict)
            or thumbnail_artifact.get("mime_type") not in {"image/jpeg", "image/png"}
            or not isinstance(thumbnail_artifact.get("storage_key"), str)
            or not isinstance(thumbnail_artifact.get("sha256"), str)
            or not isinstance(thumbnail_artifact.get("byte_size"), int)
            or not 0 < thumbnail_artifact["byte_size"] <= 50_000_000
            or not isinstance(thumbnail_artifact.get("uploaded"), bool)
        ):
            raise YouTubeUploadPermanentError("The approved thumbnail snapshot is invalid.")
        return PublishJobContext(
            id=job.id,
            organization_id=job.organization_id,
            production_run_id=job.production_run_id,
            youtube_channel_id=job.youtube_channel_id,
            status=job.status,
            privacy_status=job.privacy_status,
            publish_at=job.publish_at,
            title=job.title,
            description=job.description,
            tags=list(job.tags),
            storage_backend=job.storage_backend,
            storage_key=job.storage_key,
            mime_type=job.mime_type,
            sha256=job.sha256,
            byte_size=job.byte_size,
            bytes_uploaded=job.bytes_uploaded,
            upload_session_url_encrypted=job.upload_session_url_encrypted,
            upload_session_key_version=job.upload_session_key_version,
            upload_session_expires_at=job.upload_session_expires_at,
            youtube_video_id=job.youtube_video_id,
            attempt_count=job.attempt_count,
            encrypted_refresh_token=channel.encrypted_refresh_token,
            channel_key_version=channel.key_version,
            external_channel_id=channel.channel_id,
            granted_scopes=channel.granted_scopes,
            channel_status=channel.status,
            contains_synthetic_media=contains_synthetic_media,
            metadata_snapshot=dict(job.metadata_snapshot),
            thumbnail_artifact=(dict(thumbnail_artifact) if thumbnail_artifact else None),
        )


async def _refresh_channel_access_token(
    *,
    job_id: UUID,
    lease_token: UUID,
    session_factory,
    upload_client: YouTubeResumableUploadClient,
    settings: Settings,
    context: PublishJobContext,
) -> YouTubeAccessToken:
    """Refresh via the worker's allowlisted proxy and persist any rotated grant."""
    await validate_youtube_publish_job_snapshot(
        session_factory, job_id=job_id, lease_token=lease_token
    )
    if not hasattr(upload_client, "http_client"):
        # Compatibility for existing isolated worker tests using a minimal fake client.
        refresh_token = decrypt_refresh_token(
            context.encrypted_refresh_token,
            get_encryption_keys(settings),
            context.organization_id,
            context.external_channel_id,
            context.channel_key_version,
        )
        value = await upload_client.refresh_access_token(refresh_token)
        return YouTubeAccessToken(
            value=value,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    from sqlalchemy import func, select

    from app.models.youtube import YouTubeChannel
    from app.models.youtube_publishing import YouTubePublishJob

    async with session_factory() as session:
        async with session.begin():
            job = await session.scalar(
                select(YouTubePublishJob).where(
                    YouTubePublishJob.id == job_id,
                    YouTubePublishJob.lease_token == lease_token,
                    YouTubePublishJob.lease_expires_at > func.clock_timestamp(),
                )
            )
            if job is None:
                raise YouTubePublishLeaseLostError(
                    "YouTube publishing worker no longer owns this job."
                )
            channel = await session.scalar(
                select(YouTubeChannel)
                .where(
                    YouTubeChannel.id == context.youtube_channel_id,
                    YouTubeChannel.organization_id == context.organization_id,
                )
                .with_for_update()
            )
            if channel is None:
                raise YouTubeUploadPermanentError(
                    "The connected YouTube channel is unavailable."
                )
            if channel.status != "active" or not channel_can_publish(
                set((channel.granted_scopes or "").split())
            ):
                raise YouTubeReauthorizationRequired(
                    "The connected channel is inactive or needs renewed YouTube publishing "
                    "permissions for upload and visibility updates."
                )
            token = await refresh_youtube_access_token(
                settings,
                channel,
                client=upload_client.http_client,
                required_scope=YOUTUBE_VIDEO_WRITE_SCOPE,
            )
        return token


def _build_storage(settings: Settings) -> MediaStorage:
    if settings.media_storage_backend == "local":
        return LocalMediaStorage(
            settings.media_storage_root,
            max_object_bytes=settings.media_storage_max_object_bytes,
        )

    endpoint = settings.media_s3_endpoint_url or ""
    hostname = urlsplit(endpoint).hostname or ""
    try:
        endpoint_ip = ipaddress.ip_address(hostname)
    except ValueError:
        endpoint_ip = None

    if endpoint_ip is not None:
        if not endpoint_ip.is_private and not endpoint_ip.is_loopback:
            raise RuntimeError(
                "Remote S3 IP-literal endpoints are not allowed for YouTube publishing."
            )
        proxy_url = None
    elif (
        hostname in {"localhost", "minio", "s3"}
        or "." not in hostname
        or hostname.endswith((".local", ".internal", ".svc"))
    ):
        proxy_url = None
    elif hostname.endswith(".r2.cloudflarestorage.com") and settings.media_s3_proxy_url:
        proxy_url = settings.media_s3_proxy_url
    else:
        raise RuntimeError(
            "Remote S3 egress requires a Cloudflare R2 endpoint and configured media proxy. "
            "Use local storage or a private S3-compatible endpoint on the media-data network."
        )

    return S3CompatibleMediaStorage(
        endpoint_url=endpoint,
        bucket=settings.media_s3_bucket or "",
        region=settings.media_s3_region,
        access_key_id=(
            settings.media_s3_access_key_id.get_secret_value()
            if settings.media_s3_access_key_id is not None
            else ""
        ),
        secret_access_key=(
            settings.media_s3_secret_access_key.get_secret_value()
            if settings.media_s3_secret_access_key is not None
            else ""
        ),
        session_token=(
            settings.media_s3_session_token.get_secret_value()
            if settings.media_s3_session_token is not None
            else None
        ),
        max_object_bytes=settings.media_storage_max_object_bytes,
        timeout_seconds=settings.media_s3_timeout_seconds,
        proxy_url=proxy_url,
    )


async def _persist(
    session_factory,
    context: PublishJobContext,
    lease_token: UUID,
    *,
    values: dict,
    event_type: str,
    event_metadata: dict,
    release_lease: bool = False,
) -> None:
    await update_youtube_publish_job_under_lease(
        session_factory,
        job_id=context.id,
        lease_token=lease_token,
        values=values,
        event_type=event_type,
        event_metadata=event_metadata,
        release_lease=release_lease,
    )


async def _poll_processing_status(
    *,
    context: PublishJobContext,
    lease_token: UUID,
    session_factory,
    storage: MediaStorage,
    upload_client: YouTubeResumableUploadClient,
    access_token: str,
    settings: Settings,
) -> None:
    await validate_youtube_publish_job_snapshot(
        session_factory, job_id=context.id, lease_token=lease_token
    )
    status = await upload_client.get_video_processing_status(
        access_token=access_token,
        video_id=context.youtube_video_id or "",
    )
    if status.processing_status in {"failed", "terminated"} or status.upload_status in {
        "failed",
        "rejected",
    }:
        raise YouTubeUploadPermanentError(
            "YouTube rejected or failed to process the uploaded video."
        )

    thumbnail = context.thumbnail_artifact
    thumbnail_warning: str | None = None
    if (
        status.upload_status == "processed"
        and status.processing_status == "succeeded"
        and thumbnail is not None
        and not thumbnail["uploaded"]
        and not thumbnail.get("upload_error")
    ):
        await validate_youtube_publish_job_snapshot(
            session_factory, job_id=context.id, lease_token=lease_token
        )
        image = await storage.get(thumbnail["storage_key"])
        if (
            len(image) != thumbnail["byte_size"]
            or hashlib.sha256(image).hexdigest() != thumbnail["sha256"]
        ):
            raise YouTubeUploadPermanentError(
                "The custom thumbnail no longer matches its approved artifact."
            )
        try:
            await upload_client.upload_thumbnail(
                access_token=access_token,
                video_id=context.youtube_video_id or "",
                content=image,
                mime_type=thumbnail["mime_type"],
                processing_status=status.processing_status,
            )
        except YouTubeUploadPermanentError as exc:
            # A custom thumbnail is an optional enhancement. YouTube may reject
            # it for channel/API-project policy even when the video itself was
            # accepted. Persist the warning and continue reconciling the video
            # so a thumbnail policy failure never strands a valid upload.
            thumbnail_warning = str(exc)[:500]
            updated_snapshot = dict(context.metadata_snapshot)
            updated_thumbnail = dict(thumbnail)
            updated_thumbnail["upload_error"] = thumbnail_warning
            updated_snapshot["thumbnail_artifact"] = updated_thumbnail
            await _persist(
                session_factory,
                context,
                lease_token,
                values={"metadata_snapshot": updated_snapshot},
                event_type="thumbnail_upload_failed",
                event_metadata={
                    "artifact_id": thumbnail["artifact_id"],
                    "error": thumbnail_warning,
                },
                release_lease=False,
            )
            context = replace(
                context,
                metadata_snapshot=updated_snapshot,
                thumbnail_artifact=updated_thumbnail,
            )
        else:
            updated_snapshot = dict(context.metadata_snapshot)
            updated_thumbnail = dict(thumbnail)
            updated_thumbnail["uploaded"] = True
            updated_snapshot["thumbnail_artifact"] = updated_thumbnail
            await _persist(
                session_factory,
                context,
                lease_token,
                values={
                    "metadata_snapshot": updated_snapshot,
                    "next_attempt_at": datetime.now(UTC)
                    + timedelta(seconds=settings.youtube_publish_poll_interval_seconds),
                },
                event_type="thumbnail_uploaded",
                event_metadata={"artifact_id": thumbnail["artifact_id"]},
                release_lease=False,
            )
            context = replace(
                context,
                metadata_snapshot=updated_snapshot,
                thumbnail_artifact=updated_thumbnail,
            )

    now = datetime.now(UTC)
    values = {
        "youtube_processing_status": status.processing_status,
        "next_attempt_at": now + timedelta(
            seconds=settings.youtube_publish_poll_interval_seconds
        ),
    }
    if thumbnail_warning:
        values.update(
            {
                "failure_code": "youtube_thumbnail_upload_failed",
                "failure_message": (
                    "The video was accepted by YouTube, but its custom thumbnail "
                    f"could not be applied: {thumbnail_warning}"
                ),
            }
        )
    event_type = "processing_status_observed"
    event_metadata = {
        "upload_status": status.upload_status,
        "processing_status": status.processing_status,
        "privacy_status": status.privacy_status,
    }
    if thumbnail_warning:
        event_metadata["thumbnail_warning"] = thumbnail_warning

    if (
        status.upload_status == "processed"
        and status.processing_status == "succeeded"
    ):
        if context.publish_at is not None:
            if status.privacy_status == "public" and context.publish_at <= now:
                values["status"] = "published"
                values["published_at"] = (
                    datetime.fromisoformat(status.published_at.replace("Z", "+00:00"))
                    if status.published_at
                    else now
                )
                event_type = "video_published"
            elif status.privacy_status == "private":
                values["status"] = "uploaded"
                values["next_attempt_at"] = max(
                    context.publish_at,
                    now + timedelta(
                        seconds=settings.youtube_publish_poll_interval_seconds
                    ),
                )
                event_type = (
                    "video_scheduled_private"
                    if context.publish_at > now
                    else "scheduled_publication_pending"
                )
            else:
                values.update(
                    {
                        "status": "reconciliation_required",
                        "failure_code": "privacy_status_mismatch",
                        "failure_message": (
                            "YouTube visibility differs from the approved scheduled "
                            "publication policy. Manual reconciliation is required."
                        ),
                        "next_attempt_at": now + timedelta(days=3650),
                    }
                )
                event_type = "privacy_status_mismatch"
        elif (
            status.privacy_status == "private"
            and context.privacy_status in {"public", "unlisted"}
        ):
            await validate_youtube_publish_job_snapshot(
                session_factory, job_id=context.id, lease_token=lease_token
            )
            observed_privacy = await upload_client.update_video_privacy_status(
                access_token=access_token,
                video_id=context.youtube_video_id or "",
                privacy_status=context.privacy_status,
            )
            event_metadata["requested_privacy_status"] = context.privacy_status
            event_metadata["observed_privacy_status"] = observed_privacy
            if observed_privacy == context.privacy_status:
                values["status"] = "published"
                values["published_at"] = now
                event_type = "video_published"
            else:
                values["status"] = "processing"
                values["next_attempt_at"] = now + timedelta(
                    seconds=settings.youtube_publish_poll_interval_seconds
                )
                event_type = "visibility_update_pending"
        elif status.privacy_status == context.privacy_status:
            if context.privacy_status == "private":
                values["status"] = "uploaded"
                values["next_attempt_at"] = now
                event_type = "video_uploaded_private"
            else:
                values["status"] = "published"
                values["published_at"] = (
                    datetime.fromisoformat(status.published_at.replace("Z", "+00:00"))
                    if status.published_at
                    else now
                )
                event_type = "video_published"
        else:
            values.update(
                {
                    "status": "reconciliation_required",
                    "failure_code": "privacy_status_mismatch",
                    "failure_message": (
                        "YouTube visibility differs from the approved publishing policy. "
                        "Manual reconciliation is required."
                    ),
                    "next_attempt_at": now + timedelta(days=3650),
                }
            )
            event_type = "privacy_status_mismatch"
    else:
        values["status"] = "processing"

    await _persist(
        session_factory,
        context,
        lease_token,
        values=values,
        event_type=event_type,
        event_metadata=event_metadata,
        release_lease=True,
    )
    if values.get("status") == "reconciliation_required":
        await enqueue_operations_alert(
            session_factory,
            organization_id=context.organization_id,
            subject="Aurora Studio: YouTube visibility needs reconciliation",
            body=(
                "YouTube's observed visibility does not match the approved publication policy.\n\n"
                f"Publishing job ID: {context.id}\n"
                f"Production run ID: {context.production_run_id}\n"
                f"Failure code: {values.get('failure_code', 'privacy_status_mismatch')}\n\n"
                "Inspect the video in YouTube Studio and reconcile its visibility "
                "before attempting another upload."
            ),
            settings=settings,
        )


async def process_claimed_youtube_publish_job(
    *,
    job_id: UUID,
    lease_token: UUID,
    worker_id: str,
    session_factory,
    storage: MediaStorage,
    upload_client: YouTubeResumableUploadClient,
    settings: Settings,
) -> None:
    """Resume one job; all progress is persisted before the lease is released."""
    context = await _load_job_context(
        session_factory,
        job_id=job_id,
        lease_token=lease_token,
    )
    if context.channel_status != "active" or not channel_can_publish(
        set(context.granted_scopes.split())
    ):
        raise YouTubeReauthorizationRequired(
            "The connected channel is disconnected or needs renewed YouTube publishing "
            "permissions for upload and visibility updates."
        )
    if not context.encrypted_refresh_token:
        raise YouTubeUploadPermanentError(
            "The connected channel has no stored refresh token."
        )

    access_token = await _refresh_channel_access_token(
        job_id=job_id,
        lease_token=lease_token,
        session_factory=session_factory,
        upload_client=upload_client,
        settings=settings,
        context=context,
    )

    if context.status in {"queued", "initiating", "uploading"}:
        content = await storage.get(context.storage_key)
        if len(content) != context.byte_size:
            raise YouTubeUploadPermanentError(
                "The rendered video size no longer matches the approved artifact."
            )
        if hashlib.sha256(content).hexdigest() != context.sha256:
            raise YouTubeUploadPermanentError(
                "The rendered video checksum no longer matches the approved artifact."
            )
        if context.mime_type != "video/mp4":
            raise YouTubeUploadPermanentError(
                "Only the verified MP4 render artifact can be uploaded."
            )

        session_url: str
        session_key_version: int
        if context.upload_session_url_encrypted is None:
            if (
                context.status in {"initiating", "uploading"}
                or context.bytes_uploaded > 0
                or context.youtube_video_id is not None
            ):
                raise YouTubeUploadSessionExpired(
                    "Upload progress exists but its resumable session is missing."
                )
            # Persist an initiation marker before the non-idempotent provider
            # request. If the response is lost or the worker crashes, do not
            # blindly create a second resumable upload session.
            await _persist(
                session_factory,
                context,
                lease_token,
                values={"status": "initiating"},
                event_type="upload_initiation_started",
                event_metadata={"provider": "youtube"},
            )
            await validate_youtube_publish_job_snapshot(
                session_factory, job_id=job_id, lease_token=lease_token
            )
            session_url = await upload_client.initiate_upload(
                access_token=access_token.value,
                title=context.title,
                description=context.description,
                tags=context.tags,
                # Stage every video privately; apply approved public/unlisted visibility
                # only after YouTube confirms successful processing.
                privacy_status="private",
                publish_at=context.publish_at.isoformat().replace("+00:00", "Z")
                if context.publish_at
                else None,
                mime_type=context.mime_type,
                total_bytes=context.byte_size,
                contains_synthetic_media=context.contains_synthetic_media,
            )
            session_key_version, active_key = get_active_encryption_key(settings)
            encrypted_session = encrypt_upload_session_url(
                session_url,
                active_key,
                context.organization_id,
                context.id,
                key_version=session_key_version,
            )
            await _persist(
                session_factory,
                context,
                lease_token,
                values={
                    "status": "uploading",
                    "upload_session_url_encrypted": encrypted_session,
                    "upload_session_key_version": session_key_version,
                    "upload_session_expires_at": datetime.now(UTC)
                    + timedelta(hours=24),
                },
                event_type="upload_session_created",
                event_metadata={"session_host": "www.googleapis.com"},
            )
            server_offset = 0
        else:
            if context.upload_session_key_version is None:
                raise YouTubeUploadPermanentError(
                    "Stored upload session encryption metadata is incomplete."
                )
            session_key_version = context.upload_session_key_version
            keys = get_encryption_keys(settings)
            session_url = decrypt_upload_session_url(
                context.upload_session_url_encrypted,
                keys,
                context.organization_id,
                context.id,
                session_key_version,
            )
            await validate_youtube_publish_job_snapshot(
                session_factory, job_id=job_id, lease_token=lease_token
            )
            reconciled = await upload_client.query_upload_status(
                access_token=access_token.value,
                session_url=session_url,
                total_bytes=context.byte_size,
            )
            if reconciled.video_id is not None:
                await _persist(
                    session_factory,
                    context,
                    lease_token,
                    values={
                        "status": "processing",
                        "youtube_video_id": reconciled.video_id,
                        "youtube_processing_status": "processing",
                        "bytes_uploaded": context.byte_size,
                        "upload_session_url_encrypted": None,
                        "upload_session_key_version": None,
                        "upload_session_expires_at": None,
                        "next_attempt_at": datetime.now(UTC)
                        + timedelta(
                            seconds=settings.youtube_publish_poll_interval_seconds
                        ),
                    },
                    event_type="upload_completion_reconciled",
                    event_metadata={"video_id": reconciled.video_id},
                )
                updated_context = await _load_job_context(
                    session_factory,
                    job_id=job_id,
                    lease_token=lease_token,
                )
                await _poll_processing_status(
                    context=updated_context,
                    lease_token=lease_token,
                    session_factory=session_factory,
                    storage=storage,
                    upload_client=upload_client,
                    access_token=access_token.value,
                    settings=settings,
                )
                return
            server_offset = reconciled.bytes_received
            if server_offset != context.bytes_uploaded:
                await _persist(
                    session_factory,
                    context,
                    lease_token,
                    values={"bytes_uploaded": server_offset},
                    event_type="upload_offset_reconciled",
                    event_metadata={"bytes_uploaded": server_offset},
                )

        while server_offset < context.byte_size:
            if not await renew_youtube_publish_lease(
                session_factory,
                job_id=job_id,
                lease_token=lease_token,
                lease_seconds=settings.youtube_publish_lease_seconds,
            ):
                raise YouTubePublishLeaseLostError(
                    "YouTube upload lease was lost while transferring the video."
                )
            await validate_youtube_publish_job_snapshot(
                session_factory, job_id=job_id, lease_token=lease_token
            )
            if access_token.expires_at <= datetime.now(UTC) + timedelta(
                seconds=settings.youtube_upload_timeout_seconds + 30
            ):
                context = await _load_job_context(
                    session_factory, job_id=job_id, lease_token=lease_token
                )
                access_token = await _refresh_channel_access_token(
                    job_id=job_id,
                    lease_token=lease_token,
                    session_factory=session_factory,
                    storage=storage,
                    upload_client=upload_client,
                    settings=settings,
                    context=context,
                )
            end = min(
                server_offset + settings.youtube_upload_chunk_size_bytes,
                context.byte_size,
            )
            chunk = content[server_offset:end]
            result = await upload_client.upload_chunk(
                access_token=access_token.value,
                session_url=session_url,
                content=chunk,
                start=server_offset,
                total_bytes=context.byte_size,
            )
            if result.video_id is not None:
                await _persist(
                    session_factory,
                    context,
                    lease_token,
                    values={
                        "status": "processing",
                        "youtube_video_id": result.video_id,
                        "youtube_processing_status": "processing",
                        "bytes_uploaded": context.byte_size,
                        "upload_session_url_encrypted": None,
                        "upload_session_key_version": None,
                        "upload_session_expires_at": None,
                        "next_attempt_at": datetime.now(UTC)
                        + timedelta(
                            seconds=settings.youtube_publish_poll_interval_seconds
                        ),
                    },
                    event_type="upload_completed",
                    event_metadata={
                        "video_id": result.video_id,
                        "bytes_uploaded": context.byte_size,
                    },
                )
                updated_context = await _load_job_context(
                    session_factory,
                    job_id=job_id,
                    lease_token=lease_token,
                )
                await _poll_processing_status(
                    context=updated_context,
                    lease_token=lease_token,
                    session_factory=session_factory,
                    storage=storage,
                    upload_client=upload_client,
                    access_token=access_token.value,
                    settings=settings,
                )
                return
            if result.bytes_received <= server_offset:
                raise YouTubeUploadTransientError(
                    "YouTube did not acknowledge progress for the uploaded chunk."
                )
            server_offset = result.bytes_received
            await _persist(
                session_factory,
                context,
                lease_token,
                values={
                    "status": "uploading",
                    "bytes_uploaded": server_offset,
                },
                event_type="upload_progress",
                event_metadata={
                    "bytes_uploaded": server_offset,
                    "byte_size": context.byte_size,
                },
            )

        await validate_youtube_publish_job_snapshot(
            session_factory, job_id=job_id, lease_token=lease_token
        )
        reconciled = await upload_client.query_upload_status(
            access_token=access_token.value,
            session_url=session_url,
            total_bytes=context.byte_size,
        )
        if reconciled.video_id is None:
            raise YouTubeUploadTransientError(
                "YouTube received all video bytes but has not returned the video resource yet."
            )
        await _persist(
            session_factory,
            context,
            lease_token,
            values={
                "status": "processing",
                "youtube_video_id": reconciled.video_id,
                "youtube_processing_status": "processing",
                "bytes_uploaded": context.byte_size,
                "upload_session_url_encrypted": None,
                "upload_session_key_version": None,
                "upload_session_expires_at": None,
                "next_attempt_at": datetime.now(UTC)
                + timedelta(seconds=settings.youtube_publish_poll_interval_seconds),
            },
            event_type="upload_completion_reconciled",
            event_metadata={"video_id": reconciled.video_id},
        )
        updated_context = await _load_job_context(
            session_factory,
            job_id=job_id,
            lease_token=lease_token,
        )
        await _poll_processing_status(
            context=updated_context,
            lease_token=lease_token,
            session_factory=session_factory,
            storage=storage,
            upload_client=upload_client,
            access_token=access_token.value,
            settings=settings,
        )
        return

    if context.youtube_video_id is None:
        raise YouTubeUploadPermanentError(
            "The job has no YouTube video ID to reconcile."
        )
    await _poll_processing_status(
        context=context,
        lease_token=lease_token,
        session_factory=session_factory,
        storage=storage,
        upload_client=upload_client,
        access_token=access_token.value,
        settings=settings,
    )


async def _handle_job_error(
    *,
    job_id: UUID,
    lease_token: UUID,
    worker_id: str,
    session_factory,
    settings: Settings,
    error: Exception,
) -> None:
    try:
        context = await _load_job_context(
            session_factory,
            job_id=job_id,
            lease_token=lease_token,
        )
    except YouTubePublishLeaseLostError:
        return

    now = datetime.now(UTC)
    if isinstance(error, YouTubeUploadSessionExpired):
        values = {
            "status": "reconciliation_required",
            "failure_code": "upload_session_expired",
            "failure_message": (
                "The resumable upload session expired before completion. "
                "Manual reconciliation is required to avoid duplicate videos."
            ),
            "upload_session_url_encrypted": None,
            "upload_session_key_version": None,
            "upload_session_expires_at": None,
            "next_attempt_at": now + timedelta(days=3650),
        }
        event_type = "upload_reconciliation_required"
        event_metadata = {"worker_id": worker_id, "reason": "session_expired"}
    elif (
        isinstance(error, YouTubeUploadPermanentError)
        or (
            isinstance(error, YouTubeTokenError)
            and not isinstance(error, YouTubeTokenTransientError)
        )
        or isinstance(error, (ValueError, FileNotFoundError, MediaStorageError))
    ):
        needs_reconciliation = (
            context.status == "initiating"
            or context.bytes_uploaded > 0
            or context.youtube_video_id is not None
            or context.upload_session_url_encrypted is not None
        )
        token_error = (
            isinstance(error, YouTubeTokenError)
            and not isinstance(error, YouTubeTokenTransientError)
        )
        token_reauthorization_error = isinstance(
            error, YouTubeReauthorizationRequired
        )
        values = {
            "status": "reconciliation_required" if needs_reconciliation else "failed",
            "failure_code": (
                "youtube_reauthorization_reconciliation_required"
                if needs_reconciliation and token_reauthorization_error
                else "youtube_upload_reconciliation_required"
                if needs_reconciliation
                else "youtube_reauthorization_required"
                if token_reauthorization_error
                else "youtube_token_error"
                if token_error
                else "youtube_upload_rejected"
            ),
            "failure_message": (
                str(error)[:1000]
                if isinstance(error, (YouTubeUploadPermanentError, YouTubeTokenError))
                else "The approved video artifact or stored credentials could not be used."
            ),
            "next_attempt_at": now + timedelta(days=3650),
        }
        if not needs_reconciliation:
            values.update(
                {
                    "upload_session_url_encrypted": None,
                    "upload_session_key_version": None,
                    "upload_session_expires_at": None,
                }
            )
        event_type = (
            "upload_reconciliation_required"
            if needs_reconciliation
            else "upload_failed"
        )
        event_metadata = {
            "worker_id": worker_id,
            "error_type": type(error).__name__,
        }
    else:
        attempts = context.attempt_count + 1
        retry_delay = min(
            settings.youtube_publish_retry_base_seconds * (2 ** (attempts - 1)),
            3600,
        )
        exhausted = attempts >= settings.youtube_publish_max_attempts
        has_partial_upload = (
            context.status == "initiating"
            or context.bytes_uploaded > 0
            or context.youtube_video_id is not None
            or context.upload_session_url_encrypted is not None
        )
        terminal_status = (
            "reconciliation_required"
            if context.status == "initiating" or (exhausted and has_partial_upload)
            else "failed"
            if exhausted
            else (
                "processing"
                if context.youtube_video_id is not None
                else "uploading"
                if context.upload_session_url_encrypted is not None
                else "queued"
            )
        )
        values = {
            "attempt_count": attempts,
            "status": terminal_status,
            "failure_code": (
                "youtube_upload_reconciliation_required"
                if context.status == "initiating" or (exhausted and has_partial_upload)
                else "youtube_upload_transient"
            ),
            "failure_message": (
                "The provider may have created an upload session but its response "
                "was not durably recorded; manual reconciliation is required."
                if has_partial_upload and context.status == "initiating"
                else (
                    "The YouTube upload request failed temporarily."
                    if not exhausted
                    else (
                        "The upload retry limit was reached after partial progress; "
                        "manual reconciliation is required."
                        if has_partial_upload
                        else "The YouTube upload exceeded its retry limit."
                    )
                )
            ),
            "next_attempt_at": now + timedelta(seconds=retry_delay),
        }
        event_type = (
            "upload_retry_scheduled"
            if not exhausted and context.status != "initiating"
            else "upload_reconciliation_required"
            if has_partial_upload
            else "upload_retry_exhausted"
        )
        event_metadata = {
            "worker_id": worker_id,
            "attempt_count": attempts,
            "retry_delay_seconds": retry_delay if not exhausted else None,
            "error_type": type(error).__name__,
        }

    try:
        await _persist(
            session_factory,
            context,
            lease_token,
            values=values,
            event_type=event_type,
            event_metadata=event_metadata,
            release_lease=True,
        )
    except YouTubePublishLeaseLostError:
        return

    if values.get("status") in {"failed", "reconciliation_required"}:
        await enqueue_operations_alert(
            session_factory,
            organization_id=context.organization_id,
            subject="Aurora Studio: YouTube publishing requires attention",
            body=(
                "A YouTube publishing job reached a terminal failure or requires "
                "manual reconciliation.\n\n"
                f"Publishing job ID: {context.id}\n"
                f"Production run ID: {context.production_run_id}\n"
                f"Failure code: {values.get('failure_code', 'unknown')}\n"
                f"Status: {values['status']}\n\n"
                "Inspect the publishing event history and YouTube channel connection "
                "before retrying. Do not create a replacement upload while the outcome "
                "of a partial upload is uncertain."
            ),
            settings=settings,
        )


async def run_youtube_publish_worker(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
    upload_client: YouTubeResumableUploadClient | None = None,
    storage: MediaStorage | None = None,
) -> None:
    if max_runtime_seconds <= 0:
        raise ValueError("Maximum worker runtime must be positive.")
    settings = get_settings()
    if not settings.youtube_publish_worker_enabled:
        logger.info("YouTube publishing worker is disabled; no jobs were claimed.")
        return

    owns_client = upload_client is None
    owns_storage = storage is None
    storage = storage or _build_storage(settings)
    upload_client = upload_client or YouTubeResumableUploadClient(
        proxy_url=settings.youtube_egress_proxy_url or "",
        client_id=settings.youtube_client_id or "",
        client_secret=(
            settings.youtube_client_secret.get_secret_value()
            if settings.youtube_client_secret is not None
            else ""
        ),
        timeout_seconds=settings.youtube_upload_timeout_seconds,
    )
    session_factory = get_session_factory()
    stop_event = stop_event or asyncio.Event()
    deadline = time.monotonic() + max_runtime_seconds if drain else None
    worker_id = f"youtube-publish-worker-{uuid4()}"
    logger.info(
        "YouTube publishing worker started: mode=%s",
        "drain" if drain else "continuous",
    )

    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                logger.info("YouTube publishing worker reached its runtime limit.")
                break
            claim = await claim_next_youtube_publish_job(
                session_factory,
                worker_id=worker_id,
                lease_seconds=settings.youtube_publish_lease_seconds,
            )
            if claim is None:
                if drain:
                    logger.info("YouTube publishing worker drain complete.")
                    break
                logger.info(
                    "YouTube publishing worker heartbeat: worker_id=%s",
                    worker_id,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=settings.youtube_publish_poll_interval_seconds,
                    )
                except TimeoutError:
                    pass
                continue

            job_id, lease_token = claim
            try:
                await process_claimed_youtube_publish_job(
                    job_id=job_id,
                    lease_token=lease_token,
                    worker_id=worker_id,
                    session_factory=session_factory,
                    storage=storage,
                    upload_client=upload_client,
                    settings=settings,
                )
            except asyncio.CancelledError:
                raise
            except YouTubePublishLeaseLostError:
                logger.warning("YouTube publishing lease lost for job_id=%s", job_id)
            except Exception as exc:
                logger.error(
                    "YouTube publishing job failed: job_id=%s error_type=%s",
                    job_id,
                    type(exc).__name__,
                )
                await _handle_job_error(
                    job_id=job_id,
                    lease_token=lease_token,
                    worker_id=worker_id,
                    session_factory=session_factory,
                    settings=settings,
                    error=exc,
                )
            if drain:
                continue
    finally:
        if owns_client:
            await upload_client.aclose()
        if owns_storage:
            await storage.aclose()
        logger.info("YouTube publishing worker stopped.")


async def run_worker_with_cleanup(
    *,
    drain: bool = False,
    max_runtime_seconds: int = 3600,
    stop_event: asyncio.Event | None = None,
) -> None:
    try:
        await run_youtube_publish_worker(
            drain=drain,
            max_runtime_seconds=max_runtime_seconds,
            stop_event=stop_event,
        )
    finally:
        await dispose_engine()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aurora Studio YouTube publishing worker.")
    parser.add_argument(
        "--drain",
        action="store_true",
        help="Process due YouTube publishing jobs and exit.",
    )
    parser.add_argument("--max-runtime-seconds", type=int, default=3600)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if args.max_runtime_seconds <= 0:
        raise SystemExit("--max-runtime-seconds must be greater than zero.")
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    async def run() -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:
                pass
        await run_worker_with_cleanup(
            drain=args.drain,
            max_runtime_seconds=args.max_runtime_seconds,
            stop_event=stop_event,
        )

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        logger.info("YouTube publishing worker interrupted.")


if __name__ == "__main__":
    main()
