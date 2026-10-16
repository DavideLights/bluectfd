"""Team registration and multi-member WireGuard credential management endpoints."""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import PlainTextResponse

from ..dependencies import get_team_service, require_ctfd_token
from ..locks import LockTimeoutError
from ..models import (
    TeamMemberPutReq,
    TeamMemberResp,
    TeamMemberWG,
    TeamPutReq,
    TeamRecord,
)
from ..services.teams import (
    MemberAlreadyAssignedError,
    MemberConflictError,
    MemberNotFoundError,
    TeamAddressSpaceExhaustedError,
    TeamDatabaseError,
    TeamNetworkError,
    TeamNotFoundError,
    TeamService,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/teams", tags=["teams"], dependencies=[Depends(require_ctfd_token)])


@router.put("/{team_k}", response_model=TeamRecord)
async def create_or_update_team(
    team_k: int,
    payload: TeamPutReq,
    response: Response,
    service: TeamService = Depends(get_team_service),
):
    """Provisions or updates a team environment, allocating bridge and WireGuard namespaces."""
    try:
        team, is_new = await service.create_or_update_team(team_k=team_k, name=payload.name)
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return team
    except LockTimeoutError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Timed out acquiring lock for team {team_k}: {err}",
        )
    except (TeamNetworkError, TeamDatabaseError) as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(err),
        )


@router.get("", response_model=List[TeamRecord])
async def list_teams(
    service: TeamService = Depends(get_team_service),
):
    """Lists all registered teams."""
    return await service.list_teams()


@router.get("/{team_k}", response_model=TeamRecord)
async def get_team(
    team_k: int,
    service: TeamService = Depends(get_team_service),
):
    """Retrieves metadata for a specific team."""
    team = await service.get_team(team_k)
    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team {team_k} not found",
        )
    return team


@router.delete("/{team_k}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    team_k: int,
    service: TeamService = Depends(get_team_service),
):
    """Tears down all active containers, network namespaces, and container profiles for a team."""
    try:
        await service.teardown_team(team_k)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except LockTimeoutError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Timed out acquiring lock for team {team_k}: {err}",
        )
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to tear down containers for team {team_k}: {err}",
        )


@router.put(
    "/{team_k}/members/{user_id}",
    response_model=TeamMemberWG,
)
async def add_or_update_team_member(
    team_k: int,
    user_id: str,
    payload: TeamMemberPutReq,
    response: Response,
    service: TeamService = Depends(get_team_service),
):
    """Registers or updates a team member's WireGuard configuration."""
    try:
        member, is_new = await service.add_member(
            team_k=team_k,
            user_id=user_id,
            name=payload.name,
            public_key=payload.public_key,
        )
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return member
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except MemberAlreadyAssignedError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except TeamAddressSpaceExhaustedError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except MemberConflictError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except LockTimeoutError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Timed out acquiring lock for team {team_k}: {err}",
        )
    except Exception as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(err),
        )


@router.get("/{team_k}/members", response_model=List[TeamMemberResp])
async def list_team_members(
    team_k: int,
    service: TeamService = Depends(get_team_service),
):
    """Lists all WireGuard client peers registered for a team with real-time telemetry."""
    try:
        return await service.list_members(team_k)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )


@router.get("/{team_k}/members/{user_id}", response_model=TeamMemberResp)
async def get_team_member(
    team_k: int,
    user_id: str,
    service: TeamService = Depends(get_team_service),
):
    """Retrieves metadata and telemetry for a specific team member."""
    try:
        return await service.get_member(team_k, user_id)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except MemberNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )


@router.get("/{team_k}/members/{user_id}/config", response_class=PlainTextResponse)
async def get_member_config(
    team_k: int,
    user_id: str,
    service: TeamService = Depends(get_team_service),
):
    """Retrieves full WireGuard .conf file text for a member."""
    try:
        conf_text = await service.get_member_config(team_k, user_id)
        return PlainTextResponse(content=conf_text, media_type="text/plain")
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except MemberNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )


@router.delete("/{team_k}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team_member(
    team_k: int,
    user_id: str,
    service: TeamService = Depends(get_team_service),
):
    """Removes a member from WireGuard and frees their assigned IP."""
    try:
        await service.remove_member(team_k, user_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except MemberNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except LockTimeoutError as err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Timed out acquiring lock for team {team_k}: {err}",
        )
