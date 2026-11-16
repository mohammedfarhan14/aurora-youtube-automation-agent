import asyncio

import pytest

from app.main import app, lifespan


def test_lifespan_disposes_engine_when_redis_close_fails(monkeypatch) -> None:
    calls: list[str] = []

    class FakeRedis:
        async def ping(self) -> None:
            calls.append("ping")

        async def aclose(self) -> None:
            calls.append("redis-close")
            raise RuntimeError("Redis close failed")

    async def fake_dispose_engine() -> None:
        calls.append("engine-dispose")

    monkeypatch.setattr("app.main.get_redis_client", lambda _url: FakeRedis())
    monkeypatch.setattr("app.main.dispose_engine", fake_dispose_engine)

    async def exercise_lifespan() -> None:
        with pytest.raises(RuntimeError, match="Redis close failed"):
            async with lifespan(app):
                assert app.state.redis is not None

    asyncio.run(exercise_lifespan())

    assert calls == ["ping", "redis-close", "engine-dispose"]
    assert app.state.redis is None
