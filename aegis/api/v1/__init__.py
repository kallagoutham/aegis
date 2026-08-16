"""Version 1 of the Aegis HTTP API.

Routers are mounted here rather than in :mod:`aegis.main` so the whole v1
surface can be versioned, and eventually deprecated, as one unit.
"""

from fastapi import APIRouter

from aegis.api.v1 import (
    auth,
    chat,
    health,
    incidents,
    knowledge,
)

api_router = APIRouter()

api_router.include_router(health.router, prefix="/health", tags=["health"])
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(incidents.router, prefix="/incidents", tags=["incidents"])
api_router.include_router(knowledge.router, prefix="/knowledge", tags=["knowledge"])

__all__ = ["api_router"]
