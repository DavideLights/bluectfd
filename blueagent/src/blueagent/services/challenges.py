"""Service layer for challenge deployment, verification, and container lifecycle."""

from datetime import datetime, timezone
import asyncio
import logging
import time
from typing import List, Optional

from ..config import settings
from ..database import DatabaseManager
from ..engine.base import ContainerEngine
from ..incus import devices
from ..locks import LockRegistry, LockTimeoutError
from ..models import (
    ChallengeConfig,
    DeployResponse,
    RunningContainerRecord,
    VerifyResultResp,
)
from ..network.commands import ip
from ..network.host_net import HostNetworkManager
from ..network.templates import render_interfaces_conf, render_networkd_conf
from ..verifier.ovn import OVNVerifier

logger = logging.getLogger(__name__)


class ChallengeServiceError(Exception):
    """Base exception for challenge service operations."""


class TeamNotFoundError(ChallengeServiceError):
    """Raised when the specified team does not exist."""


class BridgeNotActiveError(ChallengeServiceError):
    """Raised when the team production bridge is not active."""


class ImageNotReadyError(ChallengeServiceError):
    """Raised when a container image is not ready or missing."""


class DeploymentRollbackError(ChallengeServiceError):
    """Raised when deployment fails and rollback succeeds."""


class DeploymentFailedError(ChallengeServiceError):
    """Raised when deployment fails and rollback also encounters errors."""


class VerificationLockedError(ChallengeServiceError):
    """Raised when verification cannot acquire required locks."""


class TeardownError(ChallengeServiceError):
    """Raised when container or state teardown fails."""


async def teardown_challenge(
    challenge_id: str,
    team_k: int,
    db: DatabaseManager,
    engine: ContainerEngine,
    container_names: Optional[List[str]] = None,
) -> None:
    """Tears down container instances and clears database state.

    If container_names is provided, only those specific containers are torn down.
    Otherwise, all containers for team_k are torn down.
    Expects caller to manage locks.
    """
    targets = []
    if container_names:
        for name in container_names:
            c = await db.get_container_by_spec(team_k, name)
            if c:
                targets.append(c)
            else:
                c_full = await db.get_container(name)
                if c_full and c_full.team_k == team_k:
                    targets.append(c_full)
                else:
                    full_name = f"c-{team_k}-{name}"
                    targets.append(type("TempTarget", (), {"instance_name": full_name})())
    else:
        targets = await db.list_team_containers(team_k)

    errors = []

    async def _delete_inst(inst):
        try:
            logger.info("Stopping and deleting instance %s", inst.instance_name)
            await engine.delete_instance(inst.instance_name, force=True)
            await db.delete_container(inst.instance_name)
        except Exception as exc:
            logger.warning("Error deleting instance %s: %s", inst.instance_name, exc)
            errors.append(f"{inst.instance_name}: {exc}")

    if targets:
        await asyncio.gather(*[_delete_inst(inst) for inst in targets])

    if errors:
        err_msg = "; ".join(errors)
        raise TeardownError(f"Failed to teardown containers for team {team_k}: {err_msg}")

    logger.info("Containers for team %d successfully torn down.", team_k)


class ChallengeService:
    """Orchestrates challenge deployment, container lifecycle, and sandbox verification."""

    def __init__(
        self,
        db: DatabaseManager,
        engine: ContainerEngine,
        host_net: HostNetworkManager,
        locks: LockRegistry,
    ):
        self.db = db
        self.engine = engine
        self.host_net = host_net
        self.locks = locks

    async def deploy_challenge(self, team_k: int, config: ChallengeConfig) -> DeployResponse:
        """Deploys a multi-container challenge to the target team's bridge idempotently."""
        config = config.render_for_team(team_k)
        async with self.locks.lock_team(team_k):
            t_deploy_start = time.perf_counter()
            # 1. Verify team exists
            t0 = time.perf_counter()
            team = await self.db.get_team_by_k(team_k)
            if not team:
                raise TeamNotFoundError(f"Team {team_k} does not exist")

            # 2. Verify team production bridge exists and is active
            bridge_name = f"{settings.host_bridge_prefix}{team_k}"
            bridge_check = await self.host_net.runner.run(
                ip.link_show(bridge_name, json=True),
                check=False,
            )
            if bridge_check.returncode != 0:
                raise BridgeNotActiveError(
                    f"Production bridge '{bridge_name}' for team {team_k} is not provisioned or active"
                )

            # 3. Verify all container images are from distrobuilder and ready
            for c in config.containers:
                img_record = await self.db.get_image(c.image)
                if not img_record or img_record.status != "ready":
                    raise ImageNotReadyError(
                        f"Image '{c.image}' is not ready in distrobuilder catalog. "
                        f"Only pre-built distrobuilder images are allowed. Build via PUT /images/{c.image} first."
                    )
                if not await self.engine.image_alias_exists(c.image):
                    raise ImageNotReadyError(
                        f"Image alias '{c.image}' does not exist in container engine runtime"
                    )

            if config.verifier:
                v_img = config.verifier.image
                v_record = await self.db.get_image(v_img)
                if not v_record or v_record.status != "ready":
                    raise ImageNotReadyError(
                        f"Verifier image '{v_img}' is not ready in distrobuilder catalog. "
                        f"Only pre-built distrobuilder images are allowed. Build via PUT /images/{v_img} first."
                    )
                if not await self.engine.image_alias_exists(v_img):
                    raise ImageNotReadyError(
                        f"Verifier image alias '{v_img}' does not exist in container engine runtime"
                    )
            validation_sec = time.perf_counter() - t0

            # 4. Container provisioning and configuration (idempotent parallel upsert)
            created_instances: List[str] = []
            container_timings: List[dict] = []

            async def _deploy_single(c):
                c_t0 = time.perf_counter()
                full_inst_name = f"c-{team_k}-{c.name}"
                ip_addr = f"10.{team_k}.1.{c.ip_suffix}"
                gateway_ip = f"10.{team_k}.1.1"

                existing_container = await self.db.get_container(full_inst_name)
                if existing_container:
                    logger.info(
                        "Container %s is already running for team %d, reusing it",
                        full_inst_name,
                        team_k,
                    )
                    return None

                dev_map = devices.nic_bridge(bridge_name, dev_name="eth0")
                extra_config = {
                    "user.blueagent.team_k": str(team_k),
                    "user.blueagent.spec_name": c.name,
                }

                spec_copy = c.model_copy(update={"name": full_inst_name})
                logger.info("Creating container %s on bridge %s", full_inst_name, bridge_name)
                t_create = time.perf_counter()
                await self.engine.create_instance(spec_copy, devices=dev_map, extra_config=extra_config)
                create_sec = time.perf_counter() - t_create
                created_instances.append(full_inst_name)

                # Inject network configurations
                t_push = time.perf_counter()
                networkd_conf = render_networkd_conf(ip=ip_addr, gateway=gateway_ip)
                await self.engine.push_file(
                    full_inst_name,
                    "/etc/systemd/network/10-eth0.network",
                    networkd_conf,
                    mode="0644",
                )

                interfaces_conf = render_interfaces_conf(ip=ip_addr, gateway=gateway_ip)
                await self.engine.push_file(
                    full_inst_name,
                    "/etc/network/interfaces",
                    interfaces_conf,
                    mode="0644",
                )

                await self.engine.push_file(
                    full_inst_name,
                    "/etc/hostname",
                    f"{c.name}\n",
                    mode="0644",
                )
                push_sec = time.perf_counter() - t_push

                # Start instance
                logger.info("Starting container %s", full_inst_name)
                t_start = time.perf_counter()
                await self.engine.start_instance(full_inst_name)
                start_sec = time.perf_counter() - t_start

                t_net = time.perf_counter()
                try:
                    await self.engine.exec_command(full_inst_name, ip.addr_replace(f"{ip_addr}/24", "eth0"), capture_output=False)
                    await self.engine.exec_command(full_inst_name, ip.link_up("eth0"), capture_output=False)
                    await self.engine.exec_command(full_inst_name, ip.route_replace_default(gateway_ip, "eth0"), capture_output=False)
                except Exception as net_err:
                    logger.warning("Could not apply immediate runtime IP to %s: %s", full_inst_name, net_err)
                runtime_net_sec = time.perf_counter() - t_net

                t_db = time.perf_counter()
                await self.db.record_container(
                    instance_name=full_inst_name,
                    team_k=team_k,
                    spec_name=c.name,
                    ip_address=ip_addr,
                    image_alias=c.image,
                )
                db_record_sec = time.perf_counter() - t_db

                return {
                    "container": c.name,
                    "create_sec": round(create_sec, 4),
                    "push_sec": round(push_sec, 4),
                    "start_sec": round(start_sec, 4),
                    "runtime_net_sec": round(runtime_net_sec, 4),
                    "db_record_sec": round(db_record_sec, 4),
                    "total_container_sec": round(time.perf_counter() - c_t0, 4),
                }

            try:
                if config.containers:
                    results = await asyncio.gather(*[_deploy_single(c) for c in config.containers])
                    container_timings = [r for r in results if r is not None]

                team_containers = await self.db.list_team_containers(team_k)
                now_dt = datetime.now(timezone.utc)
                deploy_total_sec = time.perf_counter() - t_deploy_start

                step_timings = {
                    "validation_sec": round(validation_sec, 4),
                    "total_create_sec": round(sum(ct["create_sec"] for ct in container_timings), 4),
                    "total_push_sec": round(sum(ct["push_sec"] for ct in container_timings), 4),
                    "total_start_sec": round(sum(ct["start_sec"] for ct in container_timings), 4),
                    "total_runtime_net_sec": round(sum(ct["runtime_net_sec"] for ct in container_timings), 4),
                    "total_db_record_sec": round(sum(ct["db_record_sec"] for ct in container_timings), 4),
                    "total_deploy_sec": round(deploy_total_sec, 4),
                    "container_details": container_timings,
                }

                return DeployResponse(
                    team_k=team_k,
                    challenge_id=config.challenge_id,
                    status="running",
                    config_json=config.model_dump_json(),
                    deployed_at=now_dt,
                    updated_at=now_dt,
                    containers=team_containers,
                    step_timings=step_timings,
                )

            except Exception as deploy_err:
                logger.exception(
                    "Challenge '%s' deployment failed on team %d, performing atomic rollback: %s",
                    config.challenge_id,
                    team_k,
                    deploy_err,
                )
                rollback_errors = []
                for name in reversed(created_instances):
                    try:
                        logger.info("Rolling back instance '%s'", name)
                        await self.engine.delete_instance(name, force=True)
                        await self.db.delete_container(name)
                    except Exception as rb_exc:
                        rollback_errors.append(f"{name}: {rb_exc}")

                if not rollback_errors:
                    raise DeploymentRollbackError(
                        f"Challenge deployment failed and was rolled back: {deploy_err}"
                    ) from deploy_err
                else:
                    raise DeploymentFailedError(
                        f"Challenge deployment failed and rollback encountered errors: {deploy_err}"
                    ) from deploy_err

    async def teardown_challenge(
        self,
        team_k: int,
        challenge_id: Optional[str] = "ch",
        container_names: Optional[List[str]] = None,
    ) -> None:
        """Tears down containers for team k within the team lock."""
        async with self.locks.lock_team(team_k):
            team = await self.db.get_team_by_k(team_k)
            if not team:
                raise TeamNotFoundError(f"Team {team_k} does not exist")
            await teardown_challenge(
                challenge_id=challenge_id or "ch",
                team_k=team_k,
                db=self.db,
                engine=self.engine,
                container_names=container_names,
            )

    async def get_active_containers(self, team_k: int) -> List[RunningContainerRecord]:
        """Lists active containers for a team."""
        team = await self.db.get_team_by_k(team_k)
        if not team:
            raise TeamNotFoundError(f"Team {team_k} does not exist")
        return await self.db.list_team_containers(team_k)

    async def verify_challenge(self, team_k: int, config: ChallengeConfig) -> VerifyResultResp:
        """Executes verification against dark OVN sandbox clones with readiness checks."""
        config = config.render_for_team(team_k)
        try:
            async with self.locks.acquire("verify", str(team_k), timeout=0.01):
                async with self.locks.lock_team(team_k, timeout=10.0):
                    team = await self.db.get_team_by_k(team_k)
                    if not team:
                        raise TeamNotFoundError(f"Team {team_k} does not exist")

                    session_id = str(team_k)
                    verifier = OVNVerifier(self.engine, config, session_id, team_k=team_k)
                    await verifier.__aenter__()

                result = None
                try:
                    result = await verifier.run_checks()
                finally:
                    await verifier.__aexit__(None, None, None)
                if result is not None:
                    if result.details is None:
                        result.details = {}
                    result.details["step_timings"] = verifier.step_timings
                return result
        except LockTimeoutError as lock_err:
            raise VerificationLockedError(
                f"Verification already in progress or team resource locked for team {team_k}"
            ) from lock_err
