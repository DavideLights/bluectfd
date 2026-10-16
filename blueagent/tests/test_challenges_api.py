"""Unit tests for stateless container deployment, lifecycle, and verification endpoints in api/challenges.py."""

import asyncio
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from blueagent.config import settings
from blueagent.database import DatabaseManager
from blueagent.engine.base import ContainerEngine
from blueagent.locks import LockRegistry
from blueagent.main import app, lifespan
from blueagent.models import ContainerSpec, ExecResult
from blueagent.network._runner import CommandRunner, CompletedCommand
from blueagent.network.host_net import HostNetworkManager
from blueagent.network.wireguard import WireGuardManager, generate_keypair


class DummyEngine(ContainerEngine):
    """In-memory dummy engine for testing challenge endpoints."""

    def __init__(self):
        self.aliases = {}
        self.images = set()
        self.instances = {}
        self.files = {}
        self.started = set()
        self.fail_create = False
        self.fail_exec = False
        self.executed_commands = []

    async def create_instance(self, spec: ContainerSpec, devices=None, extra_config=None) -> str:
        if self.fail_create:
            raise RuntimeError("Engine instance creation failed")
        self.instances[spec.name] = {
            "spec": spec,
            "devices": devices,
            "extra_config": extra_config,
        }
        return spec.name

    async def push_file(self, name: str, path: str, content: str | bytes, mode: str = "0644", uid: int = 0, gid: int = 0) -> None:
        self.files[(name, path)] = content

    async def image_alias_exists(self, alias: str) -> bool:
        return alias in self.aliases

    async def import_image(self, tarball_path: str) -> str:
        fp = f"fp-{Path(tarball_path).stem}"
        self.images.add(fp)
        return fp

    async def create_image_alias(self, alias: str, fingerprint: str) -> None:
        self.aliases[alias] = fingerprint

    async def delete_image_alias(self, alias: str) -> None:
        self.aliases.pop(alias, None)

    async def delete_image(self, fingerprint: str) -> None:
        self.images.discard(fingerprint)

    async def start_instance(self, name: str) -> None:
        self.started.add(name)

    async def stop_instance(self, name: str, force: bool = False) -> None:
        self.started.discard(name)

    async def delete_instance(self, name: str, force: bool = False) -> None:
        self.instances.pop(name, None)
        self.started.discard(name)

    async def clone_instance(self, source: str, target: str) -> str:
        self.instances[target] = {"cloned_from": source}
        self.started.add(target)
        return target

    async def connect_network(self, instance_name: str, network_name: str, ip_address=None) -> None:
        pass

    async def disconnect_network(self, instance_name: str, network_name: str) -> None:
        pass

    async def exec_command(self, name: str, cmd, env=None, timeout: int = 15) -> ExecResult:
        self.executed_commands.append((name, cmd))
        if self.fail_exec:
            return ExecResult(exit_code=1, stdout="", stderr="command failed")
        return ExecResult(exit_code=0, stdout="ok", stderr="")

    async def get_instance_status(self, name: str) -> str:
        return "RUNNING" if name in self.started else "STOPPED"

    async def create_network(self, name: str, config=None):
        return {}

    async def delete_network(self, name: str) -> None:
        pass

    async def list_instances(self, prefix=None):
        names = list(self.instances.keys())
        if prefix:
            return [n for n in names if n.startswith(prefix)]
        return names

    async def instance_exists(self, name: str) -> bool:
        return name in self.instances

    async def aclose(self) -> None:
        pass


class MockHostRunner(CommandRunner):
    """Mock CommandRunner simulating ip link show and bridge commands."""

    def __init__(self):
        self.active_bridges = set()

    async def run(self, argv, *, stdin=None, check=True, timeout=15.0):
        cmd = list(argv)
        # Check bridge show
        if cmd[:5] == ["ip", "-json", "link", "show", "dev"]:
            dev = cmd[5]
            if dev in self.active_bridges:
                return CompletedCommand(argv=cmd, returncode=0, stdout='[{"ifname": "' + dev + '", "operstate": "UP"}]', stderr="")
            return CompletedCommand(argv=cmd, returncode=1, stdout="", stderr="Device does not exist")
        return CompletedCommand(argv=cmd, returncode=0, stdout="", stderr="")


@pytest.fixture
async def challenge_test_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_challenges.db"
    test_locks = tmp_path / "test_ch_locks"
    test_builds = tmp_path / "test_ch_builds"
    wg_dir = tmp_path / "test_wg"

    monkeypatch.setattr(settings, "database_path", str(test_db))
    monkeypatch.setattr(settings, "lock_directory", str(test_locks))
    monkeypatch.setattr(settings, "image_build_dir", str(test_builds))
    monkeypatch.setattr(settings, "wireguard_config_dir", str(wg_dir))
    monkeypatch.setattr(settings, "ctfd_api_token", "secret-test-token")
    monkeypatch.setattr(settings, "readiness_retry_interval", 0.05)

    engine = DummyEngine()
    runner = MockHostRunner()
    host_net = HostNetworkManager(runner=runner, config=settings)
    wg = WireGuardManager(runner=runner, config=settings)

    async with lifespan(app):
        app.state.engine = engine
        app.state.host_net = host_net
        app.state.wg = wg
        db = app.state.db

        auth_headers = {"Authorization": "Bearer secret-test-token"}
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=auth_headers,
        ) as ac:
            yield {
                "client": ac,
                "db": db,
                "engine": engine,
                "runner": runner,
                "host_net": host_net,
            }


@pytest.mark.asyncio
async def test_challenge_deploy_happy_path(challenge_test_env):
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    # 1. Setup team 1 and its bridge
    await db.create_team(team_k=1, name="Team 1", bridge_name="br-team-1", wg_interface="wg-1")
    runner.active_bridges.add("br-team-1")

    # 2. Setup distrobuilder image in ready state
    await db.claim_image(alias="distro-vuln", yaml_sha256="sha-vuln", yaml_text="image: debian")
    await db.set_image_status("distro-vuln", "ready", ["building"], fingerprint="fp-vuln")
    engine.aliases["distro-vuln"] = "fp-vuln"

    # 3. Deploy challenge
    payload = {
        "challenge_id": "sqli-web",
        "team_k": 1,
        "containers": [
            {
                "name": "app",
                "image": "distro-vuln",
                "ip_suffix": 2,
                "limits_cpu": "2",
                "limits_memory": "512MB",
            },
            {
                "name": "db",
                "image": "distro-vuln",
                "ip_suffix": 3,
                "limits_cpu": "1",
                "limits_memory": "256MB",
            },
        ],
    }

    res = await client.post(f"/teams/{payload['team_k']}/machines", json=payload)
    assert res.status_code == 201
    data = res.json()
    assert data["team_k"] == 1
    assert data["status"] == "running"
    assert len(data["containers"]) == 2

    # Verify container creation parameters (c-{team_k}-{spec_name})
    assert "c-1-app" in engine.instances
    assert "c-1-db" in engine.instances
    assert "c-1-app" in engine.started
    assert "c-1-db" in engine.started

    app_devs = engine.instances["c-1-app"]["devices"]
    assert app_devs["eth0"]["parent"] == "br-team-1"
    assert app_devs["eth0"]["nictype"] == "bridged"

    # Verify pushed networkd config
    app_net_conf = engine.files.get(("c-1-app", "/etc/systemd/network/10-eth0.network"))
    assert "Address=10.1.1.2/24" in app_net_conf
    assert "Gateway=10.1.1.1" in app_net_conf

    # Verify database persistence in running_containers
    c_app = await db.get_container("c-1-app")
    assert c_app is not None
    assert c_app.spec_name == "app"


@pytest.mark.asyncio
async def test_challenge_deploy_idempotency_and_container_reuse(challenge_test_env):
    """Test Problem 1: Reusing the same container across challenges."""
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=2, name="Team 2", bridge_name="br-team-2", wg_interface="wg-2")
    runner.active_bridges.add("br-team-2")
    await db.claim_image(alias="img-shared", yaml_sha256="sha-shared", yaml_text="yaml")
    await db.set_image_status("img-shared", "ready", ["building"], fingerprint="fp-shared")
    engine.aliases["img-shared"] = "fp-shared"

    # Deploy Challenge A with container 'web'
    chal_a = {
        "challenge_id": "chal-a",
        "team_k": 2,
        "containers": [{"name": "web", "image": "img-shared", "ip_suffix": 5}],
    }
    res_a = await client.post(f"/teams/{chal_a['team_k']}/machines", json=chal_a)
    assert res_a.status_code == 201
    assert "c-2-web" in engine.instances

    # Re-deploying Challenge A is idempotent
    res_a_repeat = await client.post(f"/teams/{chal_a['team_k']}/machines", json=chal_a)
    assert res_a_repeat.status_code == 201
    assert len(res_a_repeat.json()["containers"]) == 1

    # Deploy Challenge B reusing 'web' and adding 'db'
    chal_b = {
        "challenge_id": "chal-b",
        "team_k": 2,
        "containers": [
            {"name": "web", "image": "img-shared", "ip_suffix": 5},
            {"name": "db", "image": "img-shared", "ip_suffix": 6},
        ],
    }
    res_b = await client.post(f"/teams/{chal_b['team_k']}/machines", json=chal_b)
    assert res_b.status_code == 201
    assert len(res_b.json()["containers"]) == 2

    # 'c-2-web' was reused, 'c-2-db' was created
    assert "c-2-web" in engine.instances
    assert "c-2-db" in engine.instances


@pytest.mark.asyncio
async def test_challenge_partial_teardown(challenge_test_env):
    """Test partial teardown: tear down container A while leaving container B running."""
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=3, name="Team 3", bridge_name="br-team-3", wg_interface="wg-3")
    runner.active_bridges.add("br-team-3")
    await db.claim_image(alias="img-3", yaml_sha256="sha-3", yaml_text="yaml")
    await db.set_image_status("img-3", "ready", ["building"], fingerprint="fp-3")
    engine.aliases["img-3"] = "fp-3"

    payload = {
        "challenge_id": "chal-3",
        "team_k": 3,
        "containers": [
            {"name": "web", "image": "img-3", "ip_suffix": 10},
            {"name": "worker", "image": "img-3", "ip_suffix": 11},
        ],
    }
    await client.post(f"/teams/{payload['team_k']}/machines", json=payload)
    assert "c-3-web" in engine.instances
    assert "c-3-worker" in engine.instances

    # Teardown only 'web' container
    del_payload = {
        "challenge_id": "chal-3",
        "team_k": 3,
        "containers": [{"name": "web", "image": "img-3", "ip_suffix": 10}],
    }
    del_res = await client.request("DELETE", "/teams/3/machines", json=del_payload)
    assert del_res.status_code == 204

    # 'web' container deleted, 'worker' still running
    assert "c-3-web" not in engine.instances
    assert await db.get_container("c-3-web") is None
    assert "c-3-worker" in engine.instances
    assert await db.get_container("c-3-worker") is not None


@pytest.mark.asyncio
async def test_challenge_deploy_validations(challenge_test_env):
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=4, name="Team 4", bridge_name="br-team-4", wg_interface="wg-4")
    runner.active_bridges.add("br-team-4")

    # Image not in DB
    res_no_img = await client.post(
        "/teams/4/machines",
        json={
            "challenge_id": "test-ch",
            "team_k": 4,
            "containers": [{"name": "app", "image": "images:debian/12", "ip_suffix": 2}],
        },
    )
    assert res_no_img.status_code == 422

    # Image in DB but not ready
    await db.claim_image(alias="building-img", yaml_sha256="sha-b", yaml_text="yaml")
    res_bld = await client.post(
        "/teams/4/machines",
        json={
            "challenge_id": "test-ch",
            "team_k": 4,
            "containers": [{"name": "app", "image": "building-img", "ip_suffix": 2}],
        },
    )
    assert res_bld.status_code == 422

    # Team does not exist
    res_no_team = await client.post(
        "/teams/99/machines",
        json={
            "challenge_id": "test-ch",
            "team_k": 99,
            "containers": [{"name": "app", "image": "building-img", "ip_suffix": 2}],
        },
    )
    assert res_no_team.status_code == 404

    # Bridge down
    runner.active_bridges.remove("br-team-4")
    await db.set_image_status("building-img", "ready", ["building"], fingerprint="fp-b")
    engine.aliases["building-img"] = "fp-b"
    res_no_bridge = await client.post(
        "/teams/4/machines",
        json={
            "challenge_id": "test-ch",
            "team_k": 4,
            "containers": [{"name": "app", "image": "building-img", "ip_suffix": 2}],
        },
    )
    assert res_no_bridge.status_code == 409


@pytest.mark.asyncio
async def test_teardown_and_team_delete_cascade(challenge_test_env):
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=5, name="Team 5", bridge_name="br-team-5", wg_interface="wg-5")
    runner.active_bridges.add("br-team-5")
    await db.claim_image(alias="img-5", yaml_sha256="sha-5", yaml_text="yaml")
    await db.set_image_status("img-5", "ready", ["building"], fingerprint="fp-5")
    engine.aliases["img-5"] = "fp-5"

    payload = {
        "challenge_id": "team-del-ch",
        "team_k": 5,
        "containers": [{"name": "web", "image": "img-5", "ip_suffix": 20}],
    }
    await client.post(f"/teams/{payload['team_k']}/machines", json=payload)
    assert "c-5-web" in engine.instances

    # Delete team 5 -> cascades container teardown
    del_team_res = await client.delete("/teams/5")
    assert del_team_res.status_code == 204

    # Instances and containers should be gone
    assert "c-5-web" not in engine.instances
    assert await db.get_container("c-5-web") is None
    assert await db.get_team_by_k(5) is None


@pytest.mark.asyncio
async def test_verify_readiness_then_verify_commands(challenge_test_env):
    """Test Problem 2: Readiness probes run on clones in sandbox before verify commands."""
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=6, name="Team 6", bridge_name="br-team-6", wg_interface="wg-6")
    runner.active_bridges.add("br-team-6")
    await db.claim_image(alias="img-6", yaml_sha256="sha-6", yaml_text="yaml")
    await db.set_image_status("img-6", "ready", ["building"], fingerprint="fp-6")
    engine.aliases["img-6"] = "fp-6"

    # Deploy container
    payload = {
        "challenge_id": "verify-ch",
        "team_k": 6,
        "containers": [{"name": "app", "image": "img-6", "ip_suffix": 30}],
    }
    await client.post(f"/teams/{payload['team_k']}/machines", json=payload)

    # Verification payload with readiness_commands AND verify_commands
    verify_payload = {
        "challenge_id": "verify-ch",
        "team_k": 6,
        "containers": [{"name": "app", "image": "img-6", "ip_suffix": 30}],
        "readiness_commands": [
            {
                "target": "container",
                "container_name": "app",
                "cmd": ["check", "readiness"],
                "timeout": 2,
            }
        ],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "app",
                "cmd": ["check", "exploit"],
                "timeout": 2,
            }
        ],
    }

    engine.executed_commands.clear()
    res = await client.post(f"/teams/{verify_payload['team_k']}/verificator", json=verify_payload)
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["success"] is True
    assert res_data["passed"] is True

    # Assert readiness ran on clone before verify command
    cmds = [c[1] for c in engine.executed_commands]
    assert ["check", "readiness"] in cmds
    assert ["check", "exploit"] in cmds
    readiness_idx = cmds.index(["check", "readiness"])
    verify_idx = cmds.index(["check", "exploit"])
    assert readiness_idx < verify_idx


@pytest.mark.asyncio
async def test_verify_readiness_failure_stops_verification(challenge_test_env):
    """Test Problem 2: If readiness fails in sandbox, verification stops and verify_commands never run."""
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=7, name="Team 7", bridge_name="br-team-7", wg_interface="wg-7")
    runner.active_bridges.add("br-team-7")
    await db.claim_image(alias="img-7", yaml_sha256="sha-7", yaml_text="yaml")
    await db.set_image_status("img-7", "ready", ["building"], fingerprint="fp-7")
    engine.aliases["img-7"] = "fp-7"

    payload = {
        "challenge_id": "verify-fail-ch",
        "team_k": 7,
        "containers": [{"name": "app", "image": "img-7", "ip_suffix": 40}],
    }
    await client.post(f"/teams/{payload['team_k']}/machines", json=payload)

    # Make exec commands fail
    engine.fail_exec = True

    verify_payload = {
        "challenge_id": "verify-fail-ch",
        "team_k": 7,
        "containers": [{"name": "app", "image": "img-7", "ip_suffix": 40}],
        "readiness_commands": [
            {
                "target": "container",
                "container_name": "app",
                "cmd": ["check", "readiness"],
                "timeout": 1,
            }
        ],
        "verify_commands": [
            {
                "target": "container",
                "container_name": "app",
                "cmd": ["check", "exploit"],
                "timeout": 1,
            }
        ],
    }

    engine.executed_commands.clear()
    res = await client.post(f"/teams/{verify_payload['team_k']}/verificator", json=verify_payload)
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["passed"] is False
    assert "readiness check failed" in res_data["message"].lower()

    # Verify that verify_commands were NOT executed
    cmds = [c[1] for c in engine.executed_commands]
    assert ["check", "exploit"] not in cmds


@pytest.mark.asyncio
async def test_challenge_get_and_list(challenge_test_env):
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]

    await db.create_team(team_k=8, name="Team 8", bridge_name="br-team-8", wg_interface="wg-8")
    await db.claim_image(alias="img-8", yaml_sha256="sha-8", yaml_text="yaml")
    await db.set_image_status("img-8", "ready", ["building"], fingerprint="fp-8")

    await db.record_container(
        instance_name="c-8-web",
        team_k=8,
        spec_name="web",
        ip_address="10.8.1.2",
        image_alias="img-8",
    )

    # 1. GET /challenges (removed -> 404 or 405)
    all_res = await client.get("/challenges")
    assert all_res.status_code in (404, 405)

    # 2. GET /teams/8/machines (found running containers)
    found_res = await client.get("/teams/8/machines")
    assert found_res.status_code == 200
    containers = found_res.json()
    assert len(containers) == 1
    assert containers[0]["spec_name"] == "web"
    assert containers[0]["team_k"] == 8

    # 3. GET /teams/99/machines (nonexistent team -> 404)
    missing_res = await client.get("/teams/99/machines")
    assert missing_res.status_code == 404

    # 4. Provision empty team 99 -> returns 200 with empty list
    await db.create_team(team_k=99, name="Team 99", bridge_name="br-team-99", wg_interface="wg-99")
    empty_res = await client.get("/teams/99/machines")
    assert empty_res.status_code == 200
    assert len(empty_res.json()) == 0


@pytest.mark.asyncio
async def test_challenge_lock_timeout(challenge_test_env, monkeypatch):
    client = challenge_test_env["client"]
    from blueagent.locks import LockTimeoutError
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_timeout_lock(team_k):
        raise LockTimeoutError(f"team:{team_k}", 0.1)
        yield

    monkeypatch.setattr(challenge_test_env["client"]._transport.app.state.locks, "lock_team", mock_timeout_lock)

    res = await client.post(
        "/teams/9/machines",
        json={"challenge_id": "locked-ch", "team_k": 9, "containers": []},
    )
    assert res.status_code == 503
    assert "Timed out" in res.json()["detail"]


@pytest.mark.asyncio
async def test_challenge_verifier_image_must_be_ready(challenge_test_env):
    client = challenge_test_env["client"]
    db = challenge_test_env["db"]
    engine = challenge_test_env["engine"]
    runner = challenge_test_env["runner"]

    await db.create_team(team_k=10, name="Team 10", bridge_name="br-team-10", wg_interface="wg-10")
    runner.active_bridges.add("br-team-10")
    # Image for container is ready, but verifier image is not ready / missing
    await db.claim_image(alias="cont-img", yaml_sha256="sha-cont", yaml_text="yaml")
    await db.set_image_status("cont-img", "ready", ["building"], fingerprint="fp-cont")
    engine.aliases["cont-img"] = "fp-cont"

    payload = {
        "challenge_id": "chal-verifier-img-test",
        "team_k": 10,
        "containers": [
            {"name": "web", "image": "cont-img", "ip_suffix": 10, "limits_cpu": "1", "limits_memory": "128MB"}
        ],
        "verifier": {
            "image": "missing-verifier-img",
            "ip_suffix": 250,
            "limits_cpu": "1",
            "limits_memory": "128MB",
        },
    }
    res = await client.post("/teams/10/machines", json=payload)
    assert res.status_code == 422
    assert "Verifier image 'missing-verifier-img' is not ready" in res.json()["detail"]
