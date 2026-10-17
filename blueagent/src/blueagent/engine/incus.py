"""Incus (LXC) implementation of the ContainerEngine interface.

Communicates with the local Incus daemon over the Unix Domain Socket
(/var/lib/incus/unix.socket) via asynchronous HTTP/1.1 calls, utilizing
server-side long polling on /operations/{uuid}/wait to avoid client spin-polling.
"""

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional
import httpx

from ..config import settings
from ..models import ContainerSpec, ExecResult
from .base import ContainerEngine
from .network import OvnNetworkManager

logger = logging.getLogger(__name__)


class IncusEngine(ContainerEngine):
    """Concrete implementation of ContainerEngine utilizing Incus REST API."""

    def __init__(
        self,
        socket_path: str = settings.incus_socket_path,
        base_url: str = settings.incus_base_url,
    ):
        self.socket_path = socket_path
        # Ensure trailing slash so relative paths resolve against /1.0/
        clean_base = base_url.rstrip("/") + "/"
        self.transport = httpx.AsyncHTTPTransport(uds=socket_path)
        self.client = httpx.AsyncClient(
            transport=self.transport,
            base_url=clean_base,
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=60.0),
        )
        self.network = OvnNetworkManager(self.client, self._wait_incus_op)

    async def aclose(self) -> None:
        """Closes the underlying HTTP client and Unix socket transport."""
        await self.client.aclose()

    async def _wait_incus_op(
        self, operation_uuid: str, timeout_seconds: Optional[int] = None
    ) -> Dict[str, Any]:
        """Waits for an asynchronous Incus background operation to reach terminal status.

        Args:
            operation_uuid: Operation path or UUID (e.g. '/1.0/operations/<uuid>' or '<uuid>').
            timeout_seconds: Duration before aborting. Defaults to settings.incus_operation_timeout.

        Returns:
            The terminal operation metadata dictionary.

        Raises:
            RuntimeError: If operation failed or was cancelled.
            TimeoutError: If the operation timed out.
        """
        clean_uuid = operation_uuid.split("/")[-1]
        endpoint = f"operations/{clean_uuid}/wait"
        max_duration = float(timeout_seconds if timeout_seconds is not None else settings.incus_operation_timeout)

        start_time = time.perf_counter()
        while True:
            elapsed = time.perf_counter() - start_time
            if elapsed >= max_duration:
                raise TimeoutError(f"Incus operation {clean_uuid} timed out after {max_duration:.1f}s")

            remaining = int(max(max_duration - elapsed, 1.0))
            chunk_wait = min(remaining, 60)
            http_read_timeout = max(float(chunk_wait) + 10.0, 15.0)

            try:
                res = await self.client.get(
                    endpoint,
                    params={"timeout": chunk_wait},
                    timeout=http_read_timeout,
                )
                if res.status_code == 200:
                    body = res.json()
                    if body.get("type") == "error" or body.get("error_code", 0) != 0:
                        err_msg = body.get("error") or "Incus operation error"
                        if "deadline exceeded" in err_msg.lower() or "context deadline" in err_msg.lower():
                            await asyncio.sleep(0.5)
                            continue
                        raise RuntimeError(f"Incus operation {clean_uuid} failed: {err_msg}")
                    meta = body.get("metadata") or {}
                    status = meta.get("status") or body.get("status")
                    if status == "Success":
                        return meta if meta else {"status": "Success"}
                    elif status in ("Failure", "Cancelled"):
                        err_msg = meta.get("err") or meta.get("metadata", {}).get("err") or body.get("error") or "Unknown error"
                        raise RuntimeError(f"Incus operation {clean_uuid} failed ({status}): {err_msg}")
                    elif status == "Running":
                        await asyncio.sleep(0.5)
                        continue
                elif res.status_code == 404:
                    # Operation completed and was reaped
                    return {"status": "Success"}

                raise RuntimeError(
                    f"Unexpected Incus response for operation {clean_uuid}: HTTP {res.status_code} - {res.text}"
                )
            except httpx.TimeoutException:
                continue

    async def create_instance(
        self,
        spec: ContainerSpec,
        devices: Optional[Dict[str, Any]] = None,
        extra_config: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Provisions a new system container instance from spec."""
        config = {
            "limits.cpu": spec.limits_cpu,
            "limits.memory": spec.limits_memory,
        }
        if extra_config:
            config.update(extra_config)

        payload: Dict[str, Any] = {
            "name": spec.name,
            "source": {
                "type": "image",
                "alias": spec.image,
            },
            "config": config,
        }
        if devices:
            payload["devices"] = devices

        res = await self.client.post("instances", json=payload)
        res.raise_for_status()

        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)
        return spec.name

    async def _ensure_parent_dirs(self, name: str, file_path: str) -> None:
        """Ensures all parent directories exist before writing a file inside a container."""
        from pathlib import PurePosixPath
        parent = PurePosixPath(file_path).parent
        parts = parent.parts
        current = ""
        for part in parts:
            if part == "/":
                continue
            current = f"{current}/{part}"
            headers = {
                "X-Incus-type": "directory",
                "X-Incus-mode": "0755",
            }
            try:
                res = await self.client.post(
                    f"instances/{name}/files",
                    params={"path": current},
                    headers=headers,
                )
            except Exception:
                pass

    async def push_file(
        self,
        name: str,
        path: str,
        content: str | bytes,
        mode: str = "0644",
        uid: int = 0,
        gid: int = 0,
    ) -> None:
        """Uploads a file directly into a container filesystem."""
        data = content.encode("utf-8") if isinstance(content, str) else content
        headers = {
            "X-Incus-mode": mode,
            "X-Incus-uid": str(uid),
            "X-Incus-gid": str(gid),
            "X-Incus-type": "file",
            "Content-Type": "application/octet-stream",
        }
        res = await self.client.post(
            f"instances/{name}/files",
            params={"path": path},
            content=data,
            headers=headers,
        )
        if res.status_code == 404:
            # Parent directory missing in container; recursively create parent dirs
            await self._ensure_parent_dirs(name, path)
            res = await self.client.post(
                f"instances/{name}/files",
                params={"path": path},
                content=data,
                headers=headers,
            )
        res.raise_for_status()

    async def image_alias_exists(self, alias: str) -> bool:
        """Checks if an image alias exists in Incus."""
        res = await self.client.get(f"images/aliases/{alias}")
        if res.status_code == 200:
            return True
        if res.status_code == 404:
            return False
        res.raise_for_status()
        return False

    async def import_image(self, tarball_path: str) -> str:
        """Uploads a unified container image tarball using streaming to minimize RAM and returns its fingerprint."""
        async def file_stream():
            with open(tarball_path, "rb") as f:
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        headers = {
            "Content-Type": "application/octet-stream",
            "X-Incus-public": "false",
        }
        res = await self.client.post("images", content=file_stream(), headers=headers, timeout=300.0)
        res.raise_for_status()

        body = res.json()
        op = body.get("operation")
        if op:
            meta = await self._wait_incus_op(op, timeout_seconds=300)
            fp = meta.get("metadata", {}).get("fingerprint") or meta.get("fingerprint")
            if fp:
                return fp

        # Fallback to direct response metadata
        fp = body.get("metadata", {}).get("fingerprint")
        if fp:
            return fp
        raise RuntimeError("Incus image import did not return a fingerprint")

    async def create_image_alias(self, alias: str, fingerprint: str) -> None:
        """Creates or updates an alias pointing to an image fingerprint."""
        payload = {"name": alias, "target": fingerprint}
        res = await self.client.post("images/aliases", json=payload)
        if res.status_code in (400, 409) and "already exists" in res.text.lower():
            # Already exists, verify target or update
            check = await self.client.get(f"images/aliases/{alias}")
            if check.status_code == 200:
                target = check.json().get("metadata", {}).get("target")
                if target == fingerprint:
                    return
            # Update target on existing alias
            put_res = await self.client.put(f"images/aliases/{alias}", json={"target": fingerprint})
            if put_res.status_code in (200, 201):
                return
            await self.client.delete(f"images/aliases/{alias}")
            res = await self.client.post("images/aliases", json=payload)
        res.raise_for_status()

    async def delete_image_alias(self, alias: str) -> None:
        """Deletes an image alias."""
        res = await self.client.delete(f"images/aliases/{alias}")
        if res.status_code == 404:
            return
        res.raise_for_status()

    async def delete_image(self, fingerprint: str) -> None:
        """Deletes an image by fingerprint."""
        res = await self.client.delete(f"images/{fingerprint}")
        if res.status_code == 404:
            return
        res.raise_for_status()
        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)

    async def start_instance(self, name: str) -> None:
        """Powers on an existing container instance."""
        res = await self.client.put(f"instances/{name}/state", json={"action": "start"})
        # 400 or 409 if already running; verify idempotency
        if res.status_code in (400, 409) and any(
            msg in res.text.lower() for msg in ["already running", "already started", "is running"]
        ):
            return

        res.raise_for_status()
        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)

    async def stop_instance(self, name: str, force: bool = False) -> None:
        """Stops an active container instance."""
        res = await self.client.put(
            f"instances/{name}/state",
            json={"action": "stop", "force": force},
        )
        if res.status_code == 404:
            return  # Already deleted / non-existent

        # 400 or 409 if already stopped
        if res.status_code in (400, 409) and any(
            msg in res.text.lower() for msg in ["already stopped", "not running", "is stopped"]
        ):
            return

        res.raise_for_status()
        op = res.json().get("operation")
        if op:
            try:
                await self._wait_incus_op(op)
            except RuntimeError as err:
                if any(msg in str(err).lower() for msg in ["already stopped", "not running", "is stopped"]):
                    return
                raise

    async def delete_instance(self, name: str, force: bool = False) -> None:
        """Deletes an instance and releases all associated storage volumes."""
        if force:
            try:
                await self.stop_instance(name, force=True)
            except Exception:
                pass

        res = await self.client.delete(f"instances/{name}")
        if res.status_code == 404:
            return  # Idempotent delete

        if res.status_code in (400, 409) and "is running" in res.text.lower() and not force:
            # Attempt automatic forced stop then retry delete
            await self.stop_instance(name, force=True)
            retry_res = await self.client.delete(f"instances/{name}")
            if retry_res.status_code == 404:
                return
            retry_res.raise_for_status()
            op = retry_res.json().get("operation")
            if op:
                await self._wait_incus_op(op)
            return

        res.raise_for_status()
        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)

    async def clone_instance(self, source: str, target: str) -> str:
        """Creates a fast Copy-on-Write (CoW) clone of the source container instance."""
        payload = {
            "name": target,
            "source": {
                "type": "copy",
                "source": source,
                "instance_only": True,
            },
        }
        res = await self.client.post("instances", json=payload)
        res.raise_for_status()

        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)
        return target

    async def connect_network(
        self,
        instance_name: str,
        network_name: str,
        ip_address: Optional[str] = None,
    ) -> None:
        """Attaches the instance's eth0 NIC to an OVN or bridge network."""
        eth0_config: Dict[str, str] = {
            "type": "nic",
            "network": network_name,
            "parent": "",  # Clear prior host bridge binding when transitioning to OVN
        }
        if ip_address:
            eth0_config["ipv4.address"] = ip_address

        device_patch = {
            "devices": {
                "eth0": eth0_config,
            }
        }
        res = await self.client.patch(f"instances/{instance_name}", json=device_patch)
        if res.status_code >= 400:
            raise RuntimeError(f"Failed to connect instance '{instance_name}' to network '{network_name}' ({res.status_code}): {res.text}")

        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)

    async def disconnect_network(self, instance_name: str, network_name: str) -> None:
        """Detaches the instance from a network by setting eth0 to type none."""
        device_patch = {
            "devices": {
                "eth0": {
                    "type": "none",
                }
            }
        }
        res = await self.client.patch(f"instances/{instance_name}", json=device_patch)
        res.raise_for_status()

        op = res.json().get("operation")
        if op:
            await self._wait_incus_op(op)

    async def exec_command(
        self,
        name: str,
        cmd: List[str],
        env: Optional[Dict[str, str]] = None,
        timeout: int = 15,
        capture_output: bool = True,
    ) -> ExecResult:
        """Executes a command vector in the specified container instance."""
        payload = {
            "command": cmd,
            "environment": env or {},
            "wait-for-websocket": False,
            "record-output": capture_output,
        }
        res = await self.client.post(f"instances/{name}/exec", json=payload)
        res.raise_for_status()

        body = res.json()
        op = body.get("operation")
        if not op:
            raise RuntimeError("Incus exec did not return an operation handle")

        meta = await self._wait_incus_op(op, timeout_seconds=timeout)
        output_meta = meta.get("metadata", {})
        exit_code = output_meta.get("return", 0)

        stdout = ""
        stderr = ""
        if capture_output:
            output_files = output_meta.get("output", {})
            # Map both str and int descriptors: 1 = stdout, 2 = stderr
            stdout_url = output_files.get("1") or output_files.get(1)
            stderr_url = output_files.get("2") or output_files.get(2)

            if stdout_url:
                log_path = str(stdout_url).lstrip("/")
                rel_path = log_path.removeprefix("1.0/")
                try:
                    log_res = await self.client.get(rel_path)
                    if log_res.status_code == 200:
                        stdout = log_res.text
                except Exception:
                    pass
                finally:
                    try:
                        await self.client.delete(rel_path)
                    except Exception:
                        pass

            if stderr_url:
                log_path = str(stderr_url).lstrip("/")
                rel_path = log_path.removeprefix("1.0/")
                try:
                    log_res = await self.client.get(rel_path)
                    if log_res.status_code == 200:
                        stderr = log_res.text
                except Exception:
                    pass
                finally:
                    try:
                        await self.client.delete(rel_path)
                    except Exception:
                        pass

        return ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )

    async def get_instance_status(self, name: str) -> str:
        """Returns the current state of the instance (e.g. 'RUNNING', 'STOPPED')."""
        res = await self.client.get(f"instances/{name}/state")
        if res.status_code == 404:
            return "NON_EXISTENT"
        res.raise_for_status()
        return res.json().get("metadata", {}).get("status", "UNKNOWN")

    async def get_instance_metadata(self, name: str) -> Optional[Dict[str, Any]]:
        """Retrieves full instance details and configuration metadata."""
        res = await self.client.get(f"instances/{name}")
        if res.status_code == 404:
            return None
        res.raise_for_status()
        return res.json().get("metadata", {})

    async def instance_exists(self, name: str) -> bool:
        """Checks if an instance exists."""
        meta = await self.get_instance_metadata(name)
        return meta is not None

    async def list_instances(self, prefix: Optional[str] = None) -> List[str]:
        """Lists instance names, optionally filtered by a prefix (e.g. 'verify-')."""
        res = await self.client.get("instances")
        res.raise_for_status()
        paths: List[str] = res.json().get("metadata", [])
        names = [p.split("/")[-1] for p in paths]
        if prefix:
            return [n for n in names if n.startswith(prefix)]
        return names

    async def create_network(self, name: str, config: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Provisions an isolated OVN network delegating to OvnNetworkManager."""
        subnet = (config or {}).get("ipv4.address", "10.0.1.1/24")
        enable_nat = (config or {}).get("ipv4.nat", "false").lower() == "true"
        uplink = (config or {}).get("network")
        return await self.network.create_ovn_network(
            name=name,
            subnet_cidr=subnet,
            uplink_network=uplink,
            enable_nat=enable_nat,
            extra_config=config,
            exist_ok=True,
        )

    async def delete_network(self, name: str) -> None:
        """Deletes an isolated OVN network delegating to OvnNetworkManager."""
        await self.network.delete_ovn_network(name)

    async def list_networks(self, prefix: Optional[str] = None) -> List[str]:
        """Lists network names, optionally filtered by a prefix (e.g. 'verify-')."""
        paths = await self.network.list_networks(full=False)
        names = [str(p).split("/")[-1] for p in paths]
        if prefix:
            return [n for n in names if n.startswith(prefix)]
        return names
