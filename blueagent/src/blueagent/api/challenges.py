"""Stateless challenge deployment and automated dark-sandbox verification endpoints."""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status

from ..dependencies import get_challenge_service, require_ctfd_token
from ..locks import LockTimeoutError
from ..models import (
    ActiveChallengeRecord,
    ChallengeConfig,
    RunningContainerRecord,
    VerifyResultResp,
)
from ..services.challenges import (
    BridgeNotActiveError,
    ChallengeService,
    DeploymentFailedError,
    DeploymentRollbackError,
    ImageNotReadyError,
    TeamNotFoundError,
    TeardownError,
    VerificationLockedError,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/teams",
    tags=["machines", "verificator"],
    dependencies=[Depends(require_ctfd_token)],
)


@router.post("/{team_k}/machines", response_model=ActiveChallengeRecord, status_code=status.HTTP_201_CREATED)
async def deploy_challenge(
    team_k: int,
    config: ChallengeConfig,
    service: ChallengeService = Depends(get_challenge_service),
):
    """Deploys a multi-container challenge to the target team's production bridge."""
    try:
        return await service.deploy_challenge(team_k=team_k, config=config)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except BridgeNotActiveError as err:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except ImageNotReadyError as err:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(err),
        )
    except DeploymentRollbackError as err:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(err),
        )
    except DeploymentFailedError as err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(err),
        )
    except LockTimeoutError as lock_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(lock_err),
        )


@router.delete("/{team_k}/machines", status_code=status.HTTP_204_NO_CONTENT)
async def teardown_challenge_endpoint(
    team_k: int,
    containers: Optional[str] = Query(default=None, description="Comma-separated container names to teardown (e.g. 'web,db')"),
    config: Optional[ChallengeConfig] = None,
    service: ChallengeService = Depends(get_challenge_service),
):
    """Tears down containers deployed for a challenge on team k."""
    try:
        c_names = None
        if containers:
            c_names = [c.strip() for c in containers.split(",") if c.strip()]
        elif config and config.containers:
            c_names = [c.name for c in config.containers]

        challenge_id = config.challenge_id if config else "ch"
        await service.teardown_challenge(
            team_k=team_k,
            challenge_id=challenge_id,
            container_names=c_names,
        )
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except LockTimeoutError as lock_err:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(lock_err),
        )
    except TeardownError as td_err:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(td_err),
        )


@router.get("/{team_k}/machines", response_model=List[RunningContainerRecord])
async def get_active_containers(
    team_k: int,
    service: ChallengeService = Depends(get_challenge_service),
):
    """Retrieves running containers for a team."""
    try:
        return await service.get_active_containers(team_k)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )


@router.post("/{team_k}/verificator", response_model=VerifyResultResp)
async def verify_challenge(
    team_k: int,
    config: ChallengeConfig,
    skip_cleanup: bool = False,
    service: ChallengeService = Depends(get_challenge_service),
):
    """Executes verification against dark OVN sandbox clones with readiness checks."""
    try:
        return await service.verify_challenge(team_k=team_k, config=config, skip_cleanup=skip_cleanup)
    except TeamNotFoundError as err:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(err),
        )
    except (VerificationLockedError, LockTimeoutError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Verification already in progress or team resource locked for team {team_k}",
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Verification failed unexpectedly for team %d: %s", team_k, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Verification failed: {exc}",
        )


@router.delete("/{team_k}/verificator", status_code=status.HTTP_200_OK)
async def cleanup_verify(
    team_k: int,
    config: Optional[ChallengeConfig] = None,
    service: ChallengeService = Depends(get_challenge_service),
):
    """Tears down ephemeral verifier runner, clones, and dark OVN network for team k."""
    try:
        timings = await service.cleanup_verify_sandbox(team_k=team_k, config=config)
        return {"status": "cleaned", "step_timings": timings}
    except Exception as exc:
        logger.exception("Cleanup failed unexpectedly for team %d: %s", team_k, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Verify cleanup failed: {exc}",
        )

