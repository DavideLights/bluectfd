"""Unit tests for BlueAgentClient using responses mock."""

import pytest
import responses

from CTFd.plugins.blueagent.client import (
    BlueAgentAuthError,
    BlueAgentBusy,
    BlueAgentClient,
    BlueAgentConflict,
    BlueAgentUnavailable,
    DeploymentRolledBack,
    TeamNotFound,
)

BASE_URL = "http://127.0.0.1:8000"


@responses.activate
def test_client_health():
    responses.add(
        responses.GET,
        f"{BASE_URL}/health",
        json={"status": "ok", "version": "2.0"},
        status=200,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    res = client.health()
    assert res["status"] == "ok"
    assert res["version"] == "2.0"


@responses.activate
def test_client_auth_error_401():
    responses.add(
        responses.GET,
        f"{BASE_URL}/teams",
        json={"detail": "Invalid token"},
        status=401,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="bad_token")
    with pytest.raises(BlueAgentAuthError):
        client.list_teams()


@responses.activate
def test_client_team_not_found_404():
    responses.add(
        responses.GET,
        f"{BASE_URL}/teams/99",
        json={"detail": "Team 99 does not exist"},
        status=404,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    with pytest.raises(TeamNotFound):
        client.get_team(99)


@responses.activate
def test_client_conflict_409():
    responses.add(
        responses.POST,
        f"{BASE_URL}/teams/1/verificator",
        json={"detail": "Verification already in progress or team resource locked for team 1"},
        status=409,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    with pytest.raises(BlueAgentConflict) as exc_info:
        client.verify_challenge(1, {})
    assert exc_info.value.is_verify_busy is True


@responses.activate
def test_client_deployment_rollback_502():
    responses.add(
        responses.POST,
        f"{BASE_URL}/teams/1/machines",
        json={"detail": "Container engine failed to boot container; rolled back"},
        status=502,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    with pytest.raises(DeploymentRolledBack):
        client.deploy_challenge(1, {})


@responses.activate
def test_client_lock_busy_503():
    responses.add(
        responses.PUT,
        f"{BASE_URL}/teams/1",
        json={"detail": "Lock timeout on team resource"},
        status=503,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    with pytest.raises(BlueAgentBusy):
        client.create_team(1, "Test Team")


@responses.activate
def test_client_create_team_success():
    responses.add(
        responses.PUT,
        f"{BASE_URL}/teams/1",
        json={"team_k": 1, "name": "Team 1", "bridge": "br-team1"},
        status=201,
    )
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    res = client.create_team(1, "Team 1")
    assert res["team_k"] == 1
    assert res["name"] == "Team 1"


@responses.activate
def test_client_member_lifecycle():
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")

    # 1. Add member (PUT /teams/1/members/42)
    responses.add(
        responses.PUT,
        f"{BASE_URL}/teams/1/members/42",
        json={
            "member_id": "ctfd-1-42",
            "team_k": 1,
            "name": "alice",
            "client_ip": "10.1.0.2/24",
            "public_key": "pubkey123",
            "ctfd_user_id": 42,
            "config_text": "[Interface]\nAddress = 10.1.0.2/24\n",
        },
        status=201,
    )
    added = client.add_member(team_k=1, name="alice", ctfd_user_id=42)
    assert added["member_id"] == "ctfd-1-42"
    assert added["client_ip"] == "10.1.0.2/24"

    # 2. Get member (GET /teams/1/members/42)
    responses.add(
        responses.GET,
        f"{BASE_URL}/teams/1/members/42",
        json={
            "member_id": "ctfd-1-42",
            "team_k": 1,
            "name": "alice",
            "client_ip": "10.1.0.2/24",
            "telemetry": {"online": True},
        },
        status=200,
    )
    member = client.get_member(team_k=1, user_id=42)
    assert member["name"] == "alice"

    # 3. Get member config (GET /teams/1/members/42/config)
    responses.add(
        responses.GET,
        f"{BASE_URL}/teams/1/members/42/config",
        body="[Interface]\nPrivateKey = priv\nAddress = 10.1.0.2/24\n",
        content_type="text/plain",
        status=200,
    )
    cfg = client.get_member_config(team_k=1, user_id=42)
    assert "[Interface]" in cfg

    # 4. Delete member (DELETE /teams/1/members/42)
    responses.add(
        responses.DELETE,
        f"{BASE_URL}/teams/1/members/42",
        status=204,
    )
    client.delete_member(team_k=1, user_id=42)


@responses.activate
def test_client_get_peers_unpacks_telemetry():
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")
    responses.add(
        responses.GET,
        f"{BASE_URL}/teams/1/members",
        json=[
            {
                "member_id": "ctfd-1-42",
                "team_k": 1,
                "name": "alice",
                "client_ip": "10.1.0.2/24",
                "public_key": "pub123",
                "user_id": 42,
                "telemetry": {
                    "online": True,
                    "endpoint": "203.0.113.5:51820",
                    "latest_handshake": 1700000000,
                    "transfer_rx": 1024,
                    "transfer_tx": 2048,
                    "persistent_keepalive": "25",
                },
            }
        ],
        status=200,
    )
    peers = client.get_peers(1)
    assert len(peers) == 1
    p = peers[0]
    assert p["name"] == "alice"
    assert p["online"] is True
    assert p["endpoint"] == "203.0.113.5:51820"
    assert p["transfer_rx"] == 1024


@responses.activate
def test_client_image_lifecycle():
    client = BlueAgentClient(base_url=BASE_URL, token="test_token")

    # 1. Build image (PUT /images/my-distro)
    responses.add(
        responses.PUT,
        f"{BASE_URL}/images/my-distro",
        json={"alias": "my-distro", "status": "building", "fingerprint": None},
        status=202,
    )
    res = client.build_image(definition_bytes=b"image:\n  description: my-distro\n", alias="my-distro")
    assert res["alias"] == "my-distro"
    assert res["status"] == "building"

    # 2. Get logs (GET /images/my-distro/logs)
    responses.add(
        responses.GET,
        f"{BASE_URL}/images/my-distro/logs",
        body="Starting distrobuilder build...\nCompleted step 1.\n",
        content_type="text/plain",
        status=200,
    )
    logs = client.get_image_logs("my-distro")
    assert "distrobuilder" in logs

    # 3. Delete image (DELETE /images/my-distro)
    responses.add(
        responses.DELETE,
        f"{BASE_URL}/images/my-distro",
        status=204,
    )
    client.delete_image("my-distro")
