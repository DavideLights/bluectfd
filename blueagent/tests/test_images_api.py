"""Unit tests for Distrobuilder image management and api/images.py endpoints."""

import asyncio
from io import BytesIO
from pathlib import Path
import pytest
from httpx import ASGITransport, AsyncClient

from blueagent.config import settings
from blueagent.database import DatabaseManager
from blueagent.engine.base import ContainerEngine
from blueagent.images.distrobuilder import ImageBuilder
from blueagent.locks import LockRegistry
from blueagent.main import app, lifespan
from blueagent.models import ContainerSpec, ExecResult


class DummyEngine(ContainerEngine):
    """In-memory dummy engine for testing image and challenge endpoints."""

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
async def images_test_env(tmp_path, monkeypatch):
    test_db = tmp_path / "test_images.db"
    test_locks = tmp_path / "test_images_locks"
    test_builds = tmp_path / "test_builds"

    monkeypatch.setattr(settings, "database_path", str(test_db))
    monkeypatch.setattr(settings, "lock_directory", str(test_locks))
    monkeypatch.setattr(settings, "image_build_dir", str(test_builds))
    monkeypatch.setattr(settings, "ctfd_api_token", "secret-test-token")
    monkeypatch.setattr(settings, "image_yaml_max_bytes", 500)

    engine = DummyEngine()

    async with lifespan(app):
        app.state.engine = engine
        db = app.state.db
        locks = app.state.locks
        builder = ImageBuilder(db=db, engine=engine, locks=locks)
        app.state.image_builder = builder

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
                "builder": builder,
            }


@pytest.mark.asyncio
async def test_images_api_auth(images_test_env):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as unauth:
        res = await unauth.get("/images")
        assert res.status_code == 401


@pytest.mark.asyncio
async def test_images_api_upload_and_lifecycle(images_test_env):
    client = images_test_env["client"]
    db = images_test_env["db"]

    # 1. Invalid YAML
    bad_yaml = b"image: [unbalanced"
    res = await client.put(
        "/images/debian-curl",
        files={"file": ("def.yaml", bad_yaml, "application/x-yaml")},
    )
    assert res.status_code == 422

    # 2. Oversized YAML
    big_yaml = b"image: debian\n" + b" " * 600
    res = await client.put(
        "/images/debian-curl",
        files={"file": ("def.yaml", big_yaml, "application/x-yaml")},
    )
    assert res.status_code == 413

    # 3. Invalid alias pattern
    valid_yaml = b"image: debian\npackages:\n  - curl\n"
    res = await client.put(
        "/images/INVALID_ALIAS!",
        files={"file": ("def.yaml", valid_yaml, "application/x-yaml")},
    )
    assert res.status_code == 422

    # 4. Valid upload via PUT /images/{alias}
    res = await client.put(
        "/images/debian-curl",
        files={"file": ("def.yaml", valid_yaml, "application/x-yaml")},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["alias"] == "debian-curl"
    assert data["status"] == "building"

    # 5. Get image
    get_res = await client.get("/images/debian-curl")
    assert get_res.status_code == 200
    assert get_res.json()["status"] == "building"

    # 6. List images
    list_res = await client.get("/images")
    assert list_res.status_code == 200
    assert len(list_res.json()) == 1

    # 7. Get logs endpoint
    log_res = await client.get("/images/debian-curl/logs")
    assert log_res.status_code == 200
    assert "text/plain" in log_res.headers["content-type"]

    # 8. Manually mark ready in DB to test cache-hit return (idempotency)
    await db.set_image_status("debian-curl", "ready", ["building"], fingerprint="fp-12345")

    res_hit = await client.put(
        "/images/debian-curl",
        files={"file": ("def.yaml", valid_yaml, "application/x-yaml")},
    )
    assert res_hit.status_code == 200
    assert res_hit.json()["status"] == "ready"

    # 9. Modify YAML on same alias -> triggers rebuild
    updated_yaml = b"image: debian\npackages:\n  - curl\n  - git\n"
    res_update = await client.put(
        "/images/debian-curl",
        files={"file": ("def.yaml", updated_yaml, "application/x-yaml")},
    )
    assert res_update.status_code == 202
    assert res_update.json()["status"] == "building"

    # 10. Mark ready again and test ?force=true
    await db.set_image_status("debian-curl", "ready", ["building"], fingerprint="fp-22222")
    res_force = await client.put(
        "/images/debian-curl?force=true",
        files={"file": ("def.yaml", updated_yaml, "application/x-yaml")},
    )
    assert res_force.status_code == 202
    assert res_force.json()["status"] == "building"

    # 11. Delete image
    del_res = await client.delete("/images/debian-curl")
    assert del_res.status_code == 204
    assert await db.get_image("debian-curl") is None


@pytest.mark.asyncio
async def test_images_api_archive_upload(images_test_env):
    import tarfile
    client = images_test_env["client"]
    db = images_test_env["db"]

    # Build in-memory .tar.gz archive containing definition.yaml and auxiliary source
    buf = BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        def_content = b"image:\n  distribution: debian\npackages:\n  - python3\n"
        ti_def = tarfile.TarInfo(name="definition.yaml")
        ti_def.size = len(def_content)
        tf.addfile(ti_def, BytesIO(def_content))

        app_content = b"print('Hello CTF')\n"
        ti_app = tarfile.TarInfo(name="files/app.py")
        ti_app.size = len(app_content)
        tf.addfile(ti_app, BytesIO(app_content))

    archive_bytes = buf.getvalue()

    # Upload archive via PUT /images/{alias}
    res = await client.put(
        "/images/chal-archive",
        files={"file": ("context.tar.gz", archive_bytes, "application/gzip")},
    )
    assert res.status_code == 202
    data = res.json()
    assert data["alias"] == "chal-archive"
    assert data["status"] == "building"

    # Check context archive was saved for distrobuilder
    build_dir = Path(settings.image_build_dir)
    context_file = build_dir / "chal-archive-context.bin"
    assert context_file.is_file()

    # Verify logs endpoint
    log_res = await client.get("/images/chal-archive/logs")
    assert log_res.status_code == 200

    # Clean up
    await client.delete("/images/chal-archive")
    assert not context_file.is_file()


@pytest.mark.asyncio
async def test_images_delete_in_use_conflict(images_test_env):
    client = images_test_env["client"]
    db = images_test_env["db"]

    # Create team and ready image
    await db.create_team(team_k=1, name="T1", bridge_name="br-1", wg_interface="wg-1")
    await db.claim_image(alias="in-use-img", yaml_sha256="sha-1", yaml_text="yaml")
    await db.set_image_status("in-use-img", "ready", ["building"], fingerprint="fp-1")

    # Record container that references the image
    await db.record_container(
        instance_name="ch-1-ch-ref-app",
        team_k=1,
        spec_name="app",
        ip_address="10.1.1.2",
        image_alias="in-use-img",
    )

    # Deleting in-use image must fail with 409 Conflict
    res = await client.delete("/images/in-use-img")
    assert res.status_code == 409
    assert any(term in res.json()["detail"].lower() for term in ["reference", "in use"])
