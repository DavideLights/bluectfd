"""Unit tests for ImageBuilder and teardown_challenge services."""

import asyncio
from pathlib import Path
import pytest

from blueagent.config import settings
from blueagent.database import DatabaseManager
from blueagent.images.distrobuilder import ImageBuilder
from blueagent.locks import LockRegistry
from blueagent.engine.base import ContainerEngine
from blueagent.models import ContainerSpec, ExecResult
from blueagent.services.challenges import TeardownError, teardown_challenge


class DummyEngine(ContainerEngine):
    """In-memory dummy engine for testing services."""

    def __init__(self):
        self.aliases = {}
        self.images = set()
        self.instances = {}
        self.files = {}
        self.started = set()
        self.fail_create = False
        self.fail_exec = False

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
        return target

    async def connect_network(self, instance_name: str, network_name: str, ip_address=None) -> None:
        pass

    async def disconnect_network(self, instance_name: str, network_name: str) -> None:
        pass

    async def exec_command(self, name: str, cmd, env=None, timeout: int = 15) -> ExecResult:
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


@pytest.fixture
async def service_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_services.db"
    test_locks = tmp_path / "test_svc_locks"
    test_builds = tmp_path / "test_svc_builds"
    test_wg = tmp_path / "test_svc_wg"
    test_wg.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(settings, "database_path", str(test_db))
    monkeypatch.setattr(settings, "lock_directory", str(test_locks))
    monkeypatch.setattr(settings, "image_build_dir", str(test_builds))
    monkeypatch.setattr(settings, "wireguard_config_dir", str(test_wg))
    monkeypatch.setattr(settings, "use_sudo_distrobuilder", False)

    db = DatabaseManager(db_path=str(test_db))
    await db.init_db()

    locks = LockRegistry(lock_dir=str(test_locks))
    engine = DummyEngine()
    builder = ImageBuilder(db=db, engine=engine, locks=locks)

    return {
        "db": db,
        "locks": locks,
        "engine": engine,
        "builder": builder,
        "builds_dir": test_builds,
    }


@pytest.mark.asyncio
async def test_teardown_challenge_success(service_env):
    db = service_env["db"]
    engine = service_env["engine"]

    await db.create_team(team_k=1, name="Team 1", bridge_name="br-1", wg_interface="wg-1")
    await db.claim_image(alias="img-1", yaml_sha256="sha-1", yaml_text="yaml")
    await db.set_image_status("img-1", "ready", ["building"], fingerprint="fp-1")

    # Record containers
    await db.record_container("ch-1-ch-test-app", 1, "app", "10.1.1.2", "img-1")
    await db.record_container("ch-1-ch-test-db", 1, "db", "10.1.1.3", "img-1")
    engine.instances["ch-1-ch-test-app"] = {}
    engine.instances["ch-1-ch-test-db"] = {}

    # Teardown
    await teardown_challenge("ch-test", 1, db, engine)

    # Instances deleted from engine and challenge removed from DB
    assert "ch-1-ch-test-app" not in engine.instances
    assert "ch-1-ch-test-db" not in engine.instances
    assert await db.list_team_containers(1) == []


@pytest.mark.asyncio
async def test_teardown_challenge_noop_if_missing(service_env):
    db = service_env["db"]
    engine = service_env["engine"]

    # Calling teardown on non-existent challenge is safe/noop
    await teardown_challenge("non-existent", 1, db, engine)


@pytest.mark.asyncio
async def test_teardown_challenge_error_handling(service_env, monkeypatch):
    db = service_env["db"]
    engine = service_env["engine"]

    await db.create_team(team_k=2, name="Team 2", bridge_name="br-2", wg_interface="wg-2")
    await db.claim_image(alias="img-2", yaml_sha256="sha-2", yaml_text="yaml")
    await db.set_image_status("img-2", "ready", ["building"], fingerprint="fp-2")

    await db.record_container("ch-2-ch-err-app", 2, "app", "10.2.1.2", "img-2")

    # Make delete_instance fail
    async def fake_delete(name, force=False):
        raise RuntimeError("Incus daemon uncontactable")

    monkeypatch.setattr(engine, "delete_instance", fake_delete)

    with pytest.raises(TeardownError) as exc_info:
        await teardown_challenge("ch-err", 2, db, engine)
    assert "Incus daemon uncontactable" in str(exc_info.value)

    # Container must still remain in DB because deletion failed
    c = await db.get_container("ch-2-ch-err-app")
    assert c is not None


@pytest.mark.asyncio
async def test_image_builder_skip_if_not_building(service_env):
    builder = service_env["builder"]
    db = service_env["db"]

    await db.claim_image(alias="img-ready", yaml_sha256="sha-r", yaml_text="yaml")
    await db.set_image_status("img-ready", "ready", ["building"], fingerprint="fp-r")

    # Should immediately return without running distrobuilder
    await builder.build_image("img-ready")
    img = await db.get_image("img-ready")
    assert img.status == "ready"


@pytest.mark.asyncio
async def test_image_builder_success(service_env, monkeypatch):
    builder = service_env["builder"]
    db = service_env["db"]
    engine = service_env["engine"]

    rec, _ = await db.claim_image(alias="build-ok", yaml_sha256="sha-ok", yaml_text="image: debian")

    # Mock subprocess to write fake unified tarball
    class FakeProc:
        returncode = 0
        async def communicate(self):
            return b"build success", b""
        def kill(self):
            pass
        async def wait(self):
            return 0

    async def fake_subproc(*cmd, cwd=None, stdout=None, stderr=None):
        if cwd:
            tar = Path(cwd) / "incus.tar.xz"
            tar.write_bytes(b"unified image tarball content")
        return FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subproc)

    await builder.build_image("build-ok")

    img = await db.get_image("build-ok")
    assert img.status == "ready"
    assert img.fingerprint is not None
    assert "build-ok" in engine.aliases


@pytest.mark.asyncio
async def test_image_builder_failure(service_env, monkeypatch):
    builder = service_env["builder"]
    db = service_env["db"]

    rec, _ = await db.claim_image(alias="build-fail", yaml_sha256="sha-fail", yaml_text="image: bad")

    class FakeFailProc:
        returncode = 1
        async def communicate(self):
            return b"", b"Error: debootstrap failed: package not found"
        def kill(self):
            pass
        async def wait(self):
            return 1

    async def fake_fail_subproc(*cmd, cwd=None, stdout=None, stderr=None):
        return FakeFailProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_fail_subproc)

    await builder.build_image("build-fail")

    img = await db.get_image("build-fail")
    assert img.status == "failed"
    assert "debootstrap failed" in img.error


from blueagent.models import ChallengeConfig, ContainerSpec
from blueagent.network._runner import CommandRunner, CompletedCommand
from blueagent.network.host_net import HostNetworkManager
from blueagent.network.wireguard import WireGuardManager
from blueagent.services.challenges import (
    BridgeNotActiveError,
    ChallengeService,
    ImageNotReadyError,
    TeamNotFoundError,
)
from blueagent.services.images import (
    ImageAliasInvalidError,
    ImageService,
    ImageYamlInvalidError,
)
from blueagent.services.teams import TeamService


class MockRunner(CommandRunner):
    def __init__(self):
        self.active_bridges = set()

    async def run(self, argv, *, stdin=None, check=True, timeout=15.0):
        cmd = list(argv)
        if cmd[:5] == ["ip", "-json", "link", "show", "dev"]:
            dev = cmd[5]
            if dev in self.active_bridges:
                return CompletedCommand(argv=cmd, returncode=0, stdout='[{"ifname": "' + dev + '", "operstate": "UP"}]', stderr="")
            return CompletedCommand(argv=cmd, returncode=1, stdout="", stderr="Device does not exist")
        return CompletedCommand(argv=cmd, returncode=0, stdout="", stderr="")


@pytest.mark.asyncio
async def test_challenge_service_deploy_and_teardown(service_env):
    db = service_env["db"]
    engine = service_env["engine"]
    locks = service_env["locks"]
    runner = MockRunner()
    host_net = HostNetworkManager(runner=runner, config=settings)

    svc = ChallengeService(db=db, engine=engine, host_net=host_net, locks=locks)

    # 1. Team not found
    cfg = ChallengeConfig(
        challenge_id="ch-svc",
        containers=[ContainerSpec(name="web", image="img-svc", ip_suffix=2)],
    )
    with pytest.raises(TeamNotFoundError):
        await svc.deploy_challenge(10, cfg)

    # 2. Bridge not active
    await db.create_team(team_k=10, name="Team 10", bridge_name="br-team-10", wg_interface="wg-10")
    with pytest.raises(BridgeNotActiveError):
        await svc.deploy_challenge(10, cfg)

    # 3. Image not ready
    runner.active_bridges.add("br-team-10")
    with pytest.raises(ImageNotReadyError):
        await svc.deploy_challenge(10, cfg)

    # 4. Successful deploy
    await db.claim_image(alias="img-svc", yaml_sha256="sha-svc", yaml_text="image: debian")
    await db.set_image_status("img-svc", "ready", ["building"], fingerprint="fp-svc")
    engine.aliases["img-svc"] = "fp-svc"

    resp = await svc.deploy_challenge(10, cfg)
    assert resp.status == "running"
    assert "c-10-web" in engine.instances

    # 5. Teardown
    await svc.teardown_challenge(10, "ch-svc")
    assert "c-10-web" not in engine.instances
    assert await db.list_team_containers(10) == []


@pytest.mark.asyncio
async def test_image_service_operations(service_env, monkeypatch):
    db = service_env["db"]
    engine = service_env["engine"]
    builder = service_env["builder"]

    async def fake_build(alias):
        pass

    monkeypatch.setattr(builder, "build_image", fake_build)

    img_svc = ImageService(db=db, builder=builder, engine=engine)

    # Invalid YAML
    with pytest.raises(ImageYamlInvalidError):
        await img_svc.submit_image(b"not: [a: valid: yaml: :")

    # Invalid alias regex
    with pytest.raises(ImageAliasInvalidError):
        await img_svc.submit_image(b"image: test", alias="INVALID_UPPERCASE")

    # Valid submit
    rec, is_pending = await img_svc.submit_image(b"image: debian\nversion: 12", alias="img-custom")
    assert rec.alias == "img-custom"
    assert is_pending is True

    # List & get
    assert len(await img_svc.list_images()) >= 1
    found = await img_svc.get_image("img-custom")
    assert found is not None

    # Delete
    deleted = await img_svc.delete_image("img-custom")
    assert deleted is True
    assert await img_svc.get_image("img-custom") is None


@pytest.mark.asyncio
async def test_team_service_operations(service_env):
    db = service_env["db"]
    engine = service_env["engine"]
    locks = service_env["locks"]
    runner = MockRunner()
    host_net = HostNetworkManager(runner=runner, config=settings)
    wg = WireGuardManager(runner=runner, config=settings)

    team_svc = TeamService(db=db, host_net=host_net, wg=wg, engine=engine, locks=locks)

    # Provision team
    team, is_new = await team_svc.create_or_update_team(team_k=20, name="Team 20")
    assert is_new is True
    assert team.team_k == 20

    # Add member
    member, is_new_m = await team_svc.add_member(
        team_k=20,
        user_id=101,
        name="Alice",
    )
    assert is_new_m is True
    assert member.client_ip == "10.20.0.2"

    # List members
    members = await team_svc.list_members(20)
    assert len(members) == 1
    assert members[0].user_id == 101
    assert members[0].name == "Alice"
    assert members[0].telemetry is not None

    # Teardown team
    await team_svc.teardown_team(20)
    assert await team_svc.get_team(20) is None
