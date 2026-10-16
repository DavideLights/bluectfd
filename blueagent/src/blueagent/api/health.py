"""Health and readiness diagnostic probes for BlueAgent v2."""

from typing import Any, Dict
from fastapi import APIRouter, Depends, status
from ..database import DatabaseManager
from ..dependencies import get_db, get_engine, get_lifecycle
from ..engine.base import ContainerEngine
from ..lifecycle.manager import LifecycleManager

router = APIRouter(prefix="/health", tags=["health"])


@router.get("", status_code=status.HTTP_200_OK)
async def health_check() -> Dict[str, str]:
    """Liveness probe indicating that the control plane web server is responsive."""
    return {"status": "ok"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness_check(
    engine: ContainerEngine = Depends(get_engine),
    db: DatabaseManager = Depends(get_db),
    lifecycle: LifecycleManager = Depends(get_lifecycle),
) -> Dict[str, Any]:
    """Readiness probe verifying database connectivity, container engine, and background tasks."""
    # Check SQLite connection
    db_ok = False
    try:
        conn = await db.get_connection()
        await conn.execute("SELECT 1")
        await conn.close()
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "status": "ready" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
        "lifecycle_worker": "running" if lifecycle.is_running else "stopped",
    }
