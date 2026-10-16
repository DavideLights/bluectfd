"""Usability Test 4: Full End-to-End Data-Plane Usability with Isolated Network Namespace.

Validates the complete competitor experience:
1. Team & WireGuard interface provisioning (br-team-k, wg-k).
2. Competitor credential generation & WireGuard config generation.
3. Challenge container deployment onto the team bridge with static IP.
4. Setting up an isolated competitor network namespace ('player-ns') connected via veth.
5. Connecting with wg-quick up <conf> inside the player's namespace.
6. Pinging the container across the WireGuard tunnel and Linux bridge.
7. Disconnecting with wg-quick down and cleaning up all resources.
"""

import subprocess
import time
from pathlib import Path
import pytest
from .client import BlueAgentTestClient

TEAM_K = 8
CHALLENGE_ID = "dataplane-chal"
IMAGE_ALIAS = "alpine-distro"
CONF_PATH = Path(f"/tmp/wgplayer{TEAM_K}.conf")
NETNS = f"player-ns-{TEAM_K}"
VETH_HOST = f"veth-h-{TEAM_K}"
VETH_CLIENT = f"veth-c-{TEAM_K}"
HOST_VETH_IP = "192.168.208.1"
CLIENT_VETH_IP = "192.168.208.2"


def cleanup_netns():
    """Tears down test namespace and veth links."""
    subprocess.run(["sudo", "ip", "netns", "exec", NETNS, "wg-quick", "down", str(CONF_PATH)], check=False)
    subprocess.run(["sudo", "ip", "link", "delete", "dev", VETH_HOST], check=False)
    subprocess.run(["sudo", "ip", "netns", "delete", NETNS], check=False)
    CONF_PATH.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_vpn_to_container_dataplane_connectivity(client: BlueAgentTestClient):
    """Proves that a user can connect via WireGuard from an isolated namespace and reach the container."""
    cleanup_netns()
    await client.teardown_challenge(TEAM_K)
    await client.delete_team(TEAM_K)

    try:
        # 1. Create team 8
        t_resp = await client.create_team(team_k=TEAM_K, name="DataPlane Team")
        assert t_resp.status_code == 201

        # 2. Add member and download config
        m_resp = await client.add_member(team_k=TEAM_K, ctfd_user_id=801, name="VPN Player")
        assert m_resp.status_code in (200, 201)
        member_id = m_resp.json()["member_id"]

        cfg_resp = await client.get_member_config(TEAM_K, member_id)
        assert cfg_resp.status_code == 200
        config_text = cfg_resp.text

        # 3. Deploy challenge container (target IP: 10.8.1.25)
        chal_payload = {
            "challenge_id": CHALLENGE_ID,
            "team_k": TEAM_K,
            "containers": [
                {
                    "name": "vpn-box",
                    "image": IMAGE_ALIAS,
                    "ip_suffix": 25,
                    "limits_cpu": "1",
                    "limits_memory": "128MB",
                }
            ],
            "readiness_commands": [
                {
                    "target": "container",
                    "container_name": "vpn-box",
                    "cmd": ["ip", "addr", "show", "dev", "eth0"],
                    "timeout": 15,
                }
            ],
        }
        dep_resp = await client.deploy_challenge(chal_payload)
        assert dep_resp.status_code in (200, 201), f"Deploy failed: {dep_resp.text}"

        target_ip = f"10.{TEAM_K}.1.25"

        # 4. Set up isolated client network namespace simulating external player machine
        subprocess.run(["sudo", "ip", "netns", "add", NETNS], check=True)
        subprocess.run(["sudo", "ip", "link", "add", VETH_HOST, "type", "veth", "peer", "name", VETH_CLIENT], check=True)
        subprocess.run(["sudo", "ip", "link", "set", VETH_CLIENT, "netns", NETNS], check=True)

        subprocess.run(["sudo", "ip", "addr", "add", f"{HOST_VETH_IP}/24", "dev", VETH_HOST], check=True)
        subprocess.run(["sudo", "ip", "link", "set", VETH_HOST, "up"], check=True)

        subprocess.run(["sudo", "ip", "netns", "exec", NETNS, "ip", "addr", "add", f"{CLIENT_VETH_IP}/24", "dev", VETH_CLIENT], check=True)
        subprocess.run(["sudo", "ip", "netns", "exec", NETNS, "ip", "link", "set", VETH_CLIENT, "up"], check=True)
        subprocess.run(["sudo", "ip", "netns", "exec", NETNS, "ip", "link", "set", "lo", "up"], check=True)
        subprocess.run(["sudo", "ip", "netns", "exec", NETNS, "ip", "route", "add", "default", "via", HOST_VETH_IP], check=True)

        # 5. Point WireGuard endpoint to the host veth IP reachable from the namespace
        wg_port = 51820 + TEAM_K
        custom_config = config_text.replace(f"127.0.0.1:{wg_port}", f"{HOST_VETH_IP}:{wg_port}")
        CONF_PATH.write_text(custom_config, encoding="utf-8")

        # 6. Connect WireGuard inside player network namespace
        subprocess.run(
            ["sudo", "ip", "netns", "exec", NETNS, "wg-quick", "up", str(CONF_PATH)],
            check=True,
        )

        time.sleep(1)

        # 7. Ping container from inside the player network namespace
        ping_proc = subprocess.run(
            ["sudo", "ip", "netns", "exec", NETNS, "ping", "-c", "3", "-W", "2", target_ip],
            capture_output=True,
            text=True,
        )
        print("PING RESULT FROM NETNS:\n", ping_proc.stdout, ping_proc.stderr)
        assert ping_proc.returncode == 0, f"Ping failed from player namespace to {target_ip}:\n{ping_proc.stderr or ping_proc.stdout}"
        assert "0% packet loss" in ping_proc.stdout

    finally:
        cleanup_netns()
        await client.teardown_challenge(TEAM_K)
        await client.delete_team(TEAM_K)
