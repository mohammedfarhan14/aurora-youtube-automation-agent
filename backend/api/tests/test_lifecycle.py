import asyncio
from functools import lru_cache
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

import app.database.engine as database_engine
import app.main as main_module


def test_lifespan_disposes_engine_on_shutdown(monkeypatch):
    dispose_engine = AsyncMock()

    monkeypatch.setattr(
        main_module,
        "dispose_engine",
        dispose_engine,
    )

    with TestClient(main_module.app) as client:
        response = client.get("/health/live")

        assert response.status_code == 200
        assert response.json() == {"status": "alive"}

    dispose_engine.assert_awaited_once()


def test_dispose_engine_disposes_cached_engine_and_clears_caches(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())

    @lru_cache
    def cached_engine():
        return engine

    @lru_cache
    def cached_session_factory():
        return object()

    cached_engine()
    cached_session_factory()

    monkeypatch.setattr(database_engine, "get_engine", cached_engine)
    monkeypatch.setattr(
        database_engine,
        "get_session_factory",
        cached_session_factory,
    )

    asyncio.run(database_engine.dispose_engine())

    engine.dispose.assert_awaited_once()
    assert cached_session_factory.cache_info().currsize == 0
    assert cached_engine.cache_info().currsize == 0
