from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.workers import run_media_production_worker as media_worker


@pytest.mark.anyio
async def test_media_worker_does_not_start_without_explicit_enablement(monkeypatch):
    monkeypatch.setattr(
        media_worker,
        "get_settings",
        lambda: SimpleNamespace(media_worker_enabled=False),
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("A disabled media worker must not initialize resources.")

    monkeypatch.setattr(media_worker, "get_session_factory", fail_if_called)
    monkeypatch.setattr(media_worker.shutil, "which", fail_if_called)

    await media_worker.run_media_production_worker(drain=True)


def test_media_worker_is_disabled_by_default():
    assert Settings(_env_file=None).media_worker_enabled is False
