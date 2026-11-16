import asyncio

import pytest

from app.core.config import Settings
from app.workers import run_story_production_worker as worker_module


def test_story_worker_is_disabled_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.story_worker_enabled is False
    assert settings.story_worker_llm_base_url is None
    assert settings.story_worker_model is None


def test_enabling_story_worker_requires_explicit_provider_configuration() -> None:
    with pytest.raises(ValueError, match="LLM base URL is required"):
        Settings(_env_file=None, story_worker_enabled=True, story_worker_model="local-model")


def test_story_worker_disabled_exits_without_claiming_stages(monkeypatch) -> None:
    settings = Settings(_env_file=None, story_worker_enabled=False)
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)

    async def fail_if_called():
        raise AssertionError("A disabled worker must not create a database session.")

    monkeypatch.setattr(worker_module, "get_session_factory", fail_if_called)
    asyncio.run(worker_module.run_story_production_worker())
