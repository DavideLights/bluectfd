"""Unit tests for api/teams.py endpoints and multi-member WireGuard management."""

import pytest
from httpx import ASGITransport, AsyncClient
from blueagent.main import app, lifespan
from blueagent.config import settings
from blueagent.locks import LockTimeoutError
from blueagent.network._runner import CompletedCommand

from blueagent.network.host_net import HostNetworkManager
from blueagent.network.wireguard import WireGuardManager, generate_keypair
from typing import Dict, List, Optional, Sequence, Tuple
from blueagent.network._runner import CommandRunner, CompletedCommand, HostCommandError


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
                    raise HostCommandError(argv=argv, returncode=resp.returncode, stdout=resp.stdout, stderr=resp.stderr)
                return resp
        return CompletedCommand(argv=argv, returncode=0, stdout="", stderr="")


@pytest.fixture
async def teams_test_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_teams.db"
    test_locks = tmp_path / "test_teams_locks"
    wg_conf_dir = tmp_path / "wg_configs"

    monkeypatch.setattr(settings, "database_path", str(test_db))
    monkeypatch.setattr(settings, "lock_directory", str(test_locks))
    monkeypatch.setattr(settings, "wireguard_config_dir", str(wg_conf_dir))
    monkeypatch.setattr(settings, "ctfd_api_token", "test-secret-token")

    runner = FakeRunner()
    # Mock 'ip link show' -> not found initially (returncode=1)
    runner.set_response("ip -json link show", returncode=1)
    # Mock 'wg show ... public-key'
    server_priv, server_pub = generate_keypair()
    runner.set_response("wg show wg-1 public-key", stdout=server_pub)
    runner.set_response("wg show wg-2 public-key", stdout=server_pub)
    runner.set_response("wg show wg-3 public-key", stdout=server_pub)
    runner.set_response("wg show wg-5 public-key", stdout=server_pub)

    host_net = HostNetworkManager(runner=runner, config=settings)
    wg = WireGuardManager(runner=runner, config=settings)

    async with lifespan(app):
        app.state.host_net = host_net
        app.state.wg = wg

        auth_headers = {"Authorization": "Bearer test-secret-token"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as ac:
            yield {
                "client": ac,
                "runner": runner,
                "db": app.state.db,
                "locks": app.state.locks,
                "host_net": host_net,
                "wg": wg,
                "server_pub": server_pub,
            }


@pytest.mark.asyncio
async def test_auth_headers(teams_test_env):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as unauth_client:
        # 1. No token -> 401
        res = await unauth_client.get("/teams")
        assert res.status_code == 401

        # 2. Bad Bearer token -> 401
        res = await unauth_client.get("/teams", headers={"Authorization": "Bearer wrong-token"})
        assert res.status_code == 401

        # 3. Valid X-CTFd-Token header -> 200
        res = await unauth_client.get("/teams", headers={"X-CTFd-Token": "test-secret-token"})
        assert res.status_code == 200


@pytest.mark.asyncio
async def test_team_create_and_idempotency(teams_test_env):
    client = teams_test_env["client"]

    # 1. Create Team (201 via PUT /teams/1)
    res = await client.put(
        "/teams/1",
        json={"name": "Team Red"},
    )
    assert res.status_code == 201
    data = res.json()
    assert data["team_k"] == 1
    assert data["name"] == "Team Red"
    assert data["bridge_name"] == "br-team-1"
    assert data["wg_interface"] == "wg-1"

    # 2. Re-create / update same Team (Idempotent 200)
    res_repeat = await client.put(
        "/teams/1",
        json={"name": "Team Red Renamed"},
    )
    assert res_repeat.status_code == 200
    assert res_repeat.json()["team_k"] == data["team_k"]
    assert res_repeat.json()["name"] == "Team Red Renamed"


@pytest.mark.asyncio
async def test_team_crud_and_delete(teams_test_env):
    client = teams_test_env["client"]

    # Create two teams
    r1 = await client.put("/teams/1", json={"name": "Alpha"})
    assert r1.status_code == 201
    r2 = await client.put("/teams/2", json={"name": "Beta"})
    assert r2.status_code == 201

    # List teams
    res_list = await client.get("/teams")
    assert res_list.status_code == 200
    teams = res_list.json()
    assert len(teams) == 2
    assert [t["team_k"] for t in teams] == [1, 2]

    # Get team 1
    res_get = await client.get("/teams/1")
    assert res_get.status_code == 200
    assert res_get.json()["name"] == "Alpha"

    # Get non-existent team -> 404
    res_get_missing = await client.get("/teams/99")
    assert res_get_missing.status_code == 404

    # Delete team 1 -> 204
    res_del = await client.delete("/teams/1")
    assert res_del.status_code == 204

    # Verify team 1 deleted
    assert (await client.get("/teams/1")).status_code == 404
    assert len((await client.get("/teams")).json()) == 1

    # Idempotent delete of non-existent team -> 204
    assert (await client.delete("/teams/1")).status_code == 204


@pytest.mark.asyncio
async def test_explicit_team_creation_required_for_member(teams_test_env):
    client = teams_test_env["client"]

    # Team 3 has not been created yet -> must return 404
    res_missing = await client.put(
        "/teams/3/members/301",
        json={"name": "Alice"},
    )
    assert res_missing.status_code == 404

    # Provision team 3 explicitly
    t_res = await client.put("/teams/3", json={"name": "Team 3"})
    assert t_res.status_code == 201

    # Now adding member succeeds
    res = await client.put(
        "/teams/3/members/301",
        json={"name": "Alice"},
    )
    assert res.status_code == 201
    member = res.json()
    assert member["member_id"] == "ctfd-3-301"
    assert member["client_ip"] == "10.3.0.2"
    assert member["public_key"] is not None
    assert member["private_key"] is not None
    assert "PrivateKey = " in member["config_text"]
    assert "Address = 10.3.0.2/32" in member["config_text"]


@pytest.mark.asyncio
async def test_member_reclick_idempotency(teams_test_env):
    client = teams_test_env["client"]

    # Create team
    await client.put("/teams/1", json={"name": "Team One"})

    # 1. Add Member Alice (201 Created)
    res1 = await client.put(
        "/teams/1/members/10",
        json={"name": "Alice"},
    )
    assert res1.status_code == 201
    m1 = res1.json()
    assert m1["client_ip"] == "10.1.0.2"
    orig_priv = m1["private_key"]
    orig_pub = m1["public_key"]
    orig_config = m1["config_text"]

    # 2. Add Member Bob (gets next IP: 10.1.0.3)
    res2 = await client.put(
        "/teams/1/members/20",
        json={"name": "Bob"},
    )
    assert res2.status_code == 201
    m2 = res2.json()
    assert m2["client_ip"] == "10.1.0.3"

    # 3. Alice re-clicks! Idempotent 200 OK: same IP, same private_key, same config_text!
    res1_reclick = await client.put(
        "/teams/1/members/10",
        json={"name": "Alice"},
    )
    assert res1_reclick.status_code == 200
    m1_reclick = res1_reclick.json()
    assert m1_reclick["client_ip"] == "10.1.0.2"
    assert m1_reclick["public_key"] == orig_pub
    assert m1_reclick["private_key"] == orig_priv
    assert m1_reclick["config_text"] == orig_config

    # Ensure total members is still 2
    members_list = await client.get("/teams/1/members")
    assert len(members_list.json()) == 2


@pytest.mark.asyncio
async def test_member_cross_team_conflict(teams_test_env):
    client = teams_test_env["client"]

    # Create Team 1 and Team 2
    await client.put("/teams/1", json={"name": "Team 1"})
    await client.put("/teams/2", json={"name": "Team 2"})

    # Add user 50 to Team 1
    await client.put("/teams/1/members/50", json={"name": "Charlie"})

    # Attempt to add user 50 to Team 2 -> 409 Conflict
    conflict_res = await client.put(
        "/teams/2/members/50",
        json={"name": "Charlie"},
    )
    assert conflict_res.status_code == 409
    assert "already assigned" in conflict_res.json()["detail"]


@pytest.mark.asyncio
async def test_member_ip_hole_filling(teams_test_env):
    client = teams_test_env["client"]
    await client.put("/teams/1", json={"name": "Team 1"})

    # Allocate 3 members: .2, .3, .4
    r1 = await client.put("/teams/1/members/1", json={"name": "M1"})
    r2 = await client.put("/teams/1/members/2", json={"name": "M2"})
    r3 = await client.put("/teams/1/members/3", json={"name": "M3"})

    assert r1.json()["client_ip"] == "10.1.0.2"
    assert r2.json()["client_ip"] == "10.1.0.3"
    assert r3.json()["client_ip"] == "10.1.0.4"

    # Delete M2 (.3) using user_id "2"
    del_res = await client.delete("/teams/1/members/2")
    assert del_res.status_code == 204

    # Add new member M4 -> must reuse .3!
    r4 = await client.put("/teams/1/members/4", json={"name": "M4"})
    assert r4.json()["client_ip"] == "10.1.0.3"


@pytest.mark.asyncio
async def test_member_config_download_and_masking(teams_test_env):
    client = teams_test_env["client"]
    await client.put("/teams/1", json={"name": "Team 1"})

    create_res = await client.put(
        "/teams/1/members/77",
        json={"name": "SpecialAgent"},
    )
    assert create_res.status_code == 201

    # 1. Download WireGuard .conf using user_id 77
    conf_res = await client.get("/teams/1/members/77/config")
    assert conf_res.status_code == 200
    assert conf_res.headers["content-type"].startswith("text/plain")
    conf_text = conf_res.text
    assert "[Interface]" in conf_text
    assert "PrivateKey = " in conf_text
    assert "Address = 10.1.0.2/32" in conf_text
    assert "[Peer]" in conf_text
    assert "AllowedIPs = 10.1.0.0/24, 10.1.1.0/24" in conf_text

    # 2. Listing members masks private_key and preshared_key and embeds telemetry
    list_res = await client.get("/teams/1/members")
    assert list_res.status_code == 200
    listed_member = list_res.json()[0]
    assert "private_key" not in listed_member
    assert "preshared_key" not in listed_member
    assert listed_member["public_key"] is not None
    assert listed_member["user_id"] == 77
    assert listed_member["telemetry"] is not None


@pytest.mark.asyncio
async def test_peer_telemetry_in_members_list(teams_test_env):
    client = teams_test_env["client"]
    runner = teams_test_env["runner"]

    await client.put("/teams/1", json={"name": "Team 1"})
    m_res = await client.put("/teams/1/members/11", json={"name": "Dave"})
    pub = m_res.json()["public_key"]

    # Mock 'wg show wg-1 dump'
    dump_out = f"wg-1\tserverpriv\tserverpub\t51821\toff\n{pub}\tpsk123\t198.51.100.5:41234\t10.1.0.2/32\t1727980000\t1048576\t2097152\t25\n"
    runner.set_response("wg show wg-1 dump", stdout=dump_out)

    # Legacy /peers is removed -> 404
    peers_res = await client.get("/teams/1/peers")
    assert peers_res.status_code == 404

    # GET /teams/1/members returns real-time telemetry directly
    members_res = await client.get("/teams/1/members")
    assert members_res.status_code == 200
    members = members_res.json()
    assert len(members) == 1
    m = members[0]
    assert m["user_id"] == 11
    assert m["public_key"] == pub
    assert m["telemetry"]["endpoint"] == "198.51.100.5:41234"
    assert m["telemetry"]["transfer_rx"] == 1048576
    assert m["telemetry"]["transfer_tx"] == 2097152
    assert m["telemetry"]["latest_handshake"] == 1727980000


@pytest.mark.asyncio
async def test_error_handling_and_rollbacks(teams_test_env):
    client = teams_test_env["client"]
    runner = teams_test_env["runner"]

    # 1. WireGuard setup failure during team creation -> 500 & bridge rolled back
    runner.set_response("wg set wg-5 listen-port", returncode=1, stderr="RTNETLINK answers: Permission denied")
    res = await client.put("/teams/5", json={"name": "Failing Team"})
    assert res.status_code == 500
    # Ensure bridge was torn down
    assert any("delete" in " ".join(call[0]) and "br-team-5" in " ".join(call[0]) for call in runner.calls)
    # Ensure team was not saved in DB
    assert (await client.get("/teams/5")).status_code == 404


@pytest.mark.asyncio
async def test_capacity_limit_253_members(teams_test_env):
    client = teams_test_env["client"]
    db = teams_test_env["db"]

    # Create team
    await client.put("/teams/1", json={"name": "Team Full"})

    # Insert 253 members directly into DB (IPs .2 to .254)
    for host_byte in range(2, 255):
        ip = f"10.1.0.{host_byte}"
        await db.add_team_member(
            member_id=f"ctfd-1-{host_byte}",
            team_k=1,
            name=f"Member{host_byte}",
            client_ip=ip,
            public_key=f"pub_{host_byte}=" + "A" * 38,
            ctfd_user_id=1000 + host_byte,
        )

    # 254th member creation attempt -> 409 Conflict
    res_overflow = await client.put(
        "/teams/1/members/9999",
        json={"name": "OverflowMember"},
    )
    assert res_overflow.status_code == 409
    assert "exhausted" in res_overflow.json()["detail"].lower()


@pytest.mark.asyncio
async def test_client_provided_public_key(teams_test_env):
    client = teams_test_env["client"]
    await client.put("/teams/1", json={"name": "Team Custom Key"})

    custom_pub = "ClientProvidedCustomPublicKey1234567890="
    res = await client.put(
        "/teams/1/members/888",
        json={
            "name": "CustomKeyUser",
            "public_key": custom_pub,
        },
    )
    assert res.status_code == 201
    data = res.json()
    assert data["public_key"] == custom_pub
    assert data["private_key"] is None
    assert "# PrivateKey generated client-side" in data["config_text"]


@pytest.mark.asyncio
async def test_member_not_found(teams_test_env):
    client = teams_test_env["client"]
    await client.put("/teams/1", json={"name": "Team One"})

    # Non-existent member -> 404
    assert (await client.get("/teams/1/members/non-existent")).status_code == 404
    assert (await client.get("/teams/1/members/non-existent/config")).status_code == 404
    assert (await client.delete("/teams/1/members/non-existent")).status_code == 404


@pytest.mark.asyncio
async def test_lock_timeout_handling(teams_test_env, monkeypatch):
    client = teams_test_env["client"]
    locks = teams_test_env["locks"]

    from contextlib import asynccontextmanager
    @asynccontextmanager
    async def timeout_lock(team_k, timeout=None):
        raise LockTimeoutError(resource=f"team:{team_k}", timeout=0.1)
        yield

    monkeypatch.setattr(locks, "lock_team", timeout_lock)

    # 1. Team creation with lock timeout -> 503
    res_team = await client.put("/teams/1", json={"name": "TimeoutTeam"})
    assert res_team.status_code == 503

    # 2. Member creation with lock timeout -> 503
    res_mem = await client.put("/teams/1/members/1", json={"name": "TimeoutUser"})
    assert res_mem.status_code == 503

    # 3. Delete team with lock timeout -> 503
    res_del = await client.delete("/teams/1")
    assert res_del.status_code == 503
