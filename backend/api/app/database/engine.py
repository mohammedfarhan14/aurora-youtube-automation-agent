import os
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def _database_url():
    """Apply Docker host overrides while keeping .env usable from the host."""
    settings = get_settings()
    if settings.database_url is None:
        raise RuntimeError("Database URL is not configured.")
    url = settings.database_url.get_secret_value()
    host = os.getenv("DATABASE_HOST")
    port = os.getenv("DATABASE_PORT")
    if host or port:
        from sqlalchemy.engine import make_url

        parsed = make_url(url)
        url = parsed.set(
            host=host or parsed.host,
            port=int(port) if port else parsed.port,
        ).render_as_string(hide_password=False)
    return url


@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(
        _database_url(),
        pool_pre_ping=True,
    )


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
    )


async def dispose_engine() -> None:
    """Dispose the cached database engine and clear its associated caches."""

    engine = get_engine() if get_engine.cache_info().currsize > 0 else None

    # Clear the session factory before disposing its bound engine.
    get_session_factory.cache_clear()
    get_engine.cache_clear()

    if engine is not None:
        await engine.dispose()
