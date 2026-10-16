"""Host networking, Linux bridge, nftables, and WireGuard subsystems for BlueAgent v2."""

from ._runner import CommandRunner, CompletedCommand, HostCommandError, SubprocessRunner
from .addressing import TeamNetPlan, allocate_member_ip, plan_for
from .host_net import HostNetworkManager
from .wireguard import MemberCredentials, WireGuardManager

__all__ = [
    "CommandRunner",
    "CompletedCommand",
    "HostCommandError",
    "SubprocessRunner",
    "TeamNetPlan",
    "allocate_member_ip",
    "plan_for",
    "HostNetworkManager",
    "WireGuardManager",
    "MemberCredentials",
]
