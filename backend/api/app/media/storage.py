"""Local, tenant-partitioned media object storage for development and single-node use."""

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import UUID

_ALLOWED_EXTENSIONS = {
    "wav",
    "mp3",
    "m4a",
    "png",
    "jpg",
    "webp",
    "mp4",
    "webm",
    "srt",
    "vtt",
}
_KEY_PATTERN = re.compile(
    r"^(?P<organization>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/"
    r"(?P<artifact>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"\.(?P<extension>[a-z0-9]{1,8})$"
)


class MediaStorageError(RuntimeError):
    """Base class for safe media object storage failures."""


class UnsafeMediaStorageKey(MediaStorageError):
    """A storage key is malformed or escapes its tenant directory."""


class MediaObjectTooLarge(MediaStorageError):
    """A media object exceeds the configured storage size limit."""


@dataclass(frozen=True, slots=True)
class StoredMediaObjectInfo:
    storage_key: str
    last_modified: datetime


@dataclass(frozen=True, slots=True)
class StoredMediaObject:
    storage_key: str
    mime_type: str
    sha256: str
    byte_size: int


class MediaStorage(Protocol):
    backend: str

    async def put(
        self,
        *,
        organization_id: UUID,
        artifact_id: UUID,
        extension: str,
        mime_type: str,
        content: bytes,
    ) -> StoredMediaObject:
        """Atomically persist one generated media object."""

    async def get(self, storage_key: str) -> bytes:
        """Read one object by its opaque tenant-scoped key."""

    async def delete(self, storage_key: str) -> None:
        """Delete one object by its opaque tenant-scoped key."""

    async def list_objects(
        self, *, older_than: datetime, limit: int = 500
    ) -> list[StoredMediaObjectInfo]:
        """List bounded, tenant-scoped objects older than the supplied UTC cutoff."""

    async def aclose(self) -> None:
        """Release storage client resources."""


def validate_media_storage_key(storage_key: str) -> None:
    """Reject any object key outside the UUID-partitioned media namespace."""
    if not isinstance(storage_key, str):
        raise UnsafeMediaStorageKey("Media storage key is invalid.")
    match = _KEY_PATTERN.fullmatch(storage_key)
    if match is None or match.group("extension") not in _ALLOWED_EXTENSIONS:
        raise UnsafeMediaStorageKey("Media storage key is invalid.")


class LocalMediaStorage:
    """Atomic filesystem storage; all keys are derived from trusted UUIDs."""

    backend = "local"

    def __init__(self, root: Path, *, max_object_bytes: int = 100_000_000) -> None:
        if max_object_bytes <= 0:
            raise ValueError("max_object_bytes must be positive.")
        self._root = Path(root).expanduser().resolve()
        self._max_object_bytes = max_object_bytes

    def _path_for_key(self, storage_key: str) -> Path:
        validate_media_storage_key(storage_key)
        match = _KEY_PATTERN.fullmatch(storage_key)
        assert match is not None

        organization_directory = self._root / match.group("organization")
        if organization_directory.is_symlink():
            raise UnsafeMediaStorageKey("Tenant media directory must not be a symlink.")

        directory = organization_directory.resolve()
        if directory.parent != self._root:
            raise UnsafeMediaStorageKey("Media storage key escapes its configured root.")

        destination = directory / (
            f"{match.group('artifact')}.{match.group('extension')}"
        )
        if destination.is_symlink():
            raise UnsafeMediaStorageKey("Media object must not be a symlink.")
        return destination

    def _put_sync(
        self,
        *,
        organization_id: UUID,
        artifact_id: UUID,
        extension: str,
        mime_type: str,
        content: bytes,
    ) -> StoredMediaObject:
        if extension not in _ALLOWED_EXTENSIONS:
            raise ValueError("Media extension is not supported.")
        if not isinstance(mime_type, str) or not mime_type.strip() or len(mime_type) > 120:
            raise ValueError("Media MIME type is invalid.")
        if not isinstance(content, bytes) or not content:
            raise ValueError("Media content must be non-empty bytes.")
        if len(content) > self._max_object_bytes:
            raise MediaObjectTooLarge("Media object exceeds the configured size limit.")

        storage_key = f"{organization_id}/{artifact_id}.{extension}"
        destination = self._path_for_key(storage_key)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if destination.parent.is_symlink():
            raise UnsafeMediaStorageKey("Tenant media directory must not be a symlink.")
        if destination.parent.resolve().parent != self._root:
            raise UnsafeMediaStorageKey("Media storage directory escapes its configured root.")
        os.chmod(destination.parent, 0o700)

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                prefix=".aurora-media-",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return StoredMediaObject(
            storage_key=storage_key,
            mime_type=mime_type.strip().lower(),
            sha256=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
        )

    def _get_sync(self, storage_key: str) -> bytes:
        path = self._path_for_key(storage_key)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise FileNotFoundError("Media object does not exist.") from None
        with os.fdopen(descriptor, "rb") as media_file:
            file_stat = os.fstat(media_file.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnsafeMediaStorageKey("Media object must be a regular file.")
            content = media_file.read(self._max_object_bytes + 1)
        if len(content) > self._max_object_bytes:
            raise MediaObjectTooLarge("Stored media object exceeds the configured size limit.")
        return content

    def _delete_sync(self, storage_key: str) -> None:
        path = self._path_for_key(storage_key)
        path.unlink(missing_ok=True)

    async def put(
        self,
        *,
        organization_id: UUID,
        artifact_id: UUID,
        extension: str,
        mime_type: str,
        content: bytes,
    ) -> StoredMediaObject:
        return await asyncio.to_thread(
            self._put_sync,
            organization_id=organization_id,
            artifact_id=artifact_id,
            extension=extension,
            mime_type=mime_type,
            content=content,
        )

    async def get(self, storage_key: str) -> bytes:
        return await asyncio.to_thread(self._get_sync, storage_key)

    async def delete(self, storage_key: str) -> None:
        await asyncio.to_thread(self._delete_sync, storage_key)

    async def list_objects(
        self, *, older_than: datetime, limit: int = 500
    ) -> list[StoredMediaObjectInfo]:
        if older_than.tzinfo is None or limit < 1:
            raise ValueError("A timezone-aware cutoff and positive object limit are required.")

        def scan() -> list[StoredMediaObjectInfo]:
            results: list[StoredMediaObjectInfo] = []
            if not self._root.exists():
                return results
            for tenant_directory in self._root.iterdir():
                if len(results) >= limit:
                    break
                if tenant_directory.is_symlink() or not tenant_directory.is_dir():
                    continue
                tenant_name = tenant_directory.name
                if re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    tenant_name,
                ) is None:
                    continue
                for candidate in tenant_directory.iterdir():
                    if len(results) >= limit:
                        break
                    if candidate.is_symlink() or not candidate.is_file():
                        continue
                    storage_key = f"{tenant_name}/{candidate.name}"
                    try:
                        validate_media_storage_key(storage_key)
                        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
                    except (OSError, UnsafeMediaStorageKey):
                        continue
                    if modified < older_than.astimezone(UTC):
                        results.append(
                            StoredMediaObjectInfo(storage_key=storage_key, last_modified=modified)
                        )
            return results

        return await asyncio.to_thread(scan)

    async def aclose(self) -> None:
        """Local storage owns no persistent network resources."""
