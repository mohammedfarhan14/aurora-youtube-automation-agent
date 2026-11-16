import os
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from app.media.storage import (
    LocalMediaStorage,
    MediaObjectTooLarge,
    UnsafeMediaStorageKey,
)


@pytest.mark.anyio
async def test_local_media_storage_round_trips_tenant_scoped_object(tmp_path):
    organization_id = uuid4()
    artifact_id = uuid4()
    content = b"synthetic media bytes"
    storage = LocalMediaStorage(tmp_path / "media")

    stored = await storage.put(
        organization_id=organization_id,
        artifact_id=artifact_id,
        extension="wav",
        mime_type="audio/wav",
        content=content,
    )

    assert stored.storage_key == f"{organization_id}/{artifact_id}.wav"
    assert stored.sha256 == sha256(content).hexdigest()
    assert stored.byte_size == len(content)
    assert await storage.get(stored.storage_key) == content

    await storage.delete(stored.storage_key)
    with pytest.raises(FileNotFoundError):
        await storage.get(stored.storage_key)


@pytest.mark.anyio
async def test_local_media_storage_rejects_path_traversal(tmp_path):
    storage = LocalMediaStorage(tmp_path / "media")

    with pytest.raises(UnsafeMediaStorageKey):
        await storage.get("../../secrets.txt")


@pytest.mark.anyio
async def test_local_media_storage_rejects_symlinked_tenant_directory(tmp_path):
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    organization_id = uuid4()
    (root / str(organization_id)).symlink_to(outside, target_is_directory=True)
    storage = LocalMediaStorage(root)

    with pytest.raises(UnsafeMediaStorageKey):
        await storage.put(
            organization_id=organization_id,
            artifact_id=uuid4(),
            extension="mp4",
            mime_type="video/mp4",
            content=b"synthetic video bytes",
        )


@pytest.mark.anyio
async def test_local_media_storage_enforces_object_size_limit(tmp_path):
    storage = LocalMediaStorage(tmp_path / "media", max_object_bytes=4)

    with pytest.raises(MediaObjectTooLarge):
        await storage.put(
            organization_id=uuid4(),
            artifact_id=uuid4(),
            extension="mp4",
            mime_type="video/mp4",
            content=b"too large",
        )



@pytest.mark.anyio
async def test_local_media_storage_rejects_symlinked_object_file(tmp_path):
    root = tmp_path / "media"
    organization_id = uuid4()
    artifact_id = uuid4()
    tenant_directory = root / str(organization_id)
    tenant_directory.mkdir(parents=True)
    outside_file = tmp_path / "outside.mp4"
    outside_file.write_bytes(b"outside data")
    (tenant_directory / f"{artifact_id}.mp4").symlink_to(outside_file)
    storage = LocalMediaStorage(root)

    with pytest.raises(UnsafeMediaStorageKey):
        await storage.get(f"{organization_id}/{artifact_id}.mp4")


@pytest.mark.anyio
async def test_local_media_storage_lists_only_stale_valid_objects(tmp_path):
    storage = LocalMediaStorage(tmp_path / "media")
    stale_org, stale_artifact = uuid4(), uuid4()
    recent_org, recent_artifact = uuid4(), uuid4()
    stale = await storage.put(
        organization_id=stale_org,
        artifact_id=stale_artifact,
        extension="mp4",
        mime_type="video/mp4",
        content=b"stale video",
    )
    recent = await storage.put(
        organization_id=recent_org,
        artifact_id=recent_artifact,
        extension="mp4",
        mime_type="video/mp4",
        content=b"recent video",
    )
    stale_path = tmp_path / "media" / str(stale_org) / f"{stale_artifact}.mp4"
    old_timestamp = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(stale_path, (old_timestamp, old_timestamp))

    listed = await storage.list_objects(
        older_than=datetime.now(UTC) - timedelta(days=1),
        limit=10,
    )

    assert [item.storage_key for item in listed] == [stale.storage_key]
    assert recent.storage_key not in {item.storage_key for item in listed}
