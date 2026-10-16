"""FastAPI dependency injection providers.

Extracts application-wide singletons initialized during the FastAPI lifespan
context from the Request state.
"""

import hmac
from typing import Optional
from fastapi import Header, HTTPException, Request, status
from .config import settings
from .database import DatabaseManager
from .engine.base import ContainerEngine
from .lifecycle.manager import LifecycleManager
from .locks import LockRegistry
from .network.host_net import HostNetworkManager
from .network.wireguard import WireGuardManager


def get_engine(request: Request) -> ContainerEngine:
    """Dependency provider for the container runtime engine."""
    return request.app.state.engine


def get_locks(request: Request) -> LockRegistry:
    """Dependency provider for cross-process resource locks."""
    return request.app.state.locks


def get_db(request: Request) -> DatabaseManager:
    """Dependency provider for relational SQLite state."""
    return request.app.state.db


def get_lifecycle(request: Request) -> LifecycleManager:
    """Dependency provider for the lifecycle reconciliation manager."""
    return request.app.state.lifecycle


def get_host_net(request: Request) -> HostNetworkManager:
    """Dependency provider for host bridge and firewall management."""
    return request.app.state.host_net


def get_wg(request: Request) -> WireGuardManager:
    """Dependency provider for WireGuard interface and peer cryptokeys."""
    return request.app.state.wg


def get_image_builder(request: Request):
    """Dependency provider for the Distrobuilder image compilation engine."""
    return request.app.state.image_builder


def get_challenge_service(request: Request):
    """Dependency provider for ChallengeService."""
    from .services.challenges import ChallengeService
    return ChallengeService(
        db=request.app.state.db,
        engine=request.app.state.engine,
        host_net=request.app.state.host_net,
        locks=request.app.state.locks,
    )


def get_image_service(request: Request):
    """Dependency provider for ImageService."""
    from .services.images import ImageService
    return ImageService(
        db=request.app.state.db,
        builder=request.app.state.image_builder,
        engine=request.app.state.engine,
    )


def get_team_service(request: Request):
    """Dependency provider for TeamService."""
    from .services.teams import TeamService
    return TeamService(
        db=request.app.state.db,
        host_net=request.app.state.host_net,
        wg=request.app.state.wg,
        engine=request.app.state.engine,
        locks=request.app.state.locks,
    )


def require_ctfd_token(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    x_ctfd_token: Optional[str] = Header(default=None, alias="X-CTFd-Token"),
) -> str:
    """Validates CTFd authentication token from Authorization or X-CTFd-Token header."""
    token: Optional[str] = None
    if authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
        elif len(parts) == 1:
            token = parts[0]
    elif x_ctfd_token:
        token = x_ctfd_token

    expected = settings.ctfd_api_token
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing CTFd authorization token",
        )
    return token

