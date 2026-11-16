import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.config import get_settings
from app.services.rate_limit import (
    RateLimit,
    _key,
    enforce_rate_limits,
    get_redis_client,
)


def _identity_secret() -> bytes:
    configured = get_settings().rate_limit_identity_secret
    if configured is None:
        return b"aurora-development-rate-limit-key"
    return configured.get_secret_value().encode("utf-8")


def test_redis_rate_limit_is_atomic_under_concurrent_requests() -> None:
    async def exercise() -> None:
        redis = get_redis_client(get_settings().redis_url)
        limit = RateLimit(
            scope="integration-concurrent-login",
            limit=5,
            window_seconds=60,
            identity=f"test-{uuid4()}",
        )
        key = _key(limit, _identity_secret())
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=redis))
        )

        try:
            await redis.ping()
            results = await asyncio.gather(
                *(enforce_rate_limits(request, limit) for _ in range(20)),
                return_exceptions=True,
            )

            allowed = sum(result is None for result in results)
            rejected = [
                result
                for result in results
                if isinstance(result, HTTPException)
            ]

            assert allowed == 5
            assert len(rejected) == 15
            assert all(result.status_code == 429 for result in rejected)
            assert await redis.get(key) == "5"
        finally:
            await redis.delete(key)
            await redis.aclose()

    asyncio.run(exercise())


def test_redis_multi_bucket_rejection_does_not_increment_other_buckets() -> None:
    async def exercise() -> None:
        redis = get_redis_client(get_settings().redis_url)
        identity = f"test-{uuid4()}"
        primary = RateLimit(
            scope="integration-primary",
            limit=1,
            window_seconds=60,
            identity=identity,
        )
        secondary = RateLimit(
            scope="integration-secondary",
            limit=10,
            window_seconds=60,
            identity=identity,
        )
        secret = _identity_secret()
        keys = [_key(primary, secret), _key(secondary, secret)]
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(redis=redis))
        )

        try:
            await redis.ping()
            await enforce_rate_limits(request, primary, secondary)

            with pytest.raises(HTTPException) as exc_info:
                await enforce_rate_limits(request, primary, secondary)

            assert exc_info.value.status_code == 429
            assert await redis.get(keys[0]) == "1"
            assert await redis.get(keys[1]) == "1"
        finally:
            await redis.delete(*keys)
            await redis.aclose()

    asyncio.run(exercise())
