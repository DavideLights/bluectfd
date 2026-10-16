"""Service layer for team provisioning, WireGuard credentials, and member lifecycle."""

import logging
import sqlite3
import time
from typing import List, Optional, Tuple, Union

from ..database import DatabaseManager
from ..engine.base import ContainerEngine
from ..locks import LockRegistry
from ..models import PeerStats, PeerTelemetry, TeamMemberResp, TeamMemberWG, TeamRecord
from ..network.addressing import allocate_member_ip
from ..network.host_net import HostNetworkManager
from ..network.wireguard import WireGuardManager, generate_keypair, generate_preshared_key

logger = logging.getLogger(__name__)


class TeamServiceError(Exception):
    """Base exception for team service operations."""


class TeamNotFoundError(TeamServiceError):
    """Raised when the specified team does not exist."""


class TeamNetworkError(TeamServiceError):
    """Raised when network interface setup fails."""


class TeamDatabaseError(TeamServiceError):
    """Raised when persistence fails during team provisioning."""


class MemberNotFoundError(TeamServiceError):
    """Raised when a team member is not found."""


class MemberAlreadyAssignedError(TeamServiceError):
    """Raised when a user is already assigned to a different team."""


class MemberConflictError(TeamServiceError):
    """Raised when a unique constraint or conflict occurs while adding a member."""


class TeamAddressSpaceExhaustedError(TeamServiceError):
    """Raised when the team subnet has reached maximum capacity (253 members)."""


class TeamService:
    """Orchestrates team bridges, WireGuard network configurations, and member credentials."""

    def __init__(
        self,
        db: DatabaseManager,
        host_net: HostNetworkManager,
        wg: WireGuardManager,
        engine: ContainerEngine,
        locks: LockRegistry,
    ):
        self.db = db
        self.host_net = host_net
        self.wg = wg
        self.engine = engine
        self.locks = locks

    async def provision_team_network(
        self,
        team_k: int,
        name: str,
    ) -> Tuple[TeamRecord, bool]:
        """Provisions Linux bridge and WireGuard interface idempotently."""
        t_start = time.perf_counter()
        # 1. Check existing by team_k
        t0 = time.perf_counter()
        existing_by_k = await self.db.get_team_by_k(team_k)
        db_check_sec = time.perf_counter() - t0
        if existing_by_k:
            if name and existing_by_k.name != name:
                existing_by_k = await self.db.update_team_name(team_k, name) or existing_by_k
            # Ensure network state is active
            await self.host_net.setup_team_bridge(team_k)
            await self.wg.setup_team_interface(team_k)
            existing_by_k.step_timings = {
                "db_check_sec": round(db_check_sec, 4),
                "total_team_setup_sec": round(time.perf_counter() - t_start, 4),
            }
            return existing_by_k, False

        # 2. Create host bridge
        t0 = time.perf_counter()
        br_name = await self.host_net.setup_team_bridge(team_k)
        bridge_setup_sec = time.perf_counter() - t0

        # 3. Create WireGuard interface
        t0 = time.perf_counter()
        try:
            wg_name = await self.wg.setup_team_interface(team_k)
        except Exception as err:
            logger.error("Failed setting up WireGuard interface for team %d: %s", team_k, err)
            await self.host_net.teardown_team_bridge(team_k)
            raise TeamNetworkError(f"Failed to setup WireGuard interface: {err}") from err
        wg_setup_sec = time.perf_counter() - t0

        # 4. Persist team record
        t0 = time.perf_counter()
        try:
            team = await self.db.create_team(
                team_k=team_k,
                name=name,
                bridge_name=br_name,
                wg_interface=wg_name,
            )
            db_persist_sec = time.perf_counter() - t0
            team.step_timings = {
                "db_check_sec": round(db_check_sec, 4),
                "bridge_setup_sec": round(bridge_setup_sec, 4),
                "wg_setup_sec": round(wg_setup_sec, 4),
                "db_persist_sec": round(db_persist_sec, 4),
                "total_team_setup_sec": round(time.perf_counter() - t_start, 4),
            }
            return team, True
        except Exception as err:
            logger.error("Database failure creating team %d: %s", team_k, err)
            await self.wg.teardown_team_interface(team_k)
            await self.host_net.teardown_team_bridge(team_k)
            raise TeamDatabaseError(f"Database error while saving team: {err}") from err

    async def create_or_update_team(self, team_k: int, name: str) -> Tuple[TeamRecord, bool]:
        """Creates or updates team environment within lock."""
        async with self.locks.lock_team(team_k):
            return await self.provision_team_network(team_k=team_k, name=name)

    async def create_team(self, team_k: int, name: str) -> Tuple[TeamRecord, bool]:
        """Alias for create_or_update_team for backward compatibility."""
        return await self.create_or_update_team(team_k=team_k, name=name)

    async def list_teams(self) -> List[TeamRecord]:
        """Lists all registered teams."""
        return await self.db.list_teams()

    async def get_team(self, team_k: int) -> Optional[TeamRecord]:
        """Retrieves metadata for a specific team."""
        return await self.db.get_team_by_k(team_k)

    async def teardown_team(self, team_k: int) -> None:
        """Tears down all active containers, network namespaces, and team record."""
        async with self.locks.lock_team(team_k):
            # 1. Teardown all containers for this team
            containers = await self.db.list_team_containers(team_k)
            for c in containers:
                try:
                    await self.engine.stop_instance(c.instance_name, force=True)
                    await self.engine.delete_instance(c.instance_name, force=True)
                except Exception as err:
                    logger.warning("Error deleting container %s: %s", c.instance_name, err)
            await self.db.delete_all_team_containers(team_k)

            # 2. Teardown WireGuard interface and host bridge
            await self.wg.teardown_team_interface(team_k)
            await self.host_net.teardown_team_bridge(team_k)

            # 3. Delete team from database
            await self.db.delete_team(team_k)

    async def add_member(
        self,
        team_k: int,
        user_id: Union[int, str],
        name: str,
        public_key: Optional[str] = None,
    ) -> Tuple[TeamMemberWG, bool]:
        """Provisions or updates a team member's WireGuard configuration."""
        async with self.locks.lock_team(team_k):
            # 1. Resolve team - Must exist!
            team = await self.db.get_team_by_k(team_k)
            if not team:
                raise TeamNotFoundError(f"Team {team_k} not found")

            # 2. Check if member already exists (Idempotent update handling)
            member_id = f"ctfd-{team_k}-{user_id}"
            ctfd_user_id = int(user_id) if str(user_id).isdigit() else None
            existing_member = await self.db.get_team_member(str(user_id), team_k=team_k)
            if existing_member:
                if existing_member.team_k != team_k:
                    raise MemberAlreadyAssignedError(
                        f"User {user_id} already assigned to team {existing_member.team_k}. Must be deleted before switching teams."
                    )
                # Check if public key was updated
                key_updated = public_key is not None and public_key != existing_member.public_key
                if key_updated:
                    await self.wg.remove_peer(team_k, existing_member.public_key)
                    await self.wg.add_peer(
                        team_k=team_k,
                        member_id=existing_member.member_id,
                        public_key=public_key,
                        allowed_ips=existing_member.client_ip,
                        preshared_key=existing_member.preshared_key,
                    )
                    updated = await self.db.update_team_member(
                        member_id=existing_member.member_id,
                        name=name,
                        public_key=public_key,
                        private_key=None,
                    )
                    if updated:
                        existing_member = updated
                else:
                    if name != existing_member.name:
                        updated = await self.db.update_team_member(
                            member_id=existing_member.member_id,
                            name=name,
                        )
                        if updated:
                            existing_member = updated
                    # Re-add peer to WireGuard in case of daemon restart
                    await self.wg.add_peer(
                        team_k=team_k,
                        member_id=existing_member.member_id,
                        public_key=existing_member.public_key,
                        allowed_ips=existing_member.client_ip,
                        preshared_key=existing_member.preshared_key,
                    )

                creds = await self.wg.generate_member_config(
                    team_k=team_k,
                    member_name=existing_member.name,
                    client_ip=existing_member.client_ip,
                    public_key=existing_member.public_key,
                    preshared_key=existing_member.preshared_key,
                    private_key=existing_member.private_key,
                )
                existing_member.config_text = creds.config_text
                return existing_member, False

            # Check if user is already assigned to a different team
            other_member = await self.db.get_team_member(str(user_id))
            if other_member and other_member.team_k != team_k:
                raise MemberAlreadyAssignedError(
                    f"User {user_id} already assigned to team {other_member.team_k}. Must be deleted before switching teams."
                )

            t_mem_start = time.perf_counter()
            # 3. Allocate next available IP on 10.k.0.0/24
            t0 = time.perf_counter()
            used_ips = await self.db.list_used_ips(team_k)
            if len(used_ips) >= 253:
                raise TeamAddressSpaceExhaustedError(
                    f"Team {team_k} address space exhausted (maximum 253 members)"
                )
            try:
                client_ip = allocate_member_ip(team_k, used_ips)
            except ValueError as err:
                raise TeamAddressSpaceExhaustedError(str(err)) from err
            ip_alloc_sec = time.perf_counter() - t0

            # 4. Generate keys & client configuration
            t0 = time.perf_counter()
            if public_key:
                priv_key = None
                pub_key = public_key
            else:
                priv_key, pub_key = generate_keypair()
            psk = generate_preshared_key()

            creds = await self.wg.generate_member_config(
                team_k=team_k,
                member_name=name,
                client_ip=client_ip,
                public_key=pub_key,
                preshared_key=psk,
                private_key=priv_key,
            )
            keypair_gen_sec = time.perf_counter() - t0

            # 5. Add peer to WireGuard interface
            t0 = time.perf_counter()
            await self.wg.add_peer(
                team_k=team_k,
                member_id=member_id,
                public_key=pub_key,
                allowed_ips=client_ip,
                preshared_key=psk,
            )
            wg_peer_sec = time.perf_counter() - t0

            # 6. Save member record in database
            t0 = time.perf_counter()
            try:
                member = await self.db.add_team_member(
                    member_id=member_id,
                    team_k=team_k,
                    name=name,
                    client_ip=client_ip,
                    public_key=pub_key,
                    ctfd_user_id=ctfd_user_id,
                    private_key=priv_key,
                    preshared_key=psk,
                    config_path=None,
                )
            except Exception as err:
                logger.error("Failed storing team member in database: %s; rolling back peer", err)
                await self.wg.remove_peer(team_k, pub_key)
                if isinstance(err, sqlite3.IntegrityError) or "UNIQUE constraint" in str(err):
                    raise MemberConflictError(
                        f"User {user_id} already assigned or address conflict: {err}"
                    ) from err
                raise TeamDatabaseError(f"Database failure storing member: {err}") from err
            db_save_sec = time.perf_counter() - t0

            member.config_text = creds.config_text
            member.step_timings = {
                "ip_alloc_sec": round(ip_alloc_sec, 4),
                "keypair_gen_sec": round(keypair_gen_sec, 4),
                "wg_peer_sec": round(wg_peer_sec, 4),
                "db_save_sec": round(db_save_sec, 4),
                "total_member_sec": round(time.perf_counter() - t_mem_start, 4),
            }
            return member, True

    async def list_members(self, team_k: int) -> List[TeamMemberResp]:
        """Lists all WireGuard client peers registered for a team with real-time telemetry."""
        team = await self.db.get_team_by_k(team_k)
        if not team:
            raise TeamNotFoundError(f"Team {team_k} not found")

        members = await self.db.list_team_members(team_k)
        try:
            active_peers = await self.wg.list_peers(team_k)
        except Exception as err:
            logger.warning("Could not fetch WireGuard peer stats for team %d: %s", team_k, err)
            active_peers = {}

        now_ts = int(time.time())
        resp_list: List[TeamMemberResp] = []
        for m in members:
            p_stat = active_peers.get(m.public_key, {})
            latest_hs = p_stat.get("latest_handshake", 0)
            online = (now_ts - latest_hs <= 180) if latest_hs > 0 else False
            telemetry = PeerTelemetry(
                online=online,
                endpoint=p_stat.get("endpoint") if p_stat.get("endpoint") != "(none)" else None,
                latest_handshake=latest_hs,
                transfer_rx=p_stat.get("transfer_rx", 0),
                transfer_tx=p_stat.get("transfer_tx", 0),
                persistent_keepalive=p_stat.get("persistent_keepalive", "off"),
            )
            resp_list.append(
                TeamMemberResp(
                    user_id=m.ctfd_user_id if m.ctfd_user_id is not None else m.member_id,
                    member_id=m.member_id,
                    team_k=m.team_k,
                    name=m.name,
                    client_ip=m.client_ip,
                    public_key=m.public_key,
                    created_at=m.created_at,
                    telemetry=telemetry,
                )
            )
        return resp_list

    async def get_member(self, team_k: int, member_id: Union[int, str]) -> TeamMemberResp:
        """Retrieves metadata and telemetry for a specific team member."""
        team = await self.db.get_team_by_k(team_k)
        if not team:
            raise TeamNotFoundError(f"Team {team_k} not found")

        member = await self.db.get_team_member(str(member_id), team_k=team_k)
        if not member or member.team_k != team_k:
            raise MemberNotFoundError(f"Member {member_id} not found on team {team_k}")

        try:
            active_peers = await self.wg.list_peers(team_k)
        except Exception:
            active_peers = {}

        now_ts = int(time.time())
        p_stat = active_peers.get(member.public_key, {})
        latest_hs = p_stat.get("latest_handshake", 0)
        online = (now_ts - latest_hs <= 180) if latest_hs > 0 else False
        telemetry = PeerTelemetry(
            online=online,
            endpoint=p_stat.get("endpoint") if p_stat.get("endpoint") != "(none)" else None,
            latest_handshake=latest_hs,
            transfer_rx=p_stat.get("transfer_rx", 0),
            transfer_tx=p_stat.get("transfer_tx", 0),
            persistent_keepalive=p_stat.get("persistent_keepalive", "off"),
        )
        return TeamMemberResp(
            user_id=member.ctfd_user_id if member.ctfd_user_id is not None else member.member_id,
            member_id=member.member_id,
            team_k=member.team_k,
            name=member.name,
            client_ip=member.client_ip,
            public_key=member.public_key,
            created_at=member.created_at,
            telemetry=telemetry,
        )

    async def get_member_config(self, team_k: int, member_id: Union[int, str]) -> str:
        """Retrieves full WireGuard .conf file text for a member."""
        team = await self.db.get_team_by_k(team_k)
        if not team:
            raise TeamNotFoundError(f"Team {team_k} not found")

        member = await self.db.get_team_member(str(member_id), team_k=team_k)
        if not member or member.team_k != team_k:
            raise MemberNotFoundError(f"Member {member_id} not found on team {team_k}")

        creds = await self.wg.generate_member_config(
            team_k=team_k,
            member_name=member.name,
            client_ip=member.client_ip,
            public_key=member.public_key,
            preshared_key=member.preshared_key,
            private_key=member.private_key,
        )
        return creds.config_text

    async def remove_member(self, team_k: int, member_id: Union[int, str]) -> None:
        """Removes a member from WireGuard and frees their assigned IP."""
        async with self.locks.lock_team(team_k):
            team = await self.db.get_team_by_k(team_k)
            if not team:
                raise TeamNotFoundError(f"Team {team_k} not found")

            member = await self.db.get_team_member(str(member_id), team_k=team_k)
            if not member or member.team_k != team_k:
                raise MemberNotFoundError(f"Member {member_id} not found on team {team_k}")

            await self.wg.remove_peer(team_k, member.public_key)
            await self.db.delete_team_member_by_team(team_k, member.member_id)

    async def get_peer_stats(self, team_k: int) -> List[PeerStats]:
        """Returns active WireGuard peers and telemetry for a team."""
        team = await self.db.get_team_by_k(team_k)
        if not team:
            raise TeamNotFoundError(f"Team {team_k} not found")

        members = await self.db.list_team_members(team_k)
        active_peers = await self.wg.list_peers(team_k)
        now_ts = int(time.time())

        stats_list: List[PeerStats] = []
        for m in members:
            p_stat = active_peers.get(m.public_key, {})
            latest_hs = p_stat.get("latest_handshake", 0)
            online = (now_ts - latest_hs <= 180) if latest_hs > 0 else False
            stats_list.append(
                PeerStats(
                    member_id=m.member_id,
                    ctfd_user_id=m.ctfd_user_id,
                    name=m.name,
                    client_ip=m.client_ip,
                    public_key=m.public_key,
                    endpoint=p_stat.get("endpoint") if p_stat.get("endpoint") != "(none)" else None,
                    allowed_ips=p_stat.get("allowed_ips", f"{m.client_ip}/32"),
                    latest_handshake=latest_hs,
                    transfer_rx=p_stat.get("transfer_rx", 0),
                    transfer_tx=p_stat.get("transfer_tx", 0),
                    persistent_keepalive=p_stat.get("persistent_keepalive", "off"),
                    online=online,
                )
            )
        return stats_list
