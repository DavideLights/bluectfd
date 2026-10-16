"""Usability Test 1: Team Management & Network Creation (Allocation & Destruction).

Validates:
- Network creation: Linux bridge (br-team-k) and WireGuard interface (wg-k) setup with correct IP schemes.
- Team allocation: REST API provisioning, multi-member key generation, and client config generation.
- WireGuard peer registration: Kernel state synchronization.
- Resource destruction: Complete teardown of kernel interfaces and database records.
- Idempotency and re-creation: Verifies no lingering artifacts block re-provisioning.
"""

import subprocess
import pytest
from .client import BlueAgentTestClient

TEAM_K = 5
CTFD_TEAM_ID = 505
CTFD_USER_ID = 101


@pytest.mark.asyncio
async def test_team_allocation_and_kernel_interfaces(client: BlueAgentTestClient):
    """Test team allocation creates both database record and host kernel interfaces."""
    # Ensure clean slate for team 5
    await client.delete_team(TEAM_K)

    # 1. Provision team 5
    resp = await client.create_team(team_k=TEAM_K, name="Team Usability Alpha", ctfd_team_id=CTFD_TEAM_ID)
    assert resp.status_code == 201, f"Expected 201 Created, got {resp.status_code}: {resp.text}"
    data = resp.json()
    assert data["team_k"] == TEAM_K
    assert data["bridge_name"] == f"br-team-{TEAM_K}"
    assert data["wg_interface"] == f"wg-{TEAM_K}"
    assert data["name"] == "Team Usability Alpha"

    # 2. Check kernel state: Bridge
    br_proc = subprocess.run(
        ["ip", "addr", "show", "dev", f"br-team-{TEAM_K}"],
        capture_output=True,
        text=True,
    )
    assert br_proc.returncode == 0, f"Bridge br-team-{TEAM_K} not found in kernel: {br_proc.stderr}"
    assert f"10.{TEAM_K}.1.1/24" in br_proc.stdout, f"Bridge IP not set correctly: {br_proc.stdout}"

    # 3. Check kernel state: WireGuard interface
    wg_proc = subprocess.run(
        ["ip", "addr", "show", "dev", f"wg-{TEAM_K}"],
        capture_output=True,
        text=True,
    )
    assert wg_proc.returncode == 0, f"WireGuard wg-{TEAM_K} not found in kernel: {wg_proc.stderr}"
    assert f"10.{TEAM_K}.0.1/24" in wg_proc.stdout, f"WireGuard IP not set correctly: {wg_proc.stdout}"


@pytest.mark.asyncio
async def test_team_member_allocation_and_config(client: BlueAgentTestClient):
    """Test member keypair generation, IP allocation, and WireGuard configuration download."""
    # 1. Add member to team 5
    resp = await client.add_member(
        team_k=TEAM_K,
        ctfd_user_id=CTFD_USER_ID,
        name="Hacker Zero",
        ctfd_team_id=CTFD_TEAM_ID,
    )
    assert resp.status_code in (200, 201), f"Expected 200/201, got {resp.status_code}: {resp.text}"
    member = resp.json()
    assert member["team_k"] == TEAM_K
    assert member["client_ip"] == f"10.{TEAM_K}.0.2"
    assert "public_key" in member and len(member["public_key"]) > 0
    member_id = member["member_id"]

    # 2. Download WireGuard configuration
    cfg_resp = await client.get_member_config(TEAM_K, member_id)
    assert cfg_resp.status_code == 200, f"Failed to download config: {cfg_resp.text}"
    config_text = cfg_resp.text
    assert "[Interface]" in config_text
    assert f"Address = 10.{TEAM_K}.0.2/32" in config_text
    assert "[Peer]" in config_text
    assert "Endpoint =" in config_text
    assert f"AllowedIPs = 10.{TEAM_K}.0.0/24, 10.{TEAM_K}.1.0/24" in config_text

    # 3. Verify WireGuard peer in kernel
    wg_show = subprocess.run(
        ["sudo", "wg", "show", f"wg-{TEAM_K}"],
        capture_output=True,
        text=True,
    )
    assert wg_show.returncode == 0, f"Failed to inspect WireGuard: {wg_show.stderr}"
    assert member["public_key"] in wg_show.stdout, "Member public key not registered in kernel peer list"

    # 4. Check peer telemetry endpoint
    peers_resp = await client.get_peers(TEAM_K)
    assert peers_resp.status_code == 200
    peer_data = peers_resp.json()
    assert isinstance(peer_data, list)
    assert len(peer_data) >= 1
    assert any(p.get("public_key") == member["public_key"] for p in peer_data)


@pytest.mark.asyncio
async def test_team_destruction_and_kernel_cleanup(client: BlueAgentTestClient):
    """Test team deletion destroys database record and removes all kernel interfaces."""
    # 1. Delete team 5
    resp = await client.delete_team(TEAM_K)
    assert resp.status_code == 204, f"Expected 204 No Content, got {resp.status_code}: {resp.text}"

    # 2. Verify API returns 404
    get_resp = await client.get_team(TEAM_K)
    assert get_resp.status_code == 404

    # 3. Verify kernel interfaces are destroyed
    br_check = subprocess.run(
        ["ip", "link", "show", "dev", f"br-team-{TEAM_K}"],
        capture_output=True,
        text=True,
    )
    assert br_check.returncode != 0, f"Bridge br-team-{TEAM_K} still exists in kernel!"

    wg_check = subprocess.run(
        ["ip", "link", "show", "dev", f"wg-{TEAM_K}"],
        capture_output=True,
        text=True,
    )
    assert wg_check.returncode != 0, f"WireGuard interface wg-{TEAM_K} still exists in kernel!"


@pytest.mark.asyncio
async def test_team_idempotency_and_recreation(client: BlueAgentTestClient):
    """Test that after complete destruction, a team can be re-created without conflicts."""
    # Re-create team 5
    resp = await client.create_team(team_k=TEAM_K, name="Team Usability Alpha Reborn", ctfd_team_id=CTFD_TEAM_ID)
    assert resp.status_code == 201

    # Verify bridge is back
    br_proc = subprocess.run(["ip", "link", "show", "dev", f"br-team-{TEAM_K}"])
    assert br_proc.returncode == 0

    # Clean up again
    del_resp = await client.delete_team(TEAM_K)
    assert del_resp.status_code == 204
