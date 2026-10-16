# Container Engine, Concurrency & Asynchronous Management

## 1. Abstract Container Engine Layer (OOP Contract)

To decouple orchestration logic from specific runtimes (system containers vs. application containers), BlueAgent defines the `ContainerEngine` ABC:

```python
from abc import ABC, abstractmethod
from typing import Optional, Dict, List
from pydantic import BaseModel

class ExecResult(BaseModel):
    exit_code: int
    stdout: str
    stderr: str

class ContainerEngine(ABC):
    @abstractmethod
    async def create_instance(self, spec: Any) -> str: ...
    @abstractmethod
    async def start_instance(self, name: str) -> None: ...
    @abstractmethod
    async def stop_instance(self, name: str, force: bool = False) -> None: ...
    @abstractmethod
    async def delete_instance(self, name: str) -> None: ...
    @abstractmethod
    async def clone_instance(self, source: str, target: str) -> str: ...
    @abstractmethod
    async def connect_network(self, instance: str, network: str, ip: Optional[str] = None) -> None: ...
    @abstractmethod
    async def disconnect_network(self, instance: str, network: str) -> None: ...
    @abstractmethod
    async def exec_command(self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 15) -> ExecResult: ...
    @abstractmethod
    async def get_instance_status(self, name: str) -> str: ...
```

### Runtime Adaptation: Incus vs. Docker

| Operation | Incus Implementation (UDS) | Docker Implementation |
| :--- | :--- | :--- |
| **Cloning** | `POST /1.0/instances` (`source.type="copy"`) on ZFS CoW | `docker commit` $\rightarrow$ `docker run` or volume clone |
| **Network Connect**| `PATCH /1.0/instances/{name}` updating `devices.eth0` | `POST /v1.43/networks/{id}/connect` |
| **Network Disconnect**| Set `devices.eth0.type = "none"` or remove device | `POST /v1.43/networks/{id}/disconnect` |
| **Command Execution**| `POST /1.0/instances/{name}/exec` (UDS raw/websocket) | `POST /v1.43/containers/{name}/exec` |

---

## 2. Concurrency Control & State Reconciliation

### Preventing `HTTP 409 Conflict`
Concurrent mutations against the same container (simultaneous start, patch, or clone requests) cause `HTTP 409 Conflict`.
- **Single-Worker ASGI:** Coroutines synchronize via an `asyncio.Lock` per instance or per team.
- **Multi-Worker ASGI (Gunicorn / Uvicorn workers):** Cross-process synchronization is enforced using file-based locks (`fcntl.flock`) on `/tmp/blueagent-locks/<team_id>.lock` or Redis distributed locks.

### LifecycleManager & 600-Second Grace Period
A background daemon scans for orphaned `verify-*` instances and leftover OVN networks:
- **Grace Period (600s):** Freshly created OVN networks temporarily have zero connected ports before container cloning finishes. Networks and instances younger than `resource_grace_period_seconds` (default 600 seconds, evaluated every 120 seconds) are exempted from cleanup to prevent pruning active verification sessions.

---

## 3. Concrete IncusEngine Implementation

```python
import httpx
from typing import Optional, Dict, List, Any

class IncusEngine(ContainerEngine):
    """Incus client communicating over Unix Domain Socket."""

    def __init__(self, socket_path: str = "/var/lib/incus/unix.socket"):
        self.client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path),
            base_url="http://localhost/1.0"
        )

    async def _wait_op(self, op_uuid: str, timeout: int = 60) -> Dict[str, Any]:
        clean_uuid = op_uuid.split("/")[-1]
        res = await self.client.get(f"operations/{clean_uuid}/wait", params={"timeout": timeout})
        meta = res.json().get("metadata", {})
        if meta.get("status") != "Success":
            raise RuntimeError(f"Incus op {clean_uuid} failed: {meta.get('err')}")
        return meta

    async def create_instance(self, spec: Any) -> str:
        payload = {
            "name": spec.name,
            "source": {"type": "image", "alias": spec.image},
            "config": {"limits.cpu": spec.limits_cpu, "limits.memory": spec.limits_memory}
        }
        res = await self.client.post("instances", json=payload)
        if op := res.json().get("operation"):
            await self._wait_op(op)
        return spec.name

    async def start_instance(self, name: str) -> None:
        res = await self.client.put(f"instances/{name}/state", json={"action": "start"})
        if op := res.json().get("operation"):
            await self._wait_op(op)

    async def stop_instance(self, name: str, force: bool = False) -> None:
        res = await self.client.put(f"instances/{name}/state", json={"action": "stop", "force": force})
        if op := res.json().get("operation"):
            await self._wait_op(op)

    async def delete_instance(self, name: str) -> None:
        res = await self.client.delete(f"instances/{name}")
        if op := res.json().get("operation"):
            await self._wait_op(op)

    async def clone_instance(self, source: str, target: str) -> str:
        payload = {"name": target, "source": {"type": "copy", "source": source, "instance_only": True}}
        res = await self.client.post("instances", json=payload)
        if op := res.json().get("operation"):
            await self._wait_op(op)
        return target

    async def connect_network(self, instance: str, network: str, ip: Optional[str] = None) -> None:
        patch = {"devices": {"eth0": {"type": "nic", "network": network}}}
        if ip:
            patch["devices"]["eth0"]["ipv4.address"] = ip
        await self.client.patch(f"instances/{instance}", json=patch)

    async def disconnect_network(self, instance: str, network: str) -> None:
        await self.client.patch(f"instances/{instance}", json={"devices": {"eth0": {"type": "none"}}})

    async def exec_command(self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None, timeout: int = 15) -> ExecResult:
        payload = {"command": cmd, "environment": env or {}, "wait-for-websocket": False, "record-output": True}
        res = await self.client.post(f"instances/{name}/exec", json=payload)
        op = res.json().get("operation")
        meta = await self._wait_op(op, timeout=timeout)
        exit_code = meta.get("metadata", {}).get("return", 0)
        return ExecResult(exit_code=exit_code, stdout="", stderr="")

    async def get_instance_status(self, name: str) -> str:
        res = await self.client.get(f"instances/{name}/state")
        return res.json().get("metadata", {}).get("status", "UNKNOWN")
```

---

## 4. OVNVerifier Context Manager (Dual-Mode & Dark L2 Switch)

Encapsulates ephemeral OVN network creation (isolated dark L2 switch with max 11-character name limit), CoW cloning, in-namespace IP configuration, execution of in-container and verifier runner checks, timeout handling, and reverse LIFO cleanup:

```python
import uuid
from typing import Optional, List
from .models import ChallengeConfig, VerifyResultResp
from .engine.base import ContainerEngine

class OVNVerifier:
    def __init__(self, engine: ContainerEngine, config: ChallengeConfig, session_id: Optional[str] = None):
        self.engine = engine
        self.config = config
        self.session_id = session_id or uuid.uuid4().hex[:4]
        # Incus caps OVN network names to 11 characters
        self.net_name = f"verify-{self.session_id[:4]}"
        self.clones: List[str] = []
        self.verifier_container: Optional[str] = None
        self._network_created = False

    async def __aenter__(self):
        # 1. Ephemeral Dark OVN L2 network (isolated, ipv4.address: none, no uplink)
        # Prevents host route conflicts with live br-team-k (10.k.1.1/24)
        await self.engine.create_network(
            self.net_name,
            config={"ipv4.address": "none"}
        )
        self._network_created = True

        # 2. Duplicate live containers via CoW snapshot (~100 ms) and rebind eth0
        for c in self.config.containers:
            target = f"verify-{c.name}-{self.session_id[:4]}"
            await self.engine.clone_instance(c.name, target)
            self.clones.append(target)
            # Rebind eth0 to dark OVN network (without ipv4.address to avoid DHCP error)
            await self.engine.connect_network(target, self.net_name)
            await self.engine.start_instance(target)

            # Assign static IP inside container namespace directly
            target_ip = f"10.{self.config.team_k}.1.{c.ip_suffix}"
            await self.engine.exec_command(
                target,
                ["ip", "addr", "replace", f"{target_ip}/24", "dev", "eth0"],
                timeout=5
            )
            await self.engine.exec_command(
                target,
                ["ip", "link", "set", "eth0", "up"],
                timeout=5
            )

        # 3. Optional: Spawn ephemeral Verifier Runner Container for black-box checks
        if self.config.verifier:
            self.verifier_container = f"verify-runner-{self.session_id[:4]}"
            await self.engine.create_instance(
                ContainerSpec(
                    name=self.verifier_container,
                    image=self.config.verifier.image,
                    ip_suffix=self.config.verifier.ip_suffix,
                    limits_cpu=self.config.verifier.limits_cpu,
                    limits_memory=self.config.verifier.limits_memory,
                )
            )
            await self.engine.connect_network(self.verifier_container, self.net_name)
            await self.engine.start_instance(self.verifier_container)

            verifier_ip = f"10.{self.config.team_k}.1.{self.config.verifier.ip_suffix}"
            await self.engine.exec_command(
                self.verifier_container,
                ["ip", "addr", "replace", f"{verifier_ip}/24", "dev", "eth0"],
                timeout=5
            )
            await self.engine.exec_command(
                self.verifier_container,
                ["ip", "link", "set", "eth0", "up"],
                timeout=5
            )

        return self

    async def run_checks(self) -> VerifyResultResp:
        for check in self.config.verify_commands:
            target = self.verifier_container if check.target == "verifier" and self.verifier_container else f"verify-{check.container_name}-{self.session_id[:4]}"
            try:
                res = await self.engine.exec_command(target, check.cmd, check.env, check.timeout)
            except Exception as e:
                err_msg = str(e).lower()
                if "deadline exceeded" in err_msg or "timed out" in err_msg:
                    return VerifyResultResp(
                        success=False,
                        passed=False,
                        message=f"Command timed out: {' '.join(check.cmd)}",
                        details={"error": str(e)},
                    )
                raise

            if res.exit_code != 0:
                return VerifyResultResp(
                    success=True,
                    passed=False,
                    message=f"Check failed: {' '.join(check.cmd)}",
                    details={"exit_code": res.exit_code, "stdout": res.stdout, "stderr": res.stderr}
                )
        return VerifyResultResp(success=True, passed=True, message="All verification checks passed.")

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # Reverse LIFO cleanup:
        # 1. Verifier container
        if self.verifier_container:
            try:
                await self.engine.stop_instance(self.verifier_container, force=True)
                await self.engine.delete_instance(self.verifier_container)
            except Exception: pass

        # 2. Clones in reverse order
        for clone in reversed(self.clones):
            try:
                await self.engine.stop_instance(clone, force=True)
                await self.engine.delete_instance(clone)
            except Exception: pass

        # 3. OVN network after all attached ports are released
        if self._network_created:
            try:
                await self.engine.delete_network(self.net_name)
            except Exception: pass
```
