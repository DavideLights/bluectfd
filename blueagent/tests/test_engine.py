"""Comprehensive tests for IncusEngine and OvnNetworkManager mocking Incus REST API."""

import json
import pytest
import httpx
from blueagent.engine.incus import IncusEngine
from blueagent.models import ContainerSpec
from blueagent.network.addressing import plan_for


@pytest.fixture
def mock_incus():
    """Builds a mock Incus HTTP transport for simulating daemon responses."""
    recorded_requests = []

    stopped_instances = set()

    def handler(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        url_path = request.url.path
        method = request.method

        # 1. Operation wait handler
        if "operations/" in url_path and url_path.endswith("/wait"):
            if "fail-op" in url_path:
                return httpx.Response(
                    200,
                    json={
                        "metadata": {
                            "status": "Failure",
                            "err": "ZFS snapshot creation failed: pool full",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "status": "Success",
                        "metadata": {
                            "return": 0,
                            "fingerprint": "fp-imported-12345",
                            "output": {"1": "/1.0/logs/exec.stdout", 2: "/1.0/logs/exec.stderr"},
                        },
                    }
                },
            )

        # 2. Instance state (start/stop)
        if "/1.0/instances/" in url_path and url_path.endswith("/state"):
            inst_name = url_path.split("/")[-2]
            if method == "PUT":
                content = json.loads(request.content.decode()) if request.content else {}
                action = content.get("action")
                if action == "stop":
                    stopped_instances.add(inst_name)
                if "already-running" in url_path and action == "start":
                    return httpx.Response(400, text="The instance is already running")
                if "already-stopped" in url_path and action == "stop":
                    return httpx.Response(409, text="The instance is already stopped")
                return httpx.Response(
                    202,
                    json={"operation": "/1.0/operations/op-state-123"},
                )
            if method == "GET":
                return httpx.Response(
                    200,
                    json={"metadata": {"status": "RUNNING" if inst_name not in stopped_instances else "STOPPED"}},
                )

        # 3. Instance metadata & exists
        if url_path.startswith("/1.0/instances/") and method == "GET":
            if "nonexistent" in url_path:
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(200, json={"metadata": {"name": url_path.split("/")[-1], "status": "RUNNING"}})

        # 4. Instance creation & list
        if url_path == "/1.0/instances":
            if method == "POST":
                return httpx.Response(
                    202,
                    json={"operation": "/1.0/operations/op-create-123"},
                )
            if method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "metadata": [
                            "/1.0/instances/web-prod-1",
                            "/1.0/instances/db-prod-1",
                            "/1.0/instances/verify-web-prod-1-uuid",
                            "/1.0/instances/verify-runner-uuid",
                        ]
                    },
                )

        # 5. Instance patch (connect / disconnect network)
        if "/1.0/instances/" in url_path and method == "PATCH":
            return httpx.Response(
                202,
                json={"operation": "/1.0/operations/op-patch-123"},
            )

        # 6. Instance delete
        if "/1.0/instances/" in url_path and method == "DELETE":
            inst_name = url_path.split("/")[-1]
            if "running-inst" in url_path and inst_name not in stopped_instances:
                return httpx.Response(400, text="Instance is running")
            if "nonexistent" in url_path:
                return httpx.Response(404, json={"error": "Not Found"})
            return httpx.Response(
                202,
                json={"operation": "/1.0/operations/op-del-123"},
            )

        # 7. Instance exec
        if "/1.0/instances/" in url_path and url_path.endswith("/exec"):
            return httpx.Response(
                202,
                json={"operation": "/1.0/operations/op-exec-123"},
            )

        # 8. Exec stdout / stderr logs
        if url_path == "/1.0/logs/exec.stdout":
            return httpx.Response(200, text="command output success")
        if url_path == "/1.0/logs/exec.stderr":
            return httpx.Response(200, text="warning message")

        # 9. Network leases
        if "/1.0/networks/" in url_path and url_path.endswith("/leases"):
            return httpx.Response(
                200,
                json={"metadata": [{"hostname": "target-app", "address": "10.1.1.2", "type": "static"}]},
            )

        # 10. Network endpoints
        if url_path == "/1.0/networks":
            if method == "POST":
                content = json.loads(request.content.decode()) if request.content else {}
                if content.get("name") == "existing-net":
                    return httpx.Response(409, text="Network 'existing-net' already exists")
                return httpx.Response(
                    202,
                    json={"operation": "/1.0/operations/op-net-123", "metadata": {"name": content.get("name")}},
                )
            if method == "GET":
                if request.url.params.get("recursion") == "1":
                    return httpx.Response(
                        200,
                        json={"metadata": [{"name": "incusbr0"}, {"name": "verify-test-1"}]},
                    )
                return httpx.Response(
                    200,
                    json={"metadata": ["/1.0/networks/incusbr0", "/1.0/networks/verify-test-1"]},
                )

        if "/1.0/networks/" in url_path and method == "GET":
            name = url_path.split("/")[-1]
            return httpx.Response(200, json={"metadata": {"name": name, "type": "ovn"}})

        if "/1.0/networks/" in url_path and method == "DELETE":
            if "in-use-net" in url_path:
                return httpx.Response(400, text="Network is currently used by 2 instances")
            return httpx.Response(
                202,
                json={"operation": "/1.0/operations/op-netdel-123"},
            )

        # 11. Instance files (push_file)
        if "/1.0/instances/" in url_path and "/files" in url_path and method == "POST":
            return httpx.Response(200, json={"type": "sync", "status": "Success"})

        # 12. Images and aliases
        if url_path.startswith("/1.0/images/aliases"):
            alias = url_path.split("/")[-1]
            if method == "GET":
                if alias == "existing-alias":
                    return httpx.Response(200, json={"metadata": {"name": "existing-alias", "target": "fp-123"}})
                return httpx.Response(404, json={"error": "Not Found"})
            if method == "POST":
                return httpx.Response(201, json={"type": "sync", "status": "Success"})
            if method == "DELETE":
                return httpx.Response(200, json={"type": "sync", "status": "Success"})

        if url_path == "/1.0/images" and method == "POST":
            return httpx.Response(202, json={"operation": "/1.0/operations/op-img-import"})

        if url_path.startswith("/1.0/images/") and method == "DELETE":
            return httpx.Response(202, json={"operation": "/1.0/operations/op-del-img"})

        return httpx.Response(404, json={"error": "Not Found"})

    transport = httpx.MockTransport(handler)
    engine = IncusEngine(socket_path="/fake/incus.socket")
    engine.client = httpx.AsyncClient(
        transport=transport,
        base_url="http://localhost/1.0/",
    )
    engine.network.client = engine.client

    return engine, recorded_requests


@pytest.mark.asyncio
async def test_engine_create_and_clone(mock_incus):
    engine, reqs = mock_incus

    spec = ContainerSpec(
        name="target-app",
        image="images:debian/12",
        ip_suffix=2,
        limits_cpu="2",
        limits_memory="512MB",
    )
    name = await engine.create_instance(spec)
    assert name == "target-app"

    # Verify create POST
    post_req = next(r for r in reqs if r.method == "POST" and r.url.path == "/1.0/instances")
    body = json.loads(post_req.content.decode())
    assert body["name"] == "target-app"
    assert body["source"]["alias"] == "images:debian/12"
    assert body["config"]["limits.cpu"] == "2"

    # Clone
    clone_name = await engine.clone_instance("target-app", "target-app-clone")
    assert clone_name == "target-app-clone"


@pytest.mark.asyncio
async def test_engine_start_stop_idempotency(mock_incus):
    engine, _ = mock_incus

    # Idempotent start on already-running container
    await engine.start_instance("already-running")

    # Idempotent stop on already-stopped container
    await engine.stop_instance("already-stopped")

    # Idempotent stop on 404 nonexistent container
    await engine.stop_instance("nonexistent")


@pytest.mark.asyncio
async def test_engine_delete_force_handling(mock_incus):
    engine, reqs = mock_incus

    # Delete with automatic stop handling if currently running
    await engine.delete_instance("running-inst")

    # Verify stop was called before delete
    state_reqs = [r for r in reqs if "running-inst/state" in r.url.path]
    assert len(state_reqs) > 0


@pytest.mark.asyncio
async def test_engine_network_connect_disconnect(mock_incus):
    engine, reqs = mock_incus

    await engine.connect_network(
        instance_name="target-app",
        network_name="verify-sdn-1",
        ip_address="10.1.1.2",
    )

    patch_req = next(r for r in reqs if r.method == "PATCH")
    body = json.loads(patch_req.content.decode())
    assert body["devices"]["eth0"]["network"] == "verify-sdn-1"
    assert body["devices"]["eth0"]["ipv4.address"] == "10.1.1.2"
    # Ensure bridge parent was explicitly unlinked
    assert body["devices"]["eth0"]["parent"] == ""

    await engine.disconnect_network("target-app", "verify-sdn-1")


@pytest.mark.asyncio
async def test_engine_exec_command_output_capture(mock_incus):
    engine, _ = mock_incus

    res = await engine.exec_command(
        name="target-app",
        cmd=["echo", "hello"],
        timeout=10,
    )
    assert res.exit_code == 0
    assert "command output success" in res.stdout
    assert "warning message" in res.stderr


@pytest.mark.asyncio
async def test_engine_list_and_exists(mock_incus):
    engine, _ = mock_incus

    all_instances = await engine.list_instances()
    assert len(all_instances) == 4

    verify_instances = await engine.list_instances(prefix="verify-")
    assert len(verify_instances) == 2
    assert "verify-web-prod-1-uuid" in verify_instances

    assert await engine.instance_exists("target-app") is True
    assert await engine.instance_exists("nonexistent") is False


@pytest.mark.asyncio
async def test_engine_operation_failure(mock_incus):
    engine, _ = mock_incus

    with pytest.raises(RuntimeError) as exc_info:
        await engine._wait_incus_op("fail-op")
    assert "pool full" in str(exc_info.value)


@pytest.mark.asyncio
async def test_ovn_network_management_full(mock_incus):
    engine, reqs = mock_incus

    # 1. Create network
    net = await engine.create_network(
        name="verify-net-99",
        config={"ipv4.address": "10.99.1.1/24", "ipv4.nat": "false"},
    )
    assert net is not None

    # 2. Idempotent create network if already exists
    existing_net = await engine.network.create_ovn_network(
        name="existing-net",
        subnet_cidr="10.100.1.1/24",
        exist_ok=True,
    )
    assert existing_net["name"] == "existing-net"

    # 3. List verification networks
    v_nets = await engine.network.list_verification_networks()
    assert v_nets == ["verify-test-1"]

    # 4. List recursive full networks
    full_nets = await engine.network.list_networks(full=True)
    assert len(full_nets) == 2

    # 5. Get leases
    leases = await engine.network.get_network_leases("verify-net-99")
    assert len(leases) == 1
    assert leases[0]["address"] == "10.1.1.2"

    # 6. Delete in-use network should raise informative RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
        await engine.network.delete_ovn_network("in-use-net")
    assert "still in use" in str(exc_info.value)

    # 7. Delete network successfully
    await engine.delete_network("verify-net-99")


@pytest.mark.asyncio
async def test_create_verify_network_at_runtime_for_team(mock_incus):
    """Verifies runtime provisioning of an isolated OVN verification network for team challenge verification."""
    engine, reqs = mock_incus
    team_k = 3
    session_id = "test-session-uuid-1234"
    verify_net_name = f"verify-{session_id}"

    # Addressing: matches team k's bridge CIDR for topological fidelity
    plan = plan_for(team_k)
    verify_net = await engine.create_network(
        name=verify_net_name,
        config={
            "ipv4.address": plan.bridge_cidr,
            "ipv4.nat": "false",
        },
    )

    assert verify_net is not None
    assert await engine.network.network_exists(verify_net_name) is True

    # List verification networks
    v_nets = await engine.network.list_verification_networks()
    assert any(n.startswith("verify-") for n in v_nets)

    # Teardown at runtime
    await engine.delete_network(verify_net_name)


@pytest.mark.asyncio
async def test_engine_image_and_file_operations(mock_incus, tmp_path):
    """Verifies file push, image import, alias management, and deletion on IncusEngine."""
    engine, reqs = mock_incus

    # 1. create_instance with devices and extra_config
    spec = ContainerSpec(
        name="custom-box",
        image="existing-alias",
        ip_suffix=10,
        limits_cpu="2",
        limits_memory="512MB",
    )
    devices = {"eth0": {"type": "nic", "nictype": "bridged", "parent": "br-team-1"}}
    extra_config = {"user.blueagent.test": "true"}

    name = await engine.create_instance(spec, devices=devices, extra_config=extra_config)
    assert name == "custom-box"

    # 2. push_file
    await engine.push_file("custom-box", "/etc/hostname", "custom-box\n", mode="0644")

    # 3. image_alias_exists
    assert await engine.image_alias_exists("existing-alias") is True
    assert await engine.image_alias_exists("non-existent-alias") is False

    # 4. import_image
    fake_tarball = tmp_path / "dummy_image.tar.xz"
    fake_tarball.write_bytes(b"dummy tarball content")
    fp = await engine.import_image(str(fake_tarball))
    assert fp == "fp-imported-12345"

    # 5. create_image_alias, delete_image_alias, delete_image
    await engine.create_image_alias("my-alias", fp)
    await engine.delete_image_alias("my-alias")
    await engine.delete_image(fp)
