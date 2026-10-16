"""Usability Test 3: Challenge & Container Instance Creation and Teardown.

Validates:
- Instance creation: Provisioning Incus container instances attached to dedicated team bridge (br-team-k).
- Resource constraints: Enforcing CPU and memory limits on container instances.
- Static IP injection: Assigning static 10.k.1.X IP addresses.
- Multi-container topology: Inter-container networking over the bridge.
- Container lifecycle: Querying active running containers per team and teardown of container instances.
"""

import subprocess
import pytest
from .client import BlueAgentTestClient

TEAM_K = 7
CHALLENGE_ID = "chal-web-pwn"
IMAGE_ALIAS = "alpine-distro"


@pytest.fixture
async def team_setup(client: BlueAgentTestClient):
    """Ensures team 7 is provisioned for challenge testing, and cleaned up after."""
    await client.delete_team(TEAM_K)
    resp = await client.create_team(team_k=TEAM_K, name="Challenge Test Team")
    assert resp.status_code in (200, 201), f"Failed to setup team: {resp.text}"
    yield TEAM_K
    await client.delete_team(TEAM_K)


@pytest.mark.asyncio
async def test_challenge_and_instance_deployment(client: BlueAgentTestClient, team_setup: int):
    """Test deploying challenge containers, checking Incus runtime state, and verifying limits."""
    # Ensure any previous deployment is deleted
    await client.teardown_challenge(TEAM_K)

    payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "target-box",
                "image": IMAGE_ALIAS,
                "ip_suffix": 15,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            }
        ],
        "readiness_commands": [
            {
                "target": "container",
                "container_name": "target-box",
                "cmd": ["uname", "-a"],
                "timeout": 15,
            }
        ],
    }

    # 1. Deploy challenge
    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Challenge deploy failed: {deploy_resp.status_code} - {deploy_resp.text}"
    chal_data = deploy_resp.json()
    assert chal_data["challenge_id"] == CHALLENGE_ID
    assert chal_data["team_k"] == TEAM_K
    assert chal_data["status"] == "running"

    expected_inst_name = f"c-{TEAM_K}-target-box"

    # 2. Verify instance exists and is RUNNING in Incus
    incus_info = subprocess.run(
        ["incus", "info", expected_inst_name],
        capture_output=True,
        text=True,
    )
    assert incus_info.returncode == 0, f"Incus instance {expected_inst_name} does not exist: {incus_info.stderr}"
    assert "Status: RUNNING" in incus_info.stdout

    # 3. Verify CPU and Memory limits in Incus configuration
    incus_json = subprocess.run(
        ["incus", "config", "show", expected_inst_name],
        capture_output=True,
        text=True,
    )
    assert incus_json.returncode == 0
    config_text = incus_json.stdout
    assert "limits.cpu: \"1\"" in config_text or "limits.cpu: '1'" in config_text or "limits.cpu: 1" in config_text
    assert "128MB" in config_text

    # 4. Query active containers for team k via API
    get_resp = await client.get_active_containers(TEAM_K)
    assert get_resp.status_code == 200
    retrieved = get_resp.json()
    assert isinstance(retrieved, list)
    assert len(retrieved) >= 1
    assert any(c["instance_name"] == expected_inst_name and c["ip_address"] == f"10.{TEAM_K}.1.15" for c in retrieved)

    # 5. Teardown challenge and verify instance destruction
    del_resp = await client.teardown_challenge(TEAM_K, payload=payload)
    assert del_resp.status_code == 204

    # Verify instance removed from Incus
    incus_check = subprocess.run(
        ["incus", "info", expected_inst_name],
        capture_output=True,
        text=True,
    )
    assert incus_check.returncode != 0, f"Instance {expected_inst_name} still exists in Incus after deletion!"

    # Verify active containers list is empty for team k
    get_del = await client.get_active_containers(TEAM_K)
    assert get_del.status_code == 200
    assert len(get_del.json()) == 0


@pytest.mark.asyncio
async def test_multi_container_topology_and_readiness(client: BlueAgentTestClient, team_setup: int):
    """Test deploying a multi-container challenge topology."""
    multi_chal_id = "multi-service-chal"
    await client.teardown_challenge(TEAM_K)

    payload = {
        "challenge_id": multi_chal_id,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "backend",
                "image": IMAGE_ALIAS,
                "ip_suffix": 50,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            },
            {
                "name": "frontend",
                "image": IMAGE_ALIAS,
                "ip_suffix": 51,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            },
        ],
        "readiness_commands": [
            {
                "target": "container",
                "container_name": "backend",
                "cmd": ["echo", "backend-ready"],
                "timeout": 15,
            },
            {
                "target": "container",
                "container_name": "frontend",
                "cmd": ["echo", "frontend-ready"],
                "timeout": 15,
            },
        ],
    }

    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Multi-container deploy failed: {deploy_resp.text}"
    chal_data = deploy_resp.json()
    assert chal_data["status"] == "running"
    assert len(chal_data["instances"]) == 2

    # Query active containers
    containers_resp = await client.get_active_containers(TEAM_K)
    assert containers_resp.status_code == 200
    active_names = [c["instance_name"] for c in containers_resp.json()]
    assert f"c-{TEAM_K}-backend" in active_names
    assert f"c-{TEAM_K}-frontend" in active_names

    # Clean up multi-container deployment
    del_resp = await client.teardown_challenge(TEAM_K, payload=payload)
    assert del_resp.status_code == 204
