"""Usability Test 6: OVN Dark Sandbox Verifier & Automated Checks.

Validates:
- Dark OVN logical switch creation and teardown.
- CoW cloning of target containers and re-binding to dark switch.
- Ephemeral verifier runner provisioning and execution.
- Verification commands execution (in-container and verifier-runner).
- Check failure reporting with exit code and details.
- Command timeout handling (success=False, clean exit).
- Concurrent verification rejection (HTTP 409 Conflict).
- Guaranteed reverse-order RAII teardown (zero lingering verify-* containers/networks).
"""

import asyncio
import subprocess
import pytest
from .client import BlueAgentTestClient

TEAM_K = 12
CHALLENGE_ID = "chal-verifier-test"
IMAGE_ALIAS = "alpine-distro"


@pytest.fixture
async def team_setup(client: BlueAgentTestClient):
    """Ensures team 12 is provisioned for verifier testing, and cleaned up after."""
    await client.delete_team(TEAM_K)
    resp = await client.create_team(team_k=TEAM_K, name="Verifier Test Team")
    assert resp.status_code in (200, 201), f"Failed to setup team: {resp.text}"
    yield TEAM_K
    await client.teardown_challenge(TEAM_K)
    await client.delete_team(TEAM_K)


def check_no_lingering_verify_resources():
    """Asserts no verify-* instances or networks exist in Incus."""
    inst_res = subprocess.run(
        ["incus", "list", "verify-", "--format", "json"],
        capture_output=True,
        text=True,
    )
    if inst_res.returncode == 0:
        import json
        instances = json.loads(inst_res.stdout)
        assert len(instances) == 0, f"Lingering verify instances found: {[i['name'] for i in instances]}"

    net_res = subprocess.run(
        ["incus", "network", "list", "--format", "json"],
        capture_output=True,
        text=True,
    )
    if net_res.returncode == 0:
        import json
        networks = json.loads(net_res.stdout)
        verify_nets = [n["name"] for n in networks if n["name"].startswith("verify-")]
        assert len(verify_nets) == 0, f"Lingering verify networks found: {verify_nets}"


@pytest.mark.asyncio
async def test_verifier_success(client: BlueAgentTestClient, team_setup: int):
    """Tests successful verification with both in-container check and verifier runner probe."""
    payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "verify_internet_access": False,
        "containers": [
            {
                "name": "target-svc",
                "image": IMAGE_ALIAS,
                "ip_suffix": 15,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            }
        ],
        "verifier": {
            "image": IMAGE_ALIAS,
            "ip_suffix": 250,
            "limits_cpu": "1",
            "limits_memory": "128MB",
        },
        "readiness_commands": [
            {
                "target": "container",
                "container_name": "target-svc",
                "cmd": ["uname", "-a"],
                "timeout": 15,
            }
        ],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "target-svc",
                "cmd": ["echo", "in-situ-check-passed"],
                "timeout": 10,
            },
            {
                "target": "verifier",
                "cmd": ["ping", "-c", "2", f"10.{TEAM_K}.1.15"],
                "timeout": 15,
            },
        ],
    }

    await client.teardown_challenge(TEAM_K, payload=payload)

    # 1. Deploy challenge
    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Deploy failed: {deploy_resp.text}"

    # 2. Trigger verification
    verify_resp = await client.verify_challenge(TEAM_K, config_payload=payload)
    assert verify_resp.status_code == 200, f"Verify failed: {verify_resp.status_code} - {verify_resp.text}"

    data = verify_resp.json()
    assert data["success"] is True
    assert data["passed"] is True
    assert "passed" in data["message"].lower()

    # 3. Assert clean teardown
    check_no_lingering_verify_resources()
    await client.teardown_challenge(TEAM_K, payload=payload)


@pytest.mark.asyncio
async def test_verifier_command_failure(client: BlueAgentTestClient, team_setup: int):
    """Tests verification failure reporting when a command returns non-zero exit code."""
    payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "failing-box",
                "image": IMAGE_ALIAS,
                "ip_suffix": 20,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            }
        ],
        "readiness_commands": [],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "failing-box",
                "cmd": ["sh", "-c", "exit 42"],
                "timeout": 10,
            }
        ],
    }

    await client.teardown_challenge(TEAM_K, payload=payload)

    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Deploy failed: {deploy_resp.text}"

    verify_resp = await client.verify_challenge(TEAM_K, config_payload=payload)
    assert verify_resp.status_code == 200
    data = verify_resp.json()
    assert data["success"] is True
    assert data["passed"] is False
    assert "exit 42" in data["message"]
    assert data["details"]["exit_code"] == 42

    check_no_lingering_verify_resources()
    await client.teardown_challenge(TEAM_K, payload=payload)


@pytest.mark.asyncio
async def test_verifier_command_timeout(client: BlueAgentTestClient, team_setup: int):
    """Tests verification timeout handling when a command exceeds timeout limit."""
    payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "timeout-box",
                "image": IMAGE_ALIAS,
                "ip_suffix": 25,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            }
        ],
        "readiness_commands": [],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "timeout-box",
                "cmd": ["sleep", "10"],
                "timeout": 1,
            }
        ],
    }

    await client.teardown_challenge(TEAM_K, payload=payload)

    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Deploy failed: {deploy_resp.text}"

    verify_resp = await client.verify_challenge(TEAM_K, config_payload=payload)
    assert verify_resp.status_code == 200
    data = verify_resp.json()
    assert data["success"] is False
    assert data["passed"] is False
    assert "timeout" in data["message"].lower()

    check_no_lingering_verify_resources()
    await client.teardown_challenge(TEAM_K, payload=payload)


@pytest.mark.asyncio
async def test_verifier_concurrent_rejected(client: BlueAgentTestClient, team_setup: int):
    """Tests that concurrent verification requests for the same team are rejected with 409 Conflict."""
    payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "slow-box",
                "image": IMAGE_ALIAS,
                "ip_suffix": 30,
                "limits_cpu": "1",
                "limits_memory": "128MB",
            }
        ],
        "readiness_commands": [],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "slow-box",
                "cmd": ["sleep", "4"],
                "timeout": 10,
            }
        ],
    }

    await client.teardown_challenge(TEAM_K, payload=payload)

    deploy_resp = await client.deploy_challenge(payload)
    assert deploy_resp.status_code in (200, 201), f"Deploy failed: {deploy_resp.text}"

    # Launch first verification in background
    task1 = asyncio.create_task(client.verify_challenge(TEAM_K, config_payload=payload))

    # Give task1 a moment to acquire the lock and start
    await asyncio.sleep(0.8)

    # Second verification request for the same team should be rejected immediately
    resp2 = await client.verify_challenge(TEAM_K, config_payload=payload)
    assert resp2.status_code == 409, f"Expected 409 Conflict for concurrent verify, got {resp2.status_code}: {resp2.text}"

    # First verification finishes successfully
    resp1 = await task1
    assert resp1.status_code == 200
    assert resp1.json()["passed"] is True

    check_no_lingering_verify_resources()
    await client.teardown_challenge(TEAM_K, payload=payload)
