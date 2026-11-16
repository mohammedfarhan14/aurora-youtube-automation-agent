from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import pytest

from app.media.storage import StoredMediaObject, StoredMediaObjectInfo
from app.services.media_artifacts import (
    MediaArtifactConflictError,
    MediaStageLeaseError,
    cleanup_orphaned_media_objects,
    store_media_artifact,
)


class FakeTransaction:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type is None and self.session.pending is not None:
            self.session.state.existing = self.session.pending
        return False


class FakeDatabaseState:
    def __init__(self, stage):
        self.stage = stage
        self.existing = None
        self.fail_commit = False
        self.authorized = True
        self.registered_keys = set()


class FakeScalarResult:
    def __init__(self, values):
        self.values = values

    def all(self):
        return list(self.values)


class FakeSession:
    def __init__(self, state):
        self.state = state
        self.pending = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return FakeTransaction(self)

    async def scalars(self, statement):
        return FakeScalarResult(self.state.registered_keys)

    async def scalar(self, statement):
        sql = str(statement)
        if "production_stages" in sql:
            return self.state.stage if self.state.authorized else None
        if "media_artifacts" in sql:
            return self.state.existing
        raise AssertionError(f"Unexpected query: {sql}")

    def add(self, record):
        self.pending = record

    async def flush(self):
        if self.state.fail_commit:
            raise RuntimeError("synthetic database failure")


class FakeStorage:
    backend = "local"

    def __init__(self):
        self.objects = {}
        self.put_count = 0
        self.delete_count = 0
        self.inventory = []

    async def put(
        self,
        *,
        organization_id,
        artifact_id,
        extension,
        mime_type,
        content,
    ):
        self.put_count += 1
        key = f"{organization_id}/{artifact_id}.{extension}"
        self.objects[key] = content
        return StoredMediaObject(
            storage_key=key,
            mime_type=mime_type,
            sha256=sha256(content).hexdigest(),
            byte_size=len(content),
        )

    async def list_objects(self, *, older_than, limit=500):
        return [item for item in self.inventory if item.last_modified < older_than][:limit]

    async def get(self, storage_key):
        return self.objects[storage_key]

    async def delete(self, storage_key):
        self.delete_count += 1
        self.objects.pop(storage_key, None)


def make_state(*, lease_token=None):
    return FakeDatabaseState(
        type(
            "Stage",
            (),
            {
                "status": "running",
                "lease_token": lease_token or uuid4(),
                "attempt_count": 1,
                "lease_expires_at": datetime.now(UTC) + timedelta(minutes=5),
            },
        )()
    )


@pytest.mark.anyio
async def test_store_media_artifact_is_tenant_bound_and_idempotent():
    organization_id = uuid4()
    production_run_id = uuid4()
    production_stage_id = uuid4()
    lease_token = uuid4()
    state = make_state(lease_token=lease_token)
    storage = FakeStorage()
    def session_factory():
        return FakeSession(state)
    args = {
        "session_factory": session_factory,
        "storage": storage,
        "organization_id": organization_id,
        "production_run_id": production_run_id,
        "production_stage_id": production_stage_id,
        "lease_token": lease_token,
        "attempt_count": 1,
        "artifact_slot": "narration-main",
        "artifact_kind": "audio",
        "extension": "wav",
        "mime_type": "audio/wav",
        "content": b"audio bytes",
        "duration_ms": 30_000,
        "metadata": {"provider": "mock"},
    }

    first = await store_media_artifact(**args)
    second = await store_media_artifact(**args)

    assert first.id == second.id
    assert first.storage_key == second.storage_key
    assert first.sha256 == sha256(b"audio bytes").hexdigest()
    assert first.organization_id == organization_id
    assert first.production_run_id == production_run_id
    assert first.production_stage_id == production_stage_id
    assert storage.put_count == 1
    assert storage.delete_count == 0


@pytest.mark.anyio
async def test_store_media_artifact_rejects_different_content_for_same_slot():
    lease_token = uuid4()
    state = make_state(lease_token=lease_token)
    storage = FakeStorage()
    def session_factory():
        return FakeSession(state)
    args = {
        "session_factory": session_factory,
        "storage": storage,
        "organization_id": uuid4(),
        "production_run_id": uuid4(),
        "production_stage_id": uuid4(),
        "lease_token": lease_token,
        "attempt_count": 1,
        "artifact_slot": "scene-001",
        "artifact_kind": "image",
        "extension": "png",
        "mime_type": "image/png",
        "content": b"first image",
    }
    await store_media_artifact(**args)

    with pytest.raises(MediaArtifactConflictError):
        await store_media_artifact(**{**args, "content": b"different image"})

    assert storage.put_count == 1


@pytest.mark.anyio
async def test_store_media_artifact_rejects_lost_stage_lease_before_upload():
    lease_token = uuid4()
    state = make_state(lease_token=uuid4())
    state.authorized = False
    storage = FakeStorage()

    with pytest.raises(MediaStageLeaseError):
        await store_media_artifact(
            session_factory=lambda: FakeSession(state),
            storage=storage,
            organization_id=uuid4(),
            production_run_id=uuid4(),
            production_stage_id=uuid4(),
            lease_token=lease_token,
            attempt_count=1,
            artifact_slot="render-final",
            artifact_kind="video",
            extension="mp4",
            mime_type="video/mp4",
            content=b"video bytes",
        )

    assert storage.put_count == 0


@pytest.mark.anyio
async def test_store_media_artifact_compensates_when_database_commit_fails():
    lease_token = uuid4()
    state = make_state(lease_token=lease_token)
    state.fail_commit = True
    storage = FakeStorage()

    with pytest.raises(RuntimeError, match="synthetic database failure"):
        await store_media_artifact(
            session_factory=lambda: FakeSession(state),
            storage=storage,
            organization_id=uuid4(),
            production_run_id=uuid4(),
            production_stage_id=uuid4(),
            lease_token=lease_token,
            attempt_count=1,
            artifact_slot="captions-main",
            artifact_kind="captions",
            extension="vtt",
            mime_type="text/vtt",
            content=b"WEBVTT",
        )

    assert storage.put_count == 1
    assert storage.delete_count == 1
    assert storage.objects == {}


@pytest.mark.anyio
async def test_cleanup_deletes_only_old_unregistered_objects():
    state = make_state()
    storage = FakeStorage()
    registered_key = f"{uuid4()}/{uuid4()}.mp4"
    orphan_key = f"{uuid4()}/{uuid4()}.mp4"
    stale_time = datetime.now(UTC) - timedelta(days=2)
    recent_time = datetime.now(UTC) - timedelta(minutes=5)
    state.registered_keys.add(registered_key)
    storage.inventory = [
        StoredMediaObjectInfo(storage_key=registered_key, last_modified=stale_time),
        StoredMediaObjectInfo(storage_key=orphan_key, last_modified=stale_time),
        StoredMediaObjectInfo(
            storage_key=f"{uuid4()}/{uuid4()}.mp4",
            last_modified=recent_time,
        ),
    ]
    storage.objects = {
        registered_key: b"registered",
        orphan_key: b"orphan",
    }

    removed = await cleanup_orphaned_media_objects(
        session_factory=lambda: FakeSession(state),
        storage=storage,
        grace_period=timedelta(days=1),
        limit=10,
    )

    assert removed == 1
    assert registered_key in storage.objects
    assert orphan_key not in storage.objects
    assert storage.delete_count == 1


@pytest.mark.anyio
async def test_cleanup_rejects_nonpositive_grace_period_or_limit():
    state = make_state()
    storage = FakeStorage()

    with pytest.raises(ValueError, match="positive grace period"):
        await cleanup_orphaned_media_objects(
            session_factory=lambda: FakeSession(state),
            storage=storage,
            grace_period=timedelta(0),
        )
