"""Async HTTP client helper for BlueAgent usability testing."""

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TOKEN = "secret-token"


class BlueAgentTestClient:
    """Async wrapper over the BlueAgent REST API matching container-centric design."""

    def __init__(self, base_url: str = DEFAULT_BASE_URL, token: str = DEFAULT_TOKEN, timeout: float = 600.0):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout)

    async def wait_server_ready(self, max_seconds: float = 15.0) -> bool:
        """Polls /health until the server is responsive."""
        start = time.time()
        while time.time() - start < max_seconds:
            try:
                async with self._client() as c:
                    r = await c.get("/health")
                    if r.status_code == 200:
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError(f"BlueAgent server at {self.base_url} did not become ready within {max_seconds}s")

    # Team & Network endpoints
    async def create_team(self, team_k: int, name: str = "Test Team", ctfd_team_id: Optional[int] = None) -> httpx.Response:
        payload = {
            "name": name,
        }
        async with self._client() as c:
            return await c.put(f"/teams/{team_k}", headers=self.headers, json=payload)

    async def get_team(self, team_k: int) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/teams/{team_k}", headers=self.headers)

    async def list_teams(self) -> httpx.Response:
        async with self._client() as c:
            return await c.get("/teams", headers=self.headers)

    async def delete_team(self, team_k: int) -> httpx.Response:
        async with self._client() as c:
            return await c.delete(f"/teams/{team_k}", headers=self.headers)

    async def add_member(
        self,
        team_k: int,
        ctfd_user_id: int,
        name: str = "Test User",
        ctfd_team_id: Optional[int] = None,
        public_key: Optional[str] = None,
    ) -> httpx.Response:
        payload = {
            "name": name,
        }
        if public_key is not None:
            payload["public_key"] = public_key
        async with self._client() as c:
            return await c.put(f"/teams/{team_k}/members/{ctfd_user_id}", headers=self.headers, json=payload)

    async def get_member(self, team_k: int, member_id: Any) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/teams/{team_k}/members/{member_id}", headers=self.headers)

    async def get_member_config(self, team_k: int, member_id: str) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/teams/{team_k}/members/{member_id}/config", headers=self.headers)

    async def delete_member(self, team_k: int, member_id: Any) -> httpx.Response:
        async with self._client() as c:
            return await c.delete(f"/teams/{team_k}/members/{member_id}", headers=self.headers)

    async def get_peers(self, team_k: int) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/teams/{team_k}/members", headers=self.headers)

    # Images endpoints
    async def create_image(self, alias: str, yaml_path: Path, force: bool = False) -> httpx.Response:
        async with self._client() as c:
            with open(yaml_path, "rb") as f:
                files = {"file": (yaml_path.name, f, "application/octet-stream")}
                params = {"force": "true"} if force else {}
                return await c.put(f"/images/{alias}", headers=self.headers, files=files, params=params)

    async def get_image(self, alias: str) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/images/{alias}", headers=self.headers)

    async def get_image_logs(self, alias: str) -> httpx.Response:
        async with self._client() as c:
            return await c.get(f"/images/{alias}/logs", headers=self.headers)

    async def list_images(self) -> httpx.Response:
        async with self._client() as c:
            return await c.get("/images", headers=self.headers)

    async def delete_image(self, alias: str) -> httpx.Response:
        async with self._client() as c:
            return await c.delete(f"/images/{alias}", headers=self.headers)

    async def wait_for_image_ready(self, alias: str, timeout: float = 300.0, poll_interval: float = 3.0) -> Dict[str, Any]:
        start = time.time()
        while time.time() - start < timeout:
            r = await self.get_image(alias)
            if r.status_code == 200:
                data = r.json()
                status = data.get("status")
                if status == "ready":
                    return data
                if status == "failed":
                    raise RuntimeError(f"Image build failed: {data.get('error')}")
            await asyncio.sleep(poll_interval)
        raise TimeoutError(f"Image '{alias}' did not reach 'ready' status within {timeout}s")

    # Container / Machines endpoints (Stateless container management)
    async def deploy_challenge(self, payload: Dict[str, Any], team_k: Optional[int] = None) -> httpx.Response:
        """Deploys challenge containers to target team's bridge."""
        target_team_k = team_k if team_k is not None else payload.get("team_k")
        payload_copy = dict(payload)
        payload_copy.pop("team_k", None)
        url = f"/teams/{target_team_k}/machines"
        async with self._client() as c:
            return await c.post(url, headers=self.headers, json=payload_copy)

    async def get_active_containers(self, team_k: int) -> httpx.Response:
        """Retrieves running containers currently active for team k."""
        async with self._client() as c:
            return await c.get(f"/teams/{team_k}/machines", headers=self.headers)

    async def teardown_challenge(
        self,
        team_k: int,
        challenge_id: Optional[str] = "ch",
        payload: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        """Tears down challenge containers for team k."""
        url = f"/teams/{team_k}/machines"
        payload_copy = dict(payload) if payload else None
        if payload_copy:
            payload_copy.pop("team_k", None)
        async with self._client() as c:
            return await c.request("DELETE", url, headers=self.headers, json=payload_copy)

    async def verify_challenge(self, team_k: int, config_payload: Dict[str, Any], skip_cleanup: bool = False) -> httpx.Response:
        """Runs automated verification in dark OVN sandbox clones for team k."""
        payload_copy = dict(config_payload)
        payload_copy.pop("team_k", None)
        url = f"/teams/{team_k}/verificator"
        if skip_cleanup:
            url += "?skip_cleanup=true"
        async with self._client() as c:
            return await c.post(url, headers=self.headers, json=payload_copy)

    async def cleanup_verify(self, team_k: int, config_payload: Optional[Dict[str, Any]] = None) -> httpx.Response:
        """Cleans up ephemeral verify sandbox clones and OVN network for team k."""
        payload_copy = dict(config_payload) if config_payload else None
        if payload_copy:
            payload_copy.pop("team_k", None)
        async with self._client() as c:
            return await c.request("DELETE", f"/teams/{team_k}/verificator", headers=self.headers, json=payload_copy)

