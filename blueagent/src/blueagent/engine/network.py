"""Specialized OVN network management for Incus container runtime.

Handles creation, configuration, query, and destruction of isolated
Open Virtual Network (OVN) Software-Defined Networking (SDN) logical switches.
"""

import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional
import httpx

from ..config import settings

logger = logging.getLogger(__name__)


class OvnNetworkManager:
    """Specialized controller for managing OVN logical switches via Incus REST API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        wait_op_fn: Callable[[str, int], Coroutine[Any, Any, Dict[str, Any]]],
    ):
        self.client = client
        self._wait_op = wait_op_fn

    async def create_ovn_network(
        self,
        name: str,
        subnet_cidr: str,
        uplink_network: str = settings.incus_uplink_network,
        enable_nat: bool = False,
        extra_config: Optional[Dict[str, str]] = None,
        exist_ok: bool = True,
    ) -> Dict[str, Any]:
        """Provisions an isolated OVN logical network linked to the uplink bridge.

        Args:
            name: The unique network name (e.g. 'verify-session-uuid').
            subnet_cidr: The deterministic IPv4 CIDR (e.g. '10.1.1.1/24').
            uplink_network: The parent bridge for external connectivity (default: incusbr0).
            enable_nat: Whether to enable outbound NAT (for public internet access).
            extra_config: Additional Incus network configuration key-values.
            exist_ok: If True, returns existing network metadata instead of raising conflict.

        Returns:
            The created or existing network metadata dictionary.
        """
        config: Dict[str, Any] = {
            "ipv4.address": subnet_cidr,
        }
        if uplink_network:
            config["network"] = uplink_network
            config["ipv4.nat"] = "true" if enable_nat else "false"
        if extra_config:
            config.update(extra_config)
            if not uplink_network and not extra_config.get("network") and "network" in config:
                del config["network"]

        payload = {
            "name": name,
            "type": "ovn",
            "config": config,
        }

        # Ensure parent uplink network has ipv4.routes allowing the subnet
        if uplink_network:
            try:
                uplink_meta = await self.get_network(uplink_network)
                if uplink_meta:
                    curr_routes = uplink_meta.get("config", {}).get("ipv4.routes", "")
                    if "10.0.0.0/8" not in curr_routes:
                        new_routes = f"{curr_routes},10.0.0.0/8".strip(",") if curr_routes else "10.0.0.0/8"
                        await self.client.patch(f"networks/{uplink_network}", json={"config": {"ipv4.routes": new_routes}})
            except Exception as route_err:
                logger.debug("Could not verify/patch routes on uplink network '%s': %s", uplink_network, route_err)

        res = await self.client.post("networks", json=payload)
        if res.status_code in (400, 409) and "already exists" in res.text.lower():
            if exist_ok:
                logger.debug("Network '%s' already exists; returning existing configuration.", name)
                existing = await self.get_network(name)
                if existing:
                    return existing
            raise RuntimeError(f"Network '{name}' already exists: {res.text}")

        if res.status_code >= 400:
            raise RuntimeError(f"Failed to create OVN network '{name}' ({res.status_code}): {res.text}")

        body = res.json()
        op = body.get("operation")
        if op:
            await self._wait_op(op, settings.incus_operation_timeout)

        return body.get("metadata", {})

    async def delete_ovn_network(self, name: str) -> None:
        """Deletes an OVN logical network once all ports have been detached.

        Args:
            name: The network identifier to remove.

        Raises:
            RuntimeError: If network is currently in use or cannot be deleted.
        """
        res = await self.client.delete(f"networks/{name}")
        if res.status_code == 404:
            return  # Idempotent delete

        if res.status_code in (400, 409) and "used by" in res.text.lower():
            raise RuntimeError(f"Cannot delete OVN network '{name}': network is still in use by instances.")

        res.raise_for_status()
        op = res.json().get("operation")
        if op:
            await self._wait_op(op, settings.incus_operation_timeout)

    async def get_network(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves network details and configuration metadata.

        Returns:
            Network dictionary if found, or None if HTTP 404.
        """
        res = await self.client.get(f"networks/{name}")
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json().get("metadata", {})

    async def list_networks(self, full: bool = False) -> List[Any]:
        """Lists network resource paths or detailed objects present in Incus.

        Args:
            full: If True, fetches full metadata dictionaries (recursion=1).
        """
        params = {"recursion": 1} if full else {}
        res = await self.client.get("networks", params=params)
        res.raise_for_status()
        return res.json().get("metadata", [])

    async def network_exists(self, name: str) -> bool:
        """Checks if a network exists."""
        net = await self.get_network(name)
        return net is not None

    async def list_verification_networks(self) -> List[str]:
        """Filters and returns network names created for verification sandboxes (verify-*)."""
        paths = await self.list_networks(full=False)
        net_names = [p.split("/")[-1] for p in paths]
        return [name for name in net_names if name.startswith("verify-")]

    async def get_network_leases(self, name: str) -> List[Dict[str, Any]]:
        """Retrieves active DHCP/static IP leases on the network."""
        res = await self.client.get(f"networks/{name}/leases")
        if res.status_code == 404:
            return []
        res.raise_for_status()
        return res.json().get("metadata", [])
