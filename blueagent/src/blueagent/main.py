"""FastAPI application entry point and lifespan context for BlueAgent v2.

Coordinates lifecycle initialization of singleton resources (IncusEngine,
LockRegistry, DatabaseManager, LifecycleManager) and mounts API routers.
"""

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import challenges_router, health_router, images_router, teams_router
from .config import settings
from .database import DatabaseManager
from .engine.incus import IncusEngine
from .images.distrobuilder import ImageBuilder
from .lifecycle.manager import LifecycleManager
from .locks import LockRegistry
from .network.host_net import HostNetworkManager
from .network.wireguard import WireGuardManager


logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("blueagent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Manages application startup and graceful shutdown hooks."""
    logger.info("Initializing BlueAgent v2 platform singletons...")

    # 1. Initialize SQLite Database and run schema migrations
    db = DatabaseManager(db_path=settings.database_path)
    await db.init_db()
    reconciled = await db.reconcile_startup_states()
    logger.info("Startup state reconciliation complete: %s", reconciled)
    app.state.db = db
    logger.info("SQLite database initialized at '%s' (WAL mode)", settings.database_path)

    # 2. Initialize Lock Registry (OS fcntl file locks for cross-worker safety)
    locks = LockRegistry(lock_dir=settings.lock_directory)
    app.state.locks = locks
    logger.info("LockRegistry initialized at '%s'", settings.lock_directory)

    # 3. Initialize Incus Container Engine singleton for this process
    engine = IncusEngine(
        socket_path=settings.incus_socket_path,
        base_url=settings.incus_base_url,
    )
    app.state.engine = engine
    logger.info("IncusEngine initialized via Unix socket '%s'", settings.incus_socket_path)

    # 4. Initialize Distrobuilder Image Builder
    image_builder = ImageBuilder(db=db, engine=engine, locks=locks)
    app.state.image_builder = image_builder

    # 4. Initialize Host Network and WireGuard Managers
    host_net = HostNetworkManager()
    wg = WireGuardManager()
    app.state.host_net = host_net
    app.state.wg = wg
    logger.info("HostNetworkManager and WireGuardManager initialized")

    # 5. Apply network forwarding & nftables rules
    try:
        await host_net.ensure_ip_forwarding()
        await host_net.apply_nftables_rules()
        logger.info("Host nftables rules and MSS clamping applied")
    except Exception as err:
        logger.warning("Could not apply host nftables rules at startup: %s", err)

    # 6. Re-sync live network state from database
    try:
        teams = await db.list_teams()
        for t in teams:
            logger.info("Re-syncing team %d (%s) network interfaces", t.team_k, t.name)
            await host_net.setup_team_bridge(t.team_k)
            await wg.setup_team_interface(t.team_k)
            members = await db.list_team_members(t.team_k)
            for m in members:
                await wg.add_peer(
                    team_k=t.team_k,
                    member_id=m.member_id,
                    public_key=m.public_key,
                    allowed_ips=m.client_ip,
                    preshared_key=m.preshared_key,
                )
    except Exception as err:
        logger.warning("Error during boot network reconciliation: %s", err)

    # 7. Initialize and start Lifecycle Manager background reconciliation worker
    lifecycle = LifecycleManager(
        engine=engine,
        interval_seconds=settings.reconciliation_interval_seconds,
        grace_period_seconds=settings.resource_grace_period_seconds,
    )
    await lifecycle.start()
    app.state.lifecycle = lifecycle
    logger.info("LifecycleManager background worker started")

    yield

    logger.info("Shutting down BlueAgent v2 platform singletons...")
    # 8. Stop background reconciliation worker gracefully
    await lifecycle.stop()

    # 6. Close Incus HTTP client and transport
    await engine.aclose()
    logger.info("BlueAgent shutdown complete.")


app = FastAPI(
    title="BlueAgent v2 Control Plane",
    description="Automated Blue-Teaming and Verification Platform combining native Linux Bridges with OVN dark sandboxes.",
    version="0.1.0",
    lifespan=lifespan,
)

# Cross-Origin Resource Sharing
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount API Routers
app.include_router(health_router)
app.include_router(teams_router)
app.include_router(images_router)
app.include_router(challenges_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "blueagent.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
