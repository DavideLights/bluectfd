"""Usability Test 5: Edge Cases, Constraint Enforcement, and Cascading Teardown.

Validates:
1. Referential integrity: Cannot delete an image that is actively referenced by deployed containers (HTTP 409).
2. Cascading team teardown: Deleting a team with running challenges automatically cleans up container instances from Incus.
3. Robust validation: Rejecting challenges referencing non-existent images (HTTP 422) or unprovisioned teams (HTTP 404).
"""

import subprocess
import pytest
from .client import BlueAgentTestClient

TEAM_K = 9
CHALLENGE_ID = "edge-chal-1"
IMAGE_ALIAS = "alpine-distro"


@pytest.fixture
async def team_setup(client: BlueAgentTestClient):
    await client.delete_team(TEAM_K)
    resp = await client.create_team(team_k=TEAM_K, name="Edge Cases Team")
    assert resp.status_code in (200, 201)
    yield TEAM_K
    await client.delete_team(TEAM_K)


@pytest.mark.asyncio
async def test_cannot_delete_image_in_use_by_challenge(client: BlueAgentTestClient, team_setup: int):
    """An image referenced by an active challenge instance must NOT be deletable (returns HTTP 409)."""
    # 1. Deploy challenge using alpine-distro
    chal_payload = {
        "challenge_id": CHALLENGE_ID,
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "protected-box",
                "image": IMAGE_ALIAS,
                "ip_suffix": 30,
            }
        ],
    }
    dep = await client.deploy_challenge(chal_payload)
    assert dep.status_code in (200, 201)

    # 2. Attempt to delete image alpine-distro
    del_img = await client.delete_image(IMAGE_ALIAS)
    assert del_img.status_code == 409, f"Expected 409 Conflict, got {del_img.status_code}"
    assert "active challenge containers reference it" in del_img.text

    # Clean up challenge
    await client.teardown_challenge(TEAM_K, payload=chal_payload)


@pytest.mark.asyncio
async def test_cascading_team_teardown_cleans_containers(client: BlueAgentTestClient):
    """Deleting a team with active challenges must automatically destroy container instances and network."""
    # Ensure team is provisioned
    await client.delete_team(TEAM_K)
    await client.create_team(team_k=TEAM_K, name="Cascade Team")

    # Deploy challenge
    chal_payload = {
        "challenge_id": "cascade-box-chal",
        "team_k": TEAM_K,
        "containers": [
            {
                "name": "orphan-test",
                "image": IMAGE_ALIAS,
                "ip_suffix": 40,
            }
        ],
    }
    dep = await client.deploy_challenge(chal_payload)
    assert dep.status_code in (200, 201)

    container_name = f"c-{TEAM_K}-orphan-test"

    # Confirm container exists in Incus
    check_incus = subprocess.run(["incus", "info", container_name], capture_output=True, text=True)
    assert check_incus.returncode == 0

    # Delete team without deleting challenge first
    del_team = await client.delete_team(TEAM_K)
    assert del_team.status_code == 204

    # Verify container was automatically purged from Incus
    check_after = subprocess.run(["incus", "info", container_name], capture_output=True, text=True)
    assert check_after.returncode != 0, f"Container {container_name} still exists in Incus after team deletion!"

    # Verify network interfaces removed
    br_check = subprocess.run(["ip", "link", "show", f"br-team-{TEAM_K}"])
    assert br_check.returncode != 0


@pytest.mark.asyncio
async def test_invalid_references_rejected(client: BlueAgentTestClient, team_setup: int):
    """Deploying challenges with invalid foreign references returns 404 or 422."""
    # 1. Non-existent image alias
    bad_img_payload = {
        "challenge_id": "bad-img-chal",
        "team_k": TEAM_K,
        "containers": [{"name": "c1", "image": "non-existent-img-xyz", "ip_suffix": 45}],
    }
    res1 = await client.deploy_challenge(bad_img_payload)
    assert res1.status_code == 422, f"Expected 422 for missing image, got {res1.status_code}"

    # 2. Non-existent team
    bad_team_payload = {
        "challenge_id": "bad-team-chal",
        "team_k": 240,  # Unprovisioned team
        "containers": [{"name": "c1", "image": IMAGE_ALIAS, "ip_suffix": 45}],
    }
    res2 = await client.deploy_challenge(bad_team_payload)
    assert res2.status_code == 404, f"Expected 404 for unprovisioned team, got {res2.status_code}"
