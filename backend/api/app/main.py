import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Response, status
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.database.engine import dispose_engine, get_engine
from app.services.metrics import render_prometheus_metrics
from app.services.rate_limit import get_redis_client

settings = get_settings()


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Manage application startup and shutdown resources."""
    redis = get_redis_client(settings.redis_url)
    try:
        await redis.ping()
        application.state.redis = redis
        yield
    finally:
        application.state.redis = None
        try:
            await redis.aclose()
        finally:
            await dispose_engine()


app = FastAPI(
    title=settings.app_name,
    description="AI-powered video production platform API.",
    version="0.1.0",
    debug=settings.debug,
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/", tags=["General"])
async def root():
    return {
        "application": settings.app_name,
        "environment": settings.app_env,
        "status": "running",
    }


@app.get("/health/live", tags=["Health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["Health"])
async def readiness() -> dict[str, str]:
    try:
        async with get_engine().connect() as connection:
            await connection.execute(text("SELECT 1"))
    except (SQLAlchemyError, RuntimeError):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database is unavailable.",
        ) from None

    redis = getattr(app.state, "redis", None)
    owns_client = redis is None
    if owns_client:
        redis = get_redis_client(settings.redis_url)
    try:
        await redis.ping()
    except RedisError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Redis is unavailable.",
        ) from None
    finally:
        if owns_client:
            await redis.aclose()

    return {"status": "ready"}

@app.get("/metrics", include_in_schema=False, tags=["Operations"])
async def metrics_endpoint(
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> Response:
    """Expose low-cardinality operational metrics only to an authenticated scraper."""
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found.")
    expected = (
        settings.metrics_bearer_token.get_secret_value()
        if settings.metrics_bearer_token is not None
        else ""
    )
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Metrics authentication required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Response(
        content=await render_prometheus_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
