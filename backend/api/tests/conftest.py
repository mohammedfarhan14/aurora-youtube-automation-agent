"""Pytest safety checks and shared test configuration.

The backend test suite must never connect to the development database or
share rate-limit state with the development Redis database.
"""

import os
from urllib.parse import urlsplit, urlunsplit

import pytest
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings

_TEST_REDIS_DATABASE = 15
_RATE_LIMIT_KEY_PATTERN = "aurora:rate-limit:v1:*"


def pytest_configure(config) -> None:
    settings = Settings()
    configured_test_url = settings.test_database_url

    if configured_test_url is None:
        pytest.exit(
            "TEST_DATABASE_URL is required. Configure a dedicated PostgreSQL "
            "test database before running the backend test suite.",
            returncode=2,
        )

    test_url = configured_test_url.get_secret_value()

    try:
        parsed_url = make_url(test_url)
    except Exception:
        pytest.exit(
            "TEST_DATABASE_URL is invalid. Check its PostgreSQL connection URL.",
            returncode=2,
        )

    database_name = parsed_url.database or ""

    if parsed_url.get_backend_name() != "postgresql" or not database_name.endswith(
        "_test"
    ):
        pytest.exit(
            "Refusing to run tests: TEST_DATABASE_URL must target PostgreSQL "
            "and the database name must end with '_test'.",
            returncode=2,
        )

    redis_parts = urlsplit(settings.redis_url)

    if redis_parts.scheme not in {"redis", "rediss"} or not redis_parts.hostname:
        pytest.exit(
            "REDIS_URL must identify a valid Redis server for backend tests.",
            returncode=2,
        )

    test_redis_url = urlunsplit(
        (
            redis_parts.scheme,
            redis_parts.netloc,
            f"/{_TEST_REDIS_DATABASE}",
            redis_parts.query,
            "",
        )
    )

    # Override both services before test modules import the application.
    os.environ["DATABASE_URL"] = test_url
    os.environ["REDIS_URL"] = test_redis_url
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def isolate_test_rate_limit_keys():
    """Clear only application rate-limit keys in the dedicated test Redis DB."""
    settings = get_settings()
    redis_parts = urlsplit(settings.redis_url)

    if redis_parts.path != f"/{_TEST_REDIS_DATABASE}":
        pytest.fail(
            "Refusing to run tests: Redis must use dedicated database 15."
        )

    redis = Redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )

    try:
        redis.ping()
        keys = list(redis.scan_iter(match=_RATE_LIMIT_KEY_PATTERN, count=200))

        if keys:
            redis.delete(*keys)
    except RedisError:
        pytest.fail(
            "Dedicated test Redis database 15 is unavailable."
        )
    finally:
        redis.close()
