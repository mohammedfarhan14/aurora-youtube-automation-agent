from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    organizations,
    production,
    research,
    youtube,
    youtube_analytics,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Authentication"],
)

api_router.include_router(
    youtube.router,
    tags=["YouTube Integrations"],
)

api_router.include_router(
    youtube_analytics.router,
    tags=["YouTube Analytics"],
)

api_router.include_router(
    youtube.callback_router,
    prefix="/integrations/youtube",
    tags=["YouTube Integrations"],
)

api_router.include_router(
    organizations.router,
    prefix="/organizations",
    tags=["Organizations"],
)


api_router.include_router(
    production.router,
    tags=["Production"],
)

api_router.include_router(
    research.router,
    tags=["Research"],
)
