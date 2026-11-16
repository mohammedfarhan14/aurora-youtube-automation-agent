import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.rate_limit import RateLimit, _key, enforce_rate_limits


class FakeRedis:
    def __init__(self, *, allowed: int = 1, retry_after: int = 12) -> None:
        self.allowed = allowed
        self.retry_after = retry_after
        self.calls = []

    async def eval(self, script, numkeys, *args):
        self.calls.append((script, numkeys, args))
        return [self.allowed, self.retry_after]


def test_rate_limit_uses_hashed_identity_and_atomic_multi_key_script():
    redis = FakeRedis()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))

    asyncio.run(
        enforce_rate_limits(
            request,
            RateLimit("login-ip", 30, 60, "127.0.0.1"),
            RateLimit("login-account", 10, 60, "Person@Example.com"),
        )
    )

    script, numkeys, args = redis.calls[0]
    assert "INCR" in script
    assert numkeys == 2
    assert args[0].startswith("aurora:rate-limit:v1:login-ip:")
    assert "person@example.com" not in " ".join(str(value) for value in args)
    assert args[-2:] == (60, 60)


def test_rate_limit_key_is_stable_and_does_not_include_raw_identity():
    secret = b"test-rate-limit-secret-at-least-32-bytes"
    first = _key(RateLimit("login-account", 10, 60, "Person@Example.com"), secret)
    second = _key(RateLimit("login-account", 10, 60, "person@example.com"), secret)

    assert first == second
    assert "person@example.com" not in first
    assert first != _key(
        RateLimit("login-account", 10, 60, "person@example.com"),
        b"different-test-secret-at-least-32-bytes",
    )


def test_rate_limit_returns_429_and_retry_after_when_exceeded():
    redis = FakeRedis(allowed=0, retry_after=17)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            enforce_rate_limits(
                request,
                RateLimit("login-ip", 30, 60, "127.0.0.1"),
            )
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "17"


def test_rate_limit_fails_closed_when_redis_is_unavailable():
    from redis.exceptions import ConnectionError as RedisConnectionError

    class BrokenRedis:
        async def eval(self, *args):
            raise RedisConnectionError("connection refused")

    redis = BrokenRedis()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(redis=redis)))

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            enforce_rate_limits(
                request,
                RateLimit("login-ip", 30, 60, "127.0.0.1"),
            )
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers["Retry-After"] == "5"
