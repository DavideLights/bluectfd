"""Pure addressing module and network topology planner for BlueAgent v2.

Acts as the single source of truth for deterministic IP subnets, interface names,
and multi-member VPN address allocations across the platform.
"""

import ipaddress
from dataclasses import dataclass
from typing import Iterable, Set

from ..config import settings


@dataclass(frozen=True)
class TeamNetPlan:
    """Deterministic network blueprint and address assignments for team k."""

    team_k: int
    bridge_name: str
    bridge_ip: str
    bridge_cidr: str
    lan_subnet: str
    wg_iface: str
    wg_server_ip: str
    wg_server_cidr: str
    vpn_subnet: str
    listen_port: int


def plan_for(
    team_k: int,
    bridge_prefix: str = settings.host_bridge_prefix,
    wg_prefix: str = settings.wg_interface_prefix,
    base_port: int = settings.wg_base_port,
) -> TeamNetPlan:
    """Calculates deterministic network topology and subnet plans for team k.

    Args:
        team_k: Team identifier index integer (0 <= team_k <= 254).
        bridge_prefix: Interface naming prefix for Linux bridges (default: 'br-team-').
        wg_prefix: Interface naming prefix for WireGuard interfaces (default: 'wg-').
        base_port: Base UDP port offset for WireGuard (default: 51820).

    Returns:
        TeamNetPlan populated with calculated IPs, CIDRs, and port offsets.

    Raises:
        ValueError: If team_k is outside the supported octet range [0, 254].
    """
    if not (0 <= team_k <= 254):
        raise ValueError(f"Team index k must be between 0 and 254, got {team_k}")

    bridge_name = f"{bridge_prefix}{team_k}"
    bridge_ip = f"10.{team_k}.1.1"
    bridge_cidr = f"10.{team_k}.1.1/24"
    lan_subnet = f"10.{team_k}.1.0/24"

    wg_iface = f"{wg_prefix}{team_k}"
    wg_server_ip = f"10.{team_k}.0.1"
    wg_server_cidr = f"10.{team_k}.0.1/24"
    vpn_subnet = f"10.{team_k}.0.0/24"
    listen_port = base_port + team_k

    return TeamNetPlan(
        team_k=team_k,
        bridge_name=bridge_name,
        bridge_ip=bridge_ip,
        bridge_cidr=bridge_cidr,
        lan_subnet=lan_subnet,
        wg_iface=wg_iface,
        wg_server_ip=wg_server_ip,
        wg_server_cidr=wg_server_cidr,
        vpn_subnet=vpn_subnet,
        listen_port=listen_port,
    )


def allocate_member_ip(team_k: int, used_ips: Iterable[str]) -> str:
    """Allocates the next available client IP address within the team VPN subnet (10.k.0.0/24).

    Host byte allocations start at .2 (.1 is reserved for the host WireGuard gateway,
    and .255 is the broadcast address).

    Args:
        team_k: Team identifier index integer (0 <= team_k <= 254).
        used_ips: Collection of already allocated IP addresses (with or without CIDR mask).

    Returns:
        The next unallocated IPv4 address string (e.g. '10.1.0.2').

    Raises:
        ValueError: If all 253 host addresses in the /24 subnet are exhausted.
    """
    plan = plan_for(team_k)
    cleaned_used: Set[str] = set()

    for item in used_ips:
        if not item:
            continue
        cleaned = item.split("/")[0].strip()
        cleaned_used.add(cleaned)

    # .1 is reserved for the host endpoint
    cleaned_used.add(plan.wg_server_ip)

    # Search range .2 to .254
    for host_byte in range(2, 255):
        candidate = f"10.{team_k}.0.{host_byte}"
        if candidate not in cleaned_used:
            return candidate

    raise ValueError(f"Exhausted all available VPN IP addresses in subnet {plan.vpn_subnet}")
