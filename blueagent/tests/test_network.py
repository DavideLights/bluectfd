"""Comprehensive unit tests for the blueagent.network subsystem.

Covers:
- _runner: CommandRunner protocol, CompletedCommand, HostCommandError, SubprocessRunner
- addressing: plan_for, allocate_member_ip, TeamNetPlan
- host_net: HostNetworkManager (bridges, nftables, MSS clamping, sysctl)
- wireguard: WireGuardManager (interfaces, peers, keys, member configs)
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from unittest.mock import patch

import pytest

from blueagent.config import Settings
from blueagent.network._runner import (
    CommandRunner,
    CompletedCommand,
    HostCommandError,
    SubprocessRunner,
)
from blueagent.network.addressing import allocate_member_ip, plan_for
from blueagent.network.host_net import HostNetworkManager
from blueagent.network.wireguard import (
    MemberCredentials,
    WireGuardManager,
    generate_keypair,
    generate_preshared_key,
    public_key_from_private,
)


class FakeRunner(CommandRunner):
    """In-memory mock CommandRunner simulating kernel command execution without OS calls."""

    def __init__(self):
        self.calls: List[Tuple[Sequence[str], Optional[bytes]]] = []
        self.responses: Dict[str, CompletedCommand] = {}

    def set_response(
        self,
        command_prefix: str,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ):
        """Sets a pre-canned response when argv starts with the given prefix."""
        self.responses[command_prefix] = CompletedCommand(
            argv=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    async def run(
        self,
        argv: Sequence[str],
        *,
        stdin: Optional[bytes] = None,
        check: bool = True,
        timeout: float = 15.0,
    ) -> CompletedCommand:
        self.calls.append((list(argv), stdin))
        cmd_str = " ".join(argv)

        for prefix, resp in self.responses.items():
            if cmd_str.startswith(prefix):
                if check and resp.returncode != 0:
                    raise HostCommandError(argv, resp.returncode, resp.stdout, resp.stderr)
                return CompletedCommand(
                    argv=argv,
                    returncode=resp.returncode,
                    stdout=resp.stdout,
                    stderr=resp.stderr,
                )

        return CompletedCommand(argv=argv, returncode=0, stdout="", stderr="")


# ==============================================================================
# 1. _runner.py Tests
# ==============================================================================


def test_host_command_error_attributes_and_str():
    err = HostCommandError(["ip", "link"], returncode=2, stdout="out", stderr="err")
    assert err.argv == ["ip", "link"]
    assert err.returncode == 2
    assert err.stdout == "out"
    assert err.stderr == "err"
    assert "Host command 'ip link' failed with exit code 2" in str(err)
    assert "Stderr: err" in str(err)
    assert "Stdout: out" in str(err)


def test_completed_command_dataclass():
    cmd = CompletedCommand(argv=["echo", "hi"], returncode=0, stdout="hi\n", stderr="")
    assert cmd.argv == ["echo", "hi"]
    assert cmd.returncode == 0
    assert cmd.stdout == "hi\n"
    assert cmd.stderr == ""


@pytest.mark.asyncio
async def test_subprocess_runner_empty_argv_raises():
    runner = SubprocessRunner()
    with pytest.raises(ValueError, match="cannot be empty"):
        await runner.run([])


@pytest.mark.asyncio
async def test_subprocess_runner_real_command_success():
    runner = SubprocessRunner()
    res = await runner.run([sys.executable, "-c", "print('hello_blueagent')"])
    assert res.returncode == 0
    assert res.stdout.strip() == "hello_blueagent"
    assert res.stderr == ""


@pytest.mark.asyncio
async def test_subprocess_runner_real_command_stdin():
    runner = SubprocessRunner()
    res = await runner.run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper())"],
        stdin=b"test_payload",
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "TEST_PAYLOAD"


@pytest.mark.asyncio
async def test_subprocess_runner_real_command_check_false():
    runner = SubprocessRunner()
    res = await runner.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('fatal\\n'); sys.exit(7)"],
        check=False,
    )
    assert res.returncode == 7
    assert "fatal" in res.stderr


@pytest.mark.asyncio
async def test_subprocess_runner_real_command_check_true_raises():
    runner = SubprocessRunner()
    with pytest.raises(HostCommandError) as exc_info:
        await runner.run(
            [sys.executable, "-c", "import sys; sys.stderr.write('err_msg\\n'); sys.exit(3)"],
            check=True,
        )
    assert exc_info.value.returncode == 3
    assert "err_msg" in exc_info.value.stderr


@pytest.mark.asyncio
async def test_subprocess_runner_timeout():
    runner = SubprocessRunner()
    with pytest.raises(TimeoutError, match="timed out after"):
        await runner.run(
            [sys.executable, "-c", "import time; time.sleep(2.0)"],
            timeout=0.1,
        )


# ==============================================================================
# 2. addressing.py Tests
# ==============================================================================


def test_addressing_plan_for_boundaries():
    # Lower boundary: team_k = 0
    plan_0 = plan_for(0)
    assert plan_0.team_k == 0
    assert plan_0.bridge_name == "br-team-0"
    assert plan_0.bridge_ip == "10.0.1.1"
    assert plan_0.bridge_cidr == "10.0.1.1/24"
    assert plan_0.lan_subnet == "10.0.1.0/24"
    assert plan_0.wg_iface == "wg-0"
    assert plan_0.wg_server_ip == "10.0.0.1"
    assert plan_0.vpn_subnet == "10.0.0.0/24"
    assert plan_0.listen_port == 51820

    # Upper boundary: team_k = 254
    plan_254 = plan_for(254)
    assert plan_254.team_k == 254
    assert plan_254.bridge_name == "br-team-254"
    assert plan_254.bridge_ip == "10.254.1.1"
    assert plan_254.bridge_cidr == "10.254.1.1/24"
    assert plan_254.lan_subnet == "10.254.1.0/24"
    assert plan_254.wg_iface == "wg-254"
    assert plan_254.wg_server_ip == "10.254.0.1"
    assert plan_254.vpn_subnet == "10.254.0.0/24"
    assert plan_254.listen_port == 51820 + 254


def test_addressing_plan_for_custom_prefixes():
    plan = plan_for(5, bridge_prefix="custom-br-", wg_prefix="vpn-", base_port=60000)
    assert plan.bridge_name == "custom-br-5"
    assert plan.wg_iface == "vpn-5"
    assert plan.listen_port == 60005


def test_addressing_invalid_team_k():
    with pytest.raises(ValueError, match="between 0 and 254"):
        plan_for(-1)

    with pytest.raises(ValueError, match="between 0 and 254"):
        plan_for(255)

    with pytest.raises(ValueError, match="between 0 and 254"):
        plan_for(999)


def test_allocate_member_ip_sequential_and_gaps():
    # Empty list starts at .2 (.1 is gateway)
    assert allocate_member_ip(1, []) == "10.1.0.2"

    # .2 used -> returns .3
    assert allocate_member_ip(1, ["10.1.0.2/32"]) == "10.1.0.3"

    # Multiple used with CIDRs and bare IPs
    assert allocate_member_ip(1, ["10.1.0.2/32", "10.1.0.3"]) == "10.1.0.4"

    # Gaps in allocation: .2 and .4 used -> fills .3
    assert allocate_member_ip(1, ["10.1.0.2", "10.1.0.4"]) == "10.1.0.3"


def test_allocate_member_ip_dirty_inputs():
    # Handles empty strings and whitespace without error
    used = ["", "  ", "10.5.0.2/32", None, "10.5.0.3 "]  # type: ignore
    assert allocate_member_ip(5, [u for u in used if u is not None]) == "10.5.0.4"


def test_allocate_member_ip_exhaustion():
    # Range is .2 to .254 (253 addresses)
    all_ips = [f"10.1.0.{i}" for i in range(2, 255)]
    with pytest.raises(ValueError, match="Exhausted all available VPN IP addresses"):
        allocate_member_ip(1, all_ips)


# ==============================================================================
# 3. host_net.py Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_setup_team_bridge_new():
    runner = FakeRunner()
    runner.set_response("ip -json link show dev br-team-1", returncode=1, stderr="does not exist")

    manager = HostNetworkManager(runner=runner)
    br_name = await manager.setup_team_bridge(1)

    assert br_name == "br-team-1"
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("ip link add name br-team-1 type bridge" in cmd for cmd in executed_cmds)
    assert any("ip addr replace 10.1.1.1/24 dev br-team-1" in cmd for cmd in executed_cmds)
    assert any("ip link set dev br-team-1 up" in cmd for cmd in executed_cmds)
    assert any("nft add element inet blueagent team_links" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_setup_team_bridge_existing_idempotent():
    runner = FakeRunner()
    runner.set_response("ip -json link show dev br-team-1", returncode=0, stdout='[{"ifname":"br-team-1"}]')

    manager = HostNetworkManager(runner=runner)
    br_name = await manager.setup_team_bridge(1)

    assert br_name == "br-team-1"
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    # Should not call ip link add
    assert not any("ip link add" in cmd for cmd in executed_cmds)
    # Should still ensure address and UP state
    assert any("ip addr replace 10.1.1.1/24 dev br-team-1" in cmd for cmd in executed_cmds)
    assert any("ip link set dev br-team-1 up" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_setup_team_bridge_custom_gateway():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    # Custom IP without /24
    await manager.setup_team_bridge(1, gateway_ip="10.1.1.254")
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("ip addr replace 10.1.1.254/24 dev br-team-1" in cmd for cmd in executed_cmds)

    # Custom IP with /24
    runner.calls.clear()
    await manager.setup_team_bridge(1, gateway_ip="10.1.1.200/24")
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("ip addr replace 10.1.1.200/24 dev br-team-1" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_teardown_team_bridge():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    await manager.teardown_team_bridge(2)

    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("nft delete element inet blueagent team_links" in cmd for cmd in executed_cmds)
    assert any("ip link set dev br-team-2 down" in cmd for cmd in executed_cmds)
    assert any("ip link delete dev br-team-2 type bridge" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_check_bridge_status_various_states():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    # State: UP in flags
    runner.set_response(
        "ip -json link show dev br-team-1",
        stdout=json.dumps([{"ifname": "br-team-1", "flags": ["UP"], "operstate": "DOWN"}]),
    )
    assert await manager.check_bridge_status(1) is True

    # State: operstate is UP
    runner.set_response(
        "ip -json link show dev br-team-1",
        stdout=json.dumps([{"ifname": "br-team-1", "flags": [], "operstate": "UP"}]),
    )
    assert await manager.check_bridge_status(1) is True

    # State: operstate is UNKNOWN (standard Linux bridge behavior before ports attached)
    runner.set_response(
        "ip -json link show dev br-team-1",
        stdout=json.dumps([{"ifname": "br-team-1", "flags": [], "operstate": "UNKNOWN"}]),
    )
    assert await manager.check_bridge_status(1) is True

    # State: DOWN
    runner.set_response(
        "ip -json link show dev br-team-1",
        stdout=json.dumps([{"ifname": "br-team-1", "flags": ["BROADCAST"], "operstate": "DOWN"}]),
    )
    assert await manager.check_bridge_status(1) is False

    # State: Device not found
    runner.set_response("ip -json link show dev br-team-1", returncode=1)
    assert await manager.check_bridge_status(1) is False

    # State: Malformed or empty JSON
    runner.set_response("ip -json link show dev br-team-1", returncode=0, stdout="not-json")
    assert await manager.check_bridge_status(1) is False

    runner.set_response("ip -json link show dev br-team-1", returncode=0, stdout="[]")
    assert await manager.check_bridge_status(1) is False


@pytest.mark.asyncio
async def test_apply_nftables_rules():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    await manager.apply_nftables_rules()

    assert len(runner.calls) == 1
    argv, stdin = runner.calls[0]
    assert argv == ["nft", "-f", "-"]
    assert stdin is not None
    rules = stdin.decode("utf-8")
    assert "add table inet blueagent" in rules
    assert "add set inet blueagent team_links" in rules
    assert "maxseg size set rt mtu" in rules
    assert "@team_links accept" in rules
    assert "masquerade" in rules


@pytest.mark.asyncio
async def test_register_and_unregister_team_links():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    # Register
    await manager.register_team_links(3)
    reg_argv, _ = runner.calls[-1]
    assert reg_argv[0:4] == ["nft", "add", "element", "inet"]
    assert '{ "wg-3" . "br-team-3", "br-team-3" . "wg-3" }' in " ".join(reg_argv)

    # Unregister
    await manager.unregister_team_links(3)
    unreg_argv, _ = runner.calls[-1]
    assert unreg_argv[0:4] == ["nft", "delete", "element", "inet"]
    assert '{ "wg-3" . "br-team-3", "br-team-3" . "wg-3" }' in " ".join(unreg_argv)


@pytest.mark.asyncio
async def test_ensure_ip_forwarding():
    runner = FakeRunner()
    manager = HostNetworkManager(runner=runner)

    # Enabled
    runner.set_response("sysctl -n net.ipv4.ip_forward", returncode=0, stdout="1\n")
    assert await manager.ensure_ip_forwarding() is True

    # Disabled
    runner.set_response("sysctl -n net.ipv4.ip_forward", returncode=0, stdout="0\n")
    assert await manager.ensure_ip_forwarding() is False

    # Command error
    runner.set_response("sysctl -n net.ipv4.ip_forward", returncode=1, stderr="error")
    assert await manager.ensure_ip_forwarding() is False


# ==============================================================================
# 4. wireguard.py Tests
# ==============================================================================


def test_wireguard_keypair_generation_and_derivation():
    priv, pub = generate_keypair()
    assert len(priv) == 44
    assert len(pub) == 44

    derived = public_key_from_private(priv)
    assert derived == pub


def test_wireguard_preshared_key():
    psk = generate_preshared_key()
    assert len(psk) == 44


def test_ensure_server_private_key_persistence(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    # 1. New key creation with mode 0600
    priv1 = manager._ensure_server_private_key("wg-test")
    key_file = tmp_path / "wg-test.key"
    assert key_file.exists()
    assert (key_file.stat().st_mode & 0o777) == 0o600
    assert priv1 == key_file.read_text().strip()

    # 2. Existing key reuse
    priv2 = manager._ensure_server_private_key("wg-test")
    assert priv2 == priv1

    # 3. Existing empty key triggers new key generation
    key_file.write_text("")
    priv3 = manager._ensure_server_private_key("wg-test")
    assert priv3 != ""
    assert priv3 == key_file.read_text().strip()


@pytest.mark.asyncio
async def test_wireguard_setup_interface_new(tmp_path):
    runner = FakeRunner()
    runner.set_response("ip -json link show dev wg-1", returncode=1)

    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    iface = await manager.setup_team_interface(1)
    assert iface == "wg-1"

    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("ip link add dev wg-1 type wireguard" in cmd for cmd in executed_cmds)
    assert any("wg set wg-1 listen-port 51821 private-key /dev/stdin" in cmd for cmd in executed_cmds)
    assert any("ip addr replace 10.1.0.1/24 dev wg-1" in cmd for cmd in executed_cmds)
    assert any("ip link set dev wg-1 mtu 1420" in cmd for cmd in executed_cmds)
    assert any("ip link set dev wg-1 up" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_wireguard_setup_interface_idempotent(tmp_path):
    runner = FakeRunner()
    runner.set_response("ip -json link show dev wg-1", returncode=0, stdout='[{"ifname":"wg-1"}]')

    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    iface = await manager.setup_team_interface(1)
    assert iface == "wg-1"

    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert not any("ip link add" in cmd for cmd in executed_cmds)
    assert any("wg set wg-1 listen-port 51821" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_wireguard_setup_interface_custom_port(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    await manager.setup_team_interface(1, listen_port=59999)
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("wg set wg-1 listen-port 59999" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_wireguard_teardown_interface(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    key_file = tmp_path / "wg-2.key"
    key_file.write_text("test-key\n")

    manager = WireGuardManager(runner=runner, config=cfg)
    await manager.teardown_team_interface(2)

    assert not key_file.exists()
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    assert any("ip link set dev wg-2 down" in cmd for cmd in executed_cmds)
    assert any("ip link delete dev wg-2" in cmd for cmd in executed_cmds)


@pytest.mark.asyncio
async def test_wireguard_add_and_remove_peer(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    # 1. Add peer with PSK and raw IP (should normalize to /32)
    psk = generate_preshared_key()
    await manager.add_peer(
        team_k=1,
        member_id="m1",
        public_key="FakePublicKey1234567890=",
        allowed_ips="10.1.0.2",
        preshared_key=psk,
    )
    last_argv, last_stdin = runner.calls[-1]
    assert "wg set wg-1 peer FakePublicKey1234567890= allowed-ips 10.1.0.2/32 preshared-key /dev/stdin" == " ".join(last_argv)
    assert last_stdin == (psk + "\n").encode()

    # 2. Add peer without PSK
    await manager.add_peer(
        team_k=1,
        member_id="m2",
        public_key="AnotherPublicKey123456789=",
        allowed_ips="10.1.0.3/32",
        preshared_key=None,
    )
    last_argv, last_stdin = runner.calls[-1]
    assert "wg set wg-1 peer AnotherPublicKey123456789= allowed-ips 10.1.0.3/32" == " ".join(last_argv)
    assert last_stdin is None

    # 3. Remove peer
    await manager.remove_peer(1, "FakePublicKey1234567890=")
    last_argv, _ = runner.calls[-1]
    assert "wg set wg-1 peer FakePublicKey1234567890= remove" == " ".join(last_argv)


@pytest.mark.asyncio
async def test_wireguard_get_server_public_key(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    # Fast path: derived from file
    priv, pub = generate_keypair()
    key_file = tmp_path / "wg-1.key"
    key_file.write_text(priv + "\n")
    assert await manager.get_server_public_key(1) == pub

    # Fallback path: key file missing, runs wg show wg-2 public-key
    runner.set_response("wg show wg-2 public-key", stdout="KernelDerivedPubKey==\n")
    assert await manager.get_server_public_key(2) == "KernelDerivedPubKey=="


@pytest.mark.asyncio
async def test_wireguard_list_peers(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path))
    manager = WireGuardManager(runner=runner, config=cfg)

    dump_output = (
        "ServerPrivKey==\tServerPubKey==\t51820\t0\n"
        "PeerPub1==\tPeerPsk1==\t192.168.1.50:54321\t10.1.0.2/32\t1700000000\t1024\t2048\t25\n"
        "PeerPub2==\t(none)\t(none)\t10.1.0.3/32\tnot_a_num\t0\t0\toff\n"
    )
    runner.set_response("wg show wg-1 dump", stdout=dump_output)

    peers = await manager.list_peers(1)
    assert len(peers) == 2
    assert peers["PeerPub1=="]["allowed_ips"] == "10.1.0.2/32"
    assert peers["PeerPub1=="]["latest_handshake"] == 1700000000
    assert peers["PeerPub1=="]["transfer_rx"] == 1024
    # Non-digit fallback
    assert peers["PeerPub2=="]["latest_handshake"] == 0

    # Command failure returns empty dict
    runner.set_response("wg show wg-99 dump", returncode=1)
    assert await manager.list_peers(99) == {}


@pytest.mark.asyncio
async def test_wireguard_generate_member_config(tmp_path):
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path), wg_public_endpoint="vpn.blueagent.local")
    priv, pub = generate_keypair()
    key_file = tmp_path / "wg-1.key"
    key_file.write_text(priv + "\n")

    manager = WireGuardManager(runner=runner, config=cfg)

    # 1. Full auto key generation
    creds = await manager.generate_member_config(
        team_k=1,
        member_name="Alice",
        client_ip="10.1.0.2",
    )
    assert creds.name == "Alice"
    assert creds.client_ip == "10.1.0.2"
    assert creds.server_public_key == pub
    assert creds.server_endpoint == "vpn.blueagent.local:51821"
    assert creds.private_key is not None
    assert f"PrivateKey = {creds.private_key}" in creds.config_text
    assert f"PublicKey = {pub}" in creds.config_text
    assert "AllowedIPs = 10.1.0.0/24, 10.1.1.0/24" in creds.config_text

    # 2. Client-provided public key (omits private key)
    client_pub_custom = "ClientProvidedPubKey1234567890="
    creds2 = await manager.generate_member_config(
        team_k=1,
        member_name="Bob",
        client_ip="10.1.0.3/32",
        public_key=client_pub_custom,
    )
    assert creds2.private_key is None
    assert creds2.public_key == client_pub_custom
    assert "# PrivateKey generated client-side" in creds2.config_text


@pytest.mark.asyncio
async def test_wireguard_multi_member_team_lifecycle(tmp_path):
    """Verifies end-to-end multi-member WireGuard management and bridge setup for a team."""
    runner = FakeRunner()
    cfg = Settings(wireguard_config_dir=str(tmp_path), wg_public_endpoint="vpn.team.org")
    host_mgr = HostNetworkManager(runner=runner, config=cfg)
    wg_mgr = WireGuardManager(runner=runner, config=cfg)
    team_k = 7

    # 1. Setup production bridge
    br_name = await host_mgr.setup_team_bridge(team_k)
    assert br_name == f"br-team-{team_k}"

    # 2. Setup WireGuard interface
    iface_name = await wg_mgr.setup_team_interface(team_k)
    assert iface_name == f"wg-{team_k}"

    # 3. Provision multiple members in the team
    members = ["Alice", "Bob", "Charlie"]
    allocated_ips: List[str] = []
    member_creds: List[MemberCredentials] = []

    for name in members:
        client_ip = allocate_member_ip(team_k, allocated_ips)
        allocated_ips.append(client_ip)
        creds = await wg_mgr.generate_member_config(
            team_k=team_k,
            member_name=name,
            client_ip=client_ip,
        )
        member_creds.append(creds)
        await wg_mgr.add_peer(
            team_k=team_k,
            member_id=name,
            public_key=creds.public_key,
            allowed_ips=client_ip,
            preshared_key=creds.preshared_key,
        )

    # Validate distinct IP allocation
    assert allocated_ips == ["10.7.0.2", "10.7.0.3", "10.7.0.4"]
    assert len(set(c.public_key for c in member_creds)) == 3

    # Verify WireGuard commands executed for all members
    executed_cmds = [" ".join(call[0]) for call in runner.calls]
    for creds in member_creds:
        assert any(
            f"wg set wg-7 peer {creds.public_key} allowed-ips {creds.client_ip}/32 preshared-key /dev/stdin" in cmd
            for cmd in executed_cmds
        )

    # 4. Remove a member (e.g. Bob)
    await wg_mgr.remove_peer(team_k, member_creds[1].public_key)
    last_cmd = " ".join(runner.calls[-1][0])
    assert f"wg set wg-7 peer {member_creds[1].public_key} remove" == last_cmd

    # 5. Reclaim / allocate for a new member (David) - should fill the gap
    allocated_ips.remove(member_creds[1].client_ip)
    new_ip = allocate_member_ip(team_k, allocated_ips)
    assert new_ip == "10.7.0.3"

    # 6. Teardown
    await wg_mgr.teardown_team_interface(team_k)
    await host_mgr.teardown_team_bridge(team_k)


# ==============================================================================
# 5. __init__.py Package Exports Test
# ==============================================================================


def test_network_package_exports():
    import blueagent.network as net

    expected_exports = [
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
    for symbol in expected_exports:
        assert hasattr(net, symbol), f"blueagent.network missing export '{symbol}'"
    assert net.__all__ == expected_exports
