"""Business logic and orchestration service layer for BlueAgent plugin.

Coordinates:
- CTFd Account (Team/User) resolution & deterministic team_k mapping
- BlueAgent infrastructure lazy provisioning & WireGuard VPN configuration
- Challenge container deployment & rollback handling
- Dark OVN verification, concurrency locking, 409 conflict recovery, and solve recording
"""

from datetime import datetime
import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.exc import IntegrityError

from CTFd.cache import clear_challenges, clear_standings
from CTFd.models import Fails, Solves, Users, db
from CTFd.utils import get_config
from CTFd.utils.user import get_current_team, get_current_user

from .client import (
    BlueAgentClient,
    BlueAgentConflict,
    BlueAgentError,
    BlueAgentUnavailable,
    TeamNotFound,
)
from .locks import (
    check_and_set_cooldown,
    refund_cooldown,
    team_deploy_lock,
    team_verify_lock,
)
from .models import (
    BlueAgentDeployment,
    BlueAgentPatchChallenge,
    BlueAgentTeamMapping,
    BlueAgentVerifyLog,
    CapacityError,
)
from .spec import render_spec_for_team

logger = logging.getLogger(__name__)


def get_current_account_context() -> Tuple[int, str, str, Optional[int]]:
    """Resolves current session account context based on CTFd configuration mode.

    Returns:
        (account_id, account_type, account_name, ctfd_user_id)
    """
    user = get_current_user()
    if not user:
        raise PermissionError("Authentication required.")

    mode = get_config("user_mode") or "teams"
    if mode == "teams":
        team = get_current_team()
        if not team:
            raise PermissionError("Team membership required in Teams mode. Please join or create a team.")
        return team.id, "team", team.name, user.id
    else:
        return user.id, "user", user.name, user.id


def get_team_mapping_for_account(account_id: int, account_type: str = "team") -> BlueAgentTeamMapping:
    """Atomically finds or allocates team_k (1..254) for account."""
    return BlueAgentTeamMapping.get_or_create(account_id=account_id, account_type=account_type)


def ensure_team_provisioned(
    team_k: int,
    team_name: str,
    client: Optional[BlueAgentClient] = None,
) -> Dict[str, Any]:
    """Ensures host bridge and WireGuard tunnel exist in BlueAgent."""
    c = client or BlueAgentClient()
    mapping = BlueAgentTeamMapping.query.filter_by(team_k=team_k).first()

    try:
        res = c.create_team(team_k=team_k, name=team_name)
        if mapping and not mapping.provisioned:
            mapping.provisioned = True
            db.session.commit()
        return res
    except Exception as err:
        logger.error("Failed to provision team %d on BlueAgent: %s", team_k, err)
        raise


def ensure_member_vpn(
    team_k: int,
    user_id: int,
    username: str,
    team_name: str,
    client: Optional[BlueAgentClient] = None,
) -> Dict[str, Any]:
    """Idempotently generates or retrieves individual WireGuard VPN profile for player."""
    c = client or BlueAgentClient()
    try:
        member = c.add_member(
            team_k=team_k,
            name=username,
            ctfd_user_id=user_id,
            team_name=team_name,
        )
    except TeamNotFound:
        ensure_team_provisioned(team_k, team_name, client=c)
        member = c.add_member(
            team_k=team_k,
            name=username,
            ctfd_user_id=user_id,
            team_name=team_name,
        )
    return member


def deploy_challenge_for_team(
    team_k: int,
    challenge: BlueAgentPatchChallenge,
    client: Optional[BlueAgentClient] = None,
) -> Dict[str, Any]:
    """Deploys challenge container topology to target team's bridge with static IPs."""
    c = client or BlueAgentClient()
    deployment = BlueAgentDeployment.get_or_create(team_k=team_k, challenge_id=challenge.id)

    with team_deploy_lock(team_k) as acquired:
        if not acquired:
            return {"status": "locked", "message": f"Deployment or maintenance already in progress for team {team_k}."}

        rendered = render_spec_for_team(challenge.parsed_spec, team_k)
        deployment.status = "deploying"
        db.session.commit()

        try:
            res = c.deploy_challenge(team_k=team_k, config=rendered)
            deployment.status = "running"
            deployment.last_error = None
            db.session.commit()
            return {"status": "running", "containers": res.get("containers", [])}
        except TeamNotFound:
            # Lazy create team bridge and retry once
            mapping = BlueAgentTeamMapping.query.filter_by(team_k=team_k).first()
            name = f"Team-{team_k}"
            ensure_team_provisioned(team_k, name, client=c)
            res = c.deploy_challenge(team_k=team_k, config=rendered)
            deployment.status = "running"
            deployment.last_error = None
            db.session.commit()
            return {"status": "running", "containers": res.get("containers", [])}
        except Exception as exc:
            deployment.status = "failed"
            deployment.last_error = str(exc)
            db.session.commit()
            raise


def teardown_challenge_for_team(
    team_k: int,
    challenge: BlueAgentPatchChallenge,
    client: Optional[BlueAgentClient] = None,
) -> None:
    """Tears down container instances for challenge on team bridge."""
    c = client or BlueAgentClient()
    deployment = BlueAgentDeployment.get_or_create(team_k=team_k, challenge_id=challenge.id)

    with team_deploy_lock(team_k) as acquired:
        if not acquired:
            raise RuntimeError(f"Deployment lock busy for team {team_k}.")

        rendered = render_spec_for_team(challenge.parsed_spec, team_k)
        try:
            c.teardown_machines(team_k=team_k, config=rendered)
        except Exception as exc:
            logger.warning("Teardown error on team %d: %s", team_k, exc)
            deployment.last_error = str(exc)

        deployment.status = "torn_down"
        db.session.commit()


def verify_and_record_solve(
    challenge: BlueAgentPatchChallenge,
    team_k: int,
    account_id: int,
    account_type: str,
    user_id: int,
    remote_ip: str,
    client: Optional[BlueAgentClient] = None,
) -> Tuple[Dict[str, Any], int]:
    """Coordinates patch verification and atomic solve commit.

    Returns:
        (response_payload, http_status_code)
    """
    c = client or BlueAgentClient()

    # 1. Check if challenge is already solved by this account
    team_id_val = account_id if account_type == "team" else None
    user_id_val = user_id

    existing_solve = Solves.query.filter_by(
        challenge_id=challenge.id,
        team_id=team_id_val,
    ).first() if team_id_val else Solves.query.filter_by(
        challenge_id=challenge.id,
        user_id=user_id_val,
    ).first()

    if existing_solve:
        return {
            "status": "already_solved",
            "solved": True,
            "message": "Challenge has already been solved by your team!",
        }, 200

    # 2. Enforce per-team verification cooldown
    cooldown_sec = challenge.verify_cooldown or 30
    is_allowed, retry_after = check_and_set_cooldown(team_k, challenge.id, cooldown_sec)
    if not is_allowed:
        return {
            "status": "cooldown",
            "message": f"Verification cooldown active. Please wait {retry_after} seconds before re-testing.",
            "retry_after": retry_after,
        }, 429

    # 3. Acquire exclusive team verification mutex
    with team_verify_lock(team_k, timeout=330) as lock_acquired:
        if not lock_acquired:
            refund_cooldown(team_k, challenge.id)
            return {
                "status": "busy",
                "message": f"Verification test is currently running for team {team_k}. Please wait 30 seconds.",
                "retry_after": 30,
            }, 409

        start_time = time.time()
        rendered_spec = render_spec_for_team(challenge.parsed_spec, team_k)

        try:
            verify_resp = c.verify_challenge(team_k=team_k, config=rendered_spec)
        except BlueAgentConflict as conflict_err:
            # BlueAgent reported 409 (e.g. internal verification in progress)
            refund_cooldown(team_k, challenge.id)
            return {
                "status": "busy",
                "message": f"Verification sandbox busy: {conflict_err.detail}. Please retry in 30 seconds.",
                "retry_after": 30,
            }, 409
        except TeamNotFound:
            refund_cooldown(team_k, challenge.id)
            return {
                "status": "not_deployed",
                "message": "Team environment not found. Please click 'Start Environment' to deploy challenge containers.",
            }, 409
        except BlueAgentUnavailable as unavail_err:
            refund_cooldown(team_k, challenge.id)
            return {
                "status": "error",
                "message": f"BlueAgent orchestrator is temporarily unreachable: {unavail_err}",
            }, 503
        except BlueAgentError as agent_err:
            refund_cooldown(team_k, challenge.id)
            return {
                "status": "error",
                "message": f"Verification engine error: {agent_err.detail}",
            }, 500

        duration_ms = int((time.time() - start_time) * 1000)
        passed = bool(verify_resp.get("passed", False))
        success = bool(verify_resp.get("success", False))
        message = str(verify_resp.get("message", ""))
        raw_details = verify_resp.get("details", {})

        # Log verification run for administrative auditing
        log_entry = BlueAgentVerifyLog(
            team_k=team_k,
            challenge_id=challenge.id,
            user_id=user_id,
            passed=passed,
            success=success,
            message=message,
            details_json=json.dumps(raw_details),
            duration_ms=duration_ms,
        )
        db.session.add(log_entry)

        if passed:
            # 4. Commit solve atomically to CTFd
            solve = Solves(
                challenge_id=challenge.id,
                user_id=user_id,
                team_id=team_id_val,
                ip=remote_ip,
                provided="VERIFIED_PATCH",
            )
            try:
                db.session.add(solve)
                db.session.commit()
                # Bust standings and challenge caches so scoreboard refreshes instantly
                clear_standings()
                clear_challenges()
                return {
                    "status": "passed",
                    "solved": True,
                    "message": "Verification passed! Exploit deflected & services intact. Points awarded!",
                }, 200
            except IntegrityError:
                # Concurrent teammate solved within the same window
                db.session.rollback()
                return {
                    "status": "already_solved",
                    "solved": True,
                    "message": "Verification passed! Challenge was already marked as solved.",
                }, 200
        else:
            # Log failed attempt
            fail = Fails(
                challenge_id=challenge.id,
                user_id=user_id,
                team_id=team_id_val,
                ip=remote_ip,
                provided="FAILED_PATCH",
            )
            db.session.add(fail)
            db.session.commit()

            # Filter details based on admin per-challenge toggle
            details_to_return = raw_details if challenge.show_verify_details else None

            return {
                "status": "failed",
                "solved": False,
                "message": message or "Patch verification failed: services unhealthy or exploit successful.",
                "details": details_to_return,
            }, 200
