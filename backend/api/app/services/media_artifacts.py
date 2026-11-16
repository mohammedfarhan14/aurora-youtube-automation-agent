"""Lease-aware persistence of media objects and their tenant-scoped metadata."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.media.storage import MediaStorage, StoredMediaObject
from app.models.media_artifact import MediaArtifact
from app.models.production_run import ProductionRun
from app.models.production_stage import ProductionStage

logger = logging.getLogger(__name__)

_ARTIFACT_SLOT_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,119}$")
_ARTIFACT_NAMESPACE = UUID("2a0d3d7c-b4ef-4d55-96f6-fb870e10f2a1")
_MAX_METADATA_BYTES = 16_384
_ALLOWED_MEDIA_TYPES = {
    "audio": {"wav": {"audio/wav", "audio/x-wav"}, "mp3": {"audio/mpeg"}, "m4a": {"audio/mp4"}},
    "image": {"png": {"image/png"}, "jpg": {"image/jpeg"}, "webp": {"image/webp"}},
    "thumbnail": {"png": {"image/png"}, "jpg": {"image/jpeg"}, "webp": {"image/webp"}},
    "video": {"mp4": {"video/mp4"}, "webm": {"video/webm"}},
    "captions": {"srt": {"application/x-subrip", "text/plain"}, "vtt": {"text/vtt"}},
}


class MediaArtifactError(RuntimeError):
    """Base class for media artifact persistence failures."""


class MediaStageLeaseError(MediaArtifactError):
    """The caller no longer owns the active production stage lease."""


class MediaArtifactConflictError(MediaArtifactError):
    """An idempotency slot was already used for different media content."""


class MediaArtifactCompensationError(MediaArtifactError):
    """A failed metadata commit could not clean up its uploaded media object."""


@dataclass(frozen=True, slots=True)
class MediaArtifactResult:
    id: UUID
    organization_id: UUID
    production_run_id: UUID
    production_stage_id: UUID
    artifact_kind: str
    storage_backend: str
    storage_key: str
    mime_type: str
    sha256: str
    byte_size: int
    duration_ms: int | None
    width: int | None
    height: int | None
    metadata_json: dict


def _artifact_id(
    production_stage_id: UUID,
    *,
    attempt_count: int,
    artifact_slot: str,
) -> UUID:
    if attempt_count < 1:
        raise ValueError("attempt_count must be positive.")
    if not isinstance(artifact_slot, str) or not _ARTIFACT_SLOT_PATTERN.fullmatch(artifact_slot):
        raise ValueError("artifact_slot must be a bounded lowercase identifier.")
    return uuid5(
        _ARTIFACT_NAMESPACE,
        f"{production_stage_id}:{attempt_count}:{artifact_slot}",
    )


def _validate_metadata(metadata: dict) -> dict:
    if not isinstance(metadata, dict):
        raise ValueError("Media artifact metadata must be a JSON object.")
    encoded = json.dumps(metadata, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError("Media artifact metadata exceeds the configured size limit.")
    return dict(metadata)


def _snapshot(record: MediaArtifact) -> MediaArtifactResult:
    return MediaArtifactResult(
        id=record.id,
        organization_id=record.organization_id,
        production_run_id=record.production_run_id,
        production_stage_id=record.production_stage_id,
        artifact_kind=record.artifact_kind,
        storage_backend=record.storage_backend,
        storage_key=record.storage_key,
        mime_type=record.mime_type,
        sha256=record.sha256,
        byte_size=record.byte_size,
        duration_ms=record.duration_ms,
        width=record.width,
        height=record.height,
        metadata_json=dict(record.metadata_json),
    )


def _matches_existing(
    record: MediaArtifact,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    artifact_kind: str,
    storage_backend: str,
    extension: str,
    mime_type: str,
    sha256: str,
    byte_size: int,
    duration_ms: int | None,
    width: int | None,
    height: int | None,
    metadata: dict,
) -> bool:
    return (
        record.organization_id == organization_id
        and record.production_run_id == production_run_id
        and record.production_stage_id == production_stage_id
        and record.artifact_kind == artifact_kind
        and record.storage_backend == storage_backend
        and record.storage_key.rsplit(".", 1)[-1] == extension
        and record.mime_type == mime_type
        and record.sha256 == sha256
        and record.byte_size == byte_size
        and record.duration_ms == duration_ms
        and record.width == width
        and record.height == height
        and record.metadata_json == metadata
    )


async def _verify_stored_object(
    storage: MediaStorage,
    artifact: MediaArtifactResult,
) -> None:
    content = await storage.get(artifact.storage_key)
    if (
        len(content) != artifact.byte_size
        or hashlib.sha256(content).hexdigest() != artifact.sha256
    ):
        raise MediaArtifactError("Registered media object failed its integrity check.")


async def _lock_active_stage(
    session: AsyncSession,
    *,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    attempt_count: int,
) -> None:
    stage = await session.scalar(
        select(ProductionStage)
        .join(
            ProductionRun,
            ProductionRun.id == ProductionStage.production_run_id,
        )
        .where(
            ProductionStage.id == production_stage_id,
            ProductionStage.production_run_id == production_run_id,
            ProductionStage.status == "running",
            ProductionStage.lease_token == lease_token,
            ProductionStage.attempt_count == attempt_count,
            ProductionStage.lease_expires_at > func.clock_timestamp(),
            ProductionRun.id == production_run_id,
            ProductionRun.organization_id == organization_id,
            ProductionRun.cancel_requested_at.is_(None),
        )
        .with_for_update(of=ProductionStage)
    )
    if stage is None:
        raise MediaStageLeaseError("The media worker no longer owns this stage lease.")


async def cleanup_orphaned_media_objects(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: MediaStorage,
    grace_period: timedelta = timedelta(hours=24),
    limit: int = 500,
) -> int:
    """Delete stale storage objects that have no committed artifact registry row.

    The grace period protects objects uploaded by workers whose database commit is
    still in flight. Only keys validated by the storage adapter are inventoried;
    the database remains authoritative for whether an object is referenced.
    """
    if grace_period <= timedelta(0) or limit < 1:
        raise ValueError("Orphan cleanup requires a positive grace period and limit.")
    cutoff = datetime.now(UTC) - grace_period
    candidates = await storage.list_objects(older_than=cutoff, limit=limit)
    if not candidates:
        return 0

    candidate_keys = {candidate.storage_key for candidate in candidates}
    async with session_factory() as session:
        registered_keys = set(
            (
                await session.scalars(
                    select(MediaArtifact.storage_key).where(
                        MediaArtifact.storage_backend == storage.backend,
                        MediaArtifact.storage_key.in_(candidate_keys),
                    )
                )
            ).all()
        )

    removed = 0
    for candidate in candidates:
        if candidate.storage_key in registered_keys:
            continue
        try:
            await storage.delete(candidate.storage_key)
        except Exception:
            logger.exception(
                "Orphan media object cleanup failed: backend=%s storage_key=%s",
                storage.backend,
                candidate.storage_key,
            )
            continue
        removed += 1
    return removed


async def store_media_artifact(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    storage: MediaStorage,
    organization_id: UUID,
    production_run_id: UUID,
    production_stage_id: UUID,
    lease_token: UUID,
    attempt_count: int,
    artifact_slot: str,
    artifact_kind: str,
    extension: str,
    mime_type: str,
    content: bytes,
    duration_ms: int | None = None,
    width: int | None = None,
    height: int | None = None,
    metadata: dict | None = None,
) -> MediaArtifactResult:
    """Persist one media object with stage-lease validation and retry-safe identity.

    A stable artifact ID is derived from stage, attempt and slot. The binary object
    uses a separate random storage ID so concurrent retries never overwrite an
    already-registered object. A database failure triggers compensating deletion.
    """

    metadata_value = _validate_metadata({} if metadata is None else metadata)
    artifact_id = _artifact_id(
        production_stage_id,
        attempt_count=attempt_count,
        artifact_slot=artifact_slot,
    )
    if not isinstance(content, bytes) or not content:
        raise ValueError("Media content must be non-empty bytes.")
    if not isinstance(mime_type, str) or not mime_type.strip() or len(mime_type) > 120:
        raise ValueError("Media MIME type is invalid.")
    normalized_mime_type = mime_type.strip().lower()
    allowed_types = _ALLOWED_MEDIA_TYPES.get(artifact_kind, {}).get(extension, set())
    if normalized_mime_type not in allowed_types:
        raise ValueError("Media artifact kind, extension and MIME type do not match.")
    if duration_ms is not None and duration_ms < 0:
        raise ValueError("Media duration cannot be negative.")
    if (width is None) != (height is None) or (
        width is not None and (width <= 0 or height is None or height <= 0)
    ):
        raise ValueError("Media dimensions must be supplied as a positive pair.")
    expected_sha256 = hashlib.sha256(content).hexdigest()
    expected_size = len(content)
    storage_backend = storage.backend
    if storage_backend not in {"local", "s3"}:
        raise ValueError("Media storage backend is not supported.")

    existing_result: MediaArtifactResult | None = None
    async with session_factory() as session:
        async with session.begin():
            await _lock_active_stage(
                session,
                organization_id=organization_id,
                production_run_id=production_run_id,
                production_stage_id=production_stage_id,
                lease_token=lease_token,
                attempt_count=attempt_count,
            )
            existing = await session.scalar(
                select(MediaArtifact).where(
                    MediaArtifact.id == artifact_id,
                    MediaArtifact.organization_id == organization_id,
                )
            )
            if existing is not None:
                if not _matches_existing(
                    existing,
                    organization_id=organization_id,
                    production_run_id=production_run_id,
                    production_stage_id=production_stage_id,
                    artifact_kind=artifact_kind,
                    storage_backend=storage_backend,
                    extension=extension,
                    mime_type=normalized_mime_type,
                    sha256=expected_sha256,
                    byte_size=expected_size,
                    duration_ms=duration_ms,
                    width=width,
                    height=height,
                    metadata=metadata_value,
                ):
                    raise MediaArtifactConflictError(
                        "This stage artifact slot already contains different media content."
                    )
                existing_result = _snapshot(existing)

    if existing_result is not None:
        await _verify_stored_object(storage, existing_result)
        return existing_result

    upload_id = uuid4()
    stored: StoredMediaObject | None = None
    try:
        stored = await storage.put(
            organization_id=organization_id,
            artifact_id=upload_id,
            extension=extension,
            mime_type=normalized_mime_type,
            content=content,
        )
        if (
            stored.sha256 != expected_sha256
            or stored.byte_size != expected_size
            or stored.mime_type != normalized_mime_type
        ):
            raise MediaArtifactError("Storage adapter returned inconsistent object metadata.")

        duplicate_result: MediaArtifactResult | None = None
        async with session_factory() as session:
            async with session.begin():
                await _lock_active_stage(
                    session,
                    organization_id=organization_id,
                    production_run_id=production_run_id,
                    production_stage_id=production_stage_id,
                    lease_token=lease_token,
                    attempt_count=attempt_count,
                )
                existing = await session.scalar(
                    select(MediaArtifact).where(
                        MediaArtifact.id == artifact_id,
                        MediaArtifact.organization_id == organization_id,
                    )
                )
                if existing is not None:
                    if not _matches_existing(
                        existing,
                        organization_id=organization_id,
                        production_run_id=production_run_id,
                        production_stage_id=production_stage_id,
                        artifact_kind=artifact_kind,
                        storage_backend=storage_backend,
                        extension=extension,
                        mime_type=normalized_mime_type,
                        sha256=expected_sha256,
                        byte_size=expected_size,
                        duration_ms=duration_ms,
                        width=width,
                        height=height,
                        metadata=metadata_value,
                    ):
                        raise MediaArtifactConflictError(
                            "This stage artifact slot already contains different media content."
                        )
                    duplicate_result = _snapshot(existing)
                else:
                    record = MediaArtifact(
                        id=artifact_id,
                        organization_id=organization_id,
                        production_run_id=production_run_id,
                        production_stage_id=production_stage_id,
                        artifact_kind=artifact_kind,
                        storage_backend=storage_backend,
                        storage_key=stored.storage_key,
                        mime_type=stored.mime_type,
                        sha256=stored.sha256,
                        byte_size=stored.byte_size,
                        duration_ms=duration_ms,
                        width=width,
                        height=height,
                        metadata_json=metadata_value,
                    )
                    session.add(record)
                    await session.flush()
                    result = _snapshot(record)

        if duplicate_result is not None:
            await storage.delete(stored.storage_key)
            await _verify_stored_object(storage, duplicate_result)
            return duplicate_result
        return result
    except Exception:
        if stored is not None:
            try:
                await storage.delete(stored.storage_key)
            except Exception as cleanup_error:
                logger.exception(
                    "Media artifact compensation failed: organization_id=%s artifact_id=%s",
                    organization_id,
                    artifact_id,
                )
                raise MediaArtifactCompensationError(
                    "Media metadata failed and the unregistered object could not be removed."
                ) from cleanup_error
        raise
