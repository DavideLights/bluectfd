"""API route definitions and endpoints for BlueAgent v2."""

from .teams import router as teams_router
from .challenges import router as challenges_router
from .images import router as images_router
from .health import router as health_router

__all__ = ["teams_router", "challenges_router", "images_router", "health_router"]
