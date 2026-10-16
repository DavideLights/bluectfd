"""Linux host networking, bridge management, and nftables firewall manager.

Governs live production network namespaces, L2 bridges (br-team-k),
and TCP MSS clamping rules to mitigate PMTU discovery blackholes.
"""

import json
import logging
from typing import Optional

from ..config import Settings, settings
from ._runner import CommandRunner, SubprocessRunner
from .addressing import TeamNetPlan, plan_for
from .commands import ip, nft
from .templates import render_nftables_base

logger = logging.getLogger(__name__)


class HostNetworkManager:
    """Manages host-level Linux bridges, routing tables, and nftables rules.

    Adheres to desired-state reconciliation principles and executes OS commands
    via a pluggable CommandRunner interface for deterministic unit testing.
    """

    def __init__(
        self,
        bridge_prefix: str = settings.host_bridge_prefix,
        runner: Optional[CommandRunner] = None,
        config: Settings = settings,
    ):
        self.bridge_prefix = bridge_prefix
        self.runner = runner or SubprocessRunner()
        self.settings = config
        self.nft_table = config.nft_table

    async def setup_team_bridge(self, team_k: int, gateway_ip: Optional[str] = None) -> str:
        """Provisions a dedicated unmanaged Linux bridge for team k (br-team-k).

        Reconciles the bridge interface, sets the gateway IP address, activates
        the link, and registers team link pairings in the nftables filter set.

        Args:
            team_k: Team identifier index k.
            gateway_ip: Host gateway IP address (default: 10.k.1.1/24).

        Returns:
            The created bridge device name.
        """
        plan = plan_for(team_k, bridge_prefix=self.bridge_prefix)
        target_ip = gateway_ip if gateway_ip else plan.bridge_cidr
        if "/" not in target_ip:
            target_ip = f"{target_ip}/24"

        logger.info("Setting up host bridge '%s' with IP %s for team %d", plan.bridge_name, target_ip, team_k)

        # 1. Check if interface exists
        check = await self.runner.run(
            ip.link_show(plan.bridge_name, json=True),
            check=False,
        )

        if check.returncode != 0:
            logger.debug("Bridge '%s' does not exist; creating bridge interface", plan.bridge_name)
            await self.runner.run(
                ip.link_add_bridge(plan.bridge_name),
                check=True,
            )

        # 2. Assign/Replace Gateway IP address idempotently
        await self.runner.run(
            ip.addr_replace(target_ip, plan.bridge_name),
            check=True,
        )

        # 3. Bring interface up
        await self.runner.run(
            ip.link_up(plan.bridge_name),
            check=True,
        )

        # 4. Register team forwarding pair in nftables set
        await self.register_team_links(team_k)

        return plan.bridge_name

    async def teardown_team_bridge(self, team_k: int) -> None:
        """Deletes the Linux bridge associated with team k and unregisters nftables elements."""
        plan = plan_for(team_k, bridge_prefix=self.bridge_prefix)
        logger.info("Tearing down host bridge '%s' for team %d", plan.bridge_name, team_k)

        # 1. Remove from nftables team_links set
        await self.unregister_team_links(team_k)

        # 2. Set link down
        await self.runner.run(
            ip.link_down(plan.bridge_name),
            check=False,
        )

        # 3. Delete bridge interface
        await self.runner.run(
            ip.link_delete(plan.bridge_name, dev_type="bridge"),
            check=False,
        )

    async def check_bridge_status(self, team_k: int) -> bool:
        """Checks if the bridge for team k is active and in UP state.

        Args:
            team_k: Team identifier index k.

        Returns:
            True if the bridge exists and is in an operational UP state, False otherwise.
        """
        plan = plan_for(team_k, bridge_prefix=self.bridge_prefix)
        res = await self.runner.run(
            ip.link_show(plan.bridge_name, json=True),
            check=False,
        )
        if res.returncode != 0:
            return False

        try:
            links = json.loads(res.stdout)
            if not links:
                return False
            link_info = links[0]
            flags = link_info.get("flags", [])
            operstate = link_info.get("operstate", "").upper()
            return "UP" in flags or operstate in ("UP", "UNKNOWN")
        except (ValueError, KeyError, IndexError):
            return False

    async def apply_nftables_rules(self) -> None:
        """Applies declarative MSS clamping, cross-team isolation, and NAT masquerade rules.

        Constructs an atomic nftables script installing the blueagent table skeleton
        with a dynamic named set ('team_links') matching authorized (wg-k, br-team-k) pairs.
        """
        table = self.nft_table
        logger.info("Applying atomic nftables skeleton for table 'inet %s'", table)

        batch_rules = render_nftables_base(table)

        await self.runner.run(
            nft.apply_batch(),
            stdin=batch_rules.encode("utf-8"),
            check=True,
        )

    async def register_team_links(self, team_k: int) -> None:
        """Authorizes bidirectional forwarding between wg-k and br-team-k in nftables."""
        plan = plan_for(team_k, bridge_prefix=self.bridge_prefix)
        table = self.nft_table
        logger.debug("Registering team %d links (%s <-> %s) in nftables", team_k, plan.wg_iface, plan.bridge_name)

        # Batch add both directions to team_links set
        cmd = nft.add_forward_pair(table, plan.wg_iface, plan.bridge_name)
        await self.runner.run(cmd, check=False)

    async def unregister_team_links(self, team_k: int) -> None:
        """Removes forwarding authorization for team k from nftables."""
        plan = plan_for(team_k, bridge_prefix=self.bridge_prefix)
        table = self.nft_table
        logger.debug("Unregistering team %d links from nftables", team_k)

        cmd = nft.del_forward_pair(table, plan.wg_iface, plan.bridge_name)
        await self.runner.run(cmd, check=False)

    async def ensure_ip_forwarding(self) -> bool:
        """Verifies that Linux kernel IPv4 forwarding is active."""
        res = await self.runner.run(["sysctl", "-n", "net.ipv4.ip_forward"], check=False)
        if res.returncode == 0 and res.stdout.strip() == "1":
            return True
        logger.warning("Kernel IPv4 forwarding is disabled (net.ipv4.ip_forward != 1)")
        return False
