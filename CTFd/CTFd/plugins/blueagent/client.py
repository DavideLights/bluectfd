"""Production REST API client for BlueAgent v2 orchestrator.

Implements full communication contract for all 20 BlueAgent endpoints:
- Team & WireGuard tunnel management
- Member provisioning & VPN configuration delivery
- Container deployment on team bridge (Incus)
- Dark OVN verification runs with ZFS CoW clones
- Distrobuilder image catalog management
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import settings

logger = logging.getLogger(__name__)


class BlueAgentError(Exception):
    """Base exception for BlueAgent communication errors."""

    def __init__(self, message: str, status_code: Optional[int] = None, detail: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or message


class BlueAgentUnavailable(BlueAgentError):
    """Raised when BlueAgent REST service is unreachable or connection times out."""


class BlueAgentAuthError(BlueAgentError):
    """Raised on HTTP 401 Unauthorized (invalid BLUEAGENT_TOKEN)."""


class TeamNotFound(BlueAgentError):
    """Raised on HTTP 404 Not Found for team resources."""


class BlueAgentConflict(BlueAgentError):
    """Raised on HTTP 409 Conflict (e.g. concurrent verification lock, bridge missing, member assigned)."""

    def __init__(self, message: str, detail: Optional[str] = None):
        super().__init__(message, status_code=409, detail=detail)
        # Classify conflict type for caller handling
        det = (detail or "").lower()
        self.is_verify_busy = "verification already in progress" in det or "locked" in det
        self.is_bridge_not_active = "bridge" in det and "not active" in det
        self.is_member_assigned = "already assigned" in det
        self.is_address_exhausted = "exhausted" in det or "subnet" in det
        self.is_image_in_use = "in use" in det
        self.is_image_state = "building" in det or "state" in det


class BlueAgentValidation(BlueAgentError):
    """Raised on HTTP 422 Unprocessable Entity (e.g. image not ready, bad parameters)."""


class DeploymentRolledBack(BlueAgentError):
    """Raised on HTTP 502 Bad Gateway when container deployment failed and was safely rolled back."""


class BlueAgentBusy(BlueAgentError):
    """Raised on HTTP 503 Service Unavailable when team lock timed out."""


class BlueAgentServerError(BlueAgentError):
    """Raised on HTTP 500 Internal Server Error."""


class BlueAgentClient:
    """Synchronous REST client for BlueAgent v2 with connection pooling and error mapping."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        connect_timeout: Optional[float] = None,
        verify_timeout: Optional[float] = None,
        deploy_timeout: Optional[float] = None,
    ):
        self.base_url = (base_url or settings.url).rstrip("/")
        self.token = token or settings.token
        self.connect_timeout = connect_timeout or settings.connect_timeout
        self.verify_timeout = verify_timeout or settings.verify_timeout
        self.deploy_timeout = deploy_timeout or settings.deploy_timeout

        self.session = requests.Session()
        retries = Retry(
            total=2,
            backoff_factor=0.3,
            status_forcelist=[503],
            allowed_methods=["GET", "HEAD", "OPTIONS"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retries, pool_connections=10, pool_maxsize=25)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _headers(self, is_json: bool = True) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-CTFd-Token": self.token,
        }
        if is_json:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Any] = None,
        files: Optional[Dict[str, Any]] = None,
        data: Optional[Any] = None,
        timeout: Optional[Tuple[float, float]] = None,
    ) -> requests.Response:
        url = f"{self.base_url}/{path.lstrip('/')}"
        is_json = files is None
        headers = self._headers(is_json=is_json)

        if timeout is None:
            timeout = (self.connect_timeout, 30.0)

        try:
            resp = self.session.request(
                method=method,
                url=url,
                params=params,
                json=json_data,
                files=files,
                data=data,
                headers=headers,
                timeout=timeout,
            )
        except requests.exceptions.ConnectionError as err:
            logger.error("BlueAgent connection error: %s", err)
            raise BlueAgentUnavailable(f"Cannot connect to BlueAgent orchestrator at {self.base_url}") from err
        except requests.exceptions.Timeout as err:
            logger.error("BlueAgent timeout on %s %s: %s", method, path, err)
            raise BlueAgentUnavailable(f"BlueAgent operation timed out ({method} {path})") from err
        except requests.exceptions.RequestException as err:
            logger.error("BlueAgent request error on %s %s: %s", method, path, err)
            raise BlueAgentError(f"BlueAgent request failed: {err}") from err

        return self._handle_response(resp)

    def _handle_response(self, resp: requests.Response) -> requests.Response:
        code = resp.status_code
        if 200 <= code < 300:
            return resp

        detail = ""
        try:
            body = resp.json()
            if isinstance(body, dict):
                detail = body.get("detail") or body.get("message") or str(body)
            else:
                detail = str(body)
        except Exception:
            detail = resp.text

        msg = f"BlueAgent {code}: {detail}"

        if code == 401:
            raise BlueAgentAuthError("Invalid BlueAgent API token", status_code=401, detail=detail)
        elif code == 404:
            raise TeamNotFound(f"Resource not found: {detail}", status_code=404, detail=detail)
        elif code == 409:
            raise BlueAgentConflict(f"Resource conflict: {detail}", detail=detail)
        elif code == 422:
            raise BlueAgentValidation(f"Validation error: {detail}", status_code=422, detail=detail)
        elif code == 502:
            raise DeploymentRolledBack(f"Deployment rolled back: {detail}", status_code=502, detail=detail)
        elif code == 503:
            raise BlueAgentBusy(f"Team lock busy or timeout: {detail}", status_code=503, detail=detail)
        elif code >= 500:
            raise BlueAgentServerError(f"Server error: {detail}", status_code=code, detail=detail)
        else:
            raise BlueAgentError(msg, status_code=code, detail=detail)

    # ==================== Health Endpoints ====================

    def health(self) -> Dict[str, Any]:
        """GET /health - Liveness probe."""
        resp = self._request("GET", "/health", timeout=(self.connect_timeout, 3.0))
        return resp.json()

    def health_ready(self) -> Dict[str, Any]:
        """GET /health/ready - Readiness probe checking Incus, OVN, and kernel."""
        resp = self._request("GET", "/health/ready", timeout=(self.connect_timeout, 5.0))
        return resp.json()

    # ==================== Team & Network Endpoints ====================

    def list_teams(self) -> List[Dict[str, Any]]:
        """GET /teams - Lists all provisioned teams."""
        resp = self._request("GET", "/teams")
        return resp.json()

    def get_team(self, team_k: int) -> Dict[str, Any]:
        """GET /teams/{team_k} - Retrieves team metadata and interfaces."""
        resp = self._request("GET", f"/teams/{team_k}")
        return resp.json()

    def create_team(self, team_k: int, name: str) -> Dict[str, Any]:
        """PUT /teams/{team_k} - Idempotently provisions team bridge and WireGuard tunnel."""
        payload = {"name": name}
        resp = self._request("PUT", f"/teams/{team_k}", json_data=payload, timeout=(self.connect_timeout, 30.0))
        return resp.json()

    def delete_team(self, team_k: int) -> None:
        """DELETE /teams/{team_k} - Tears down containers, bridge, WireGuard interface, and db records."""
        self._request("DELETE", f"/teams/{team_k}", timeout=(self.connect_timeout, 30.0))

    # ==================== Member & WireGuard Endpoints ====================

    def list_members(self, team_k: int) -> List[Dict[str, Any]]:
        """GET /teams/{team_k}/members - Lists all provisioned members for team with telemetry."""
        resp = self._request("GET", f"/teams/{team_k}/members")
        return resp.json()

    def add_member(
        self,
        team_k: int,
        name: str,
        user_id: Optional[Union[int, str]] = None,
        ctfd_user_id: Optional[int] = None,
        team_name: Optional[str] = None,
        public_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """PUT /teams/{team_k}/members/{user_id} - Idempotently allocates IP and registers WireGuard peer."""
        uid = user_id if user_id is not None else ctfd_user_id
        if uid is None:
            raise ValueError("user_id or ctfd_user_id is required")
        payload: Dict[str, Any] = {
            "name": name,
            "public_key": public_key,
        }
        resp = self._request(
            "PUT",
            f"/teams/{team_k}/members/{uid}",
            json_data=payload,
            timeout=(self.connect_timeout, 15.0),
        )
        return resp.json()

    def get_member(self, team_k: int, user_id: Union[int, str]) -> Dict[str, Any]:
        """GET /teams/{team_k}/members/{user_id} - Retrieves single team member record and telemetry."""
        resp = self._request("GET", f"/teams/{team_k}/members/{user_id}")
        return resp.json()

    def get_member_config(self, team_k: int, user_id: Union[int, str]) -> str:
        """GET /teams/{team_k}/members/{user_id}/config - Returns raw WireGuard .conf text."""
        resp = self._request(
            "GET",
            f"/teams/{team_k}/members/{user_id}/config",
            timeout=(self.connect_timeout, 10.0),
        )
        return resp.text

    def delete_member(self, team_k: int, user_id: Union[int, str]) -> None:
        """DELETE /teams/{team_k}/members/{user_id} - Removes peer from WireGuard and frees IP."""
        self._request("DELETE", f"/teams/{team_k}/members/{user_id}", timeout=(self.connect_timeout, 10.0))

    def get_peers(self, team_k: int) -> List[Dict[str, Any]]:
        """Live WireGuard peer telemetry for team_k extracted from /teams/{team_k}/members."""
        now_ts = int(time.time())
        try:
            members = self.list_members(team_k)
        except Exception as err:
            logger.warning("Could not list members for peer telemetry on team %d: %s", team_k, err)
            return []

        peers = []
        for m in members:
            t = m.get("telemetry") or {}
            latest_hs = t.get("latest_handshake", 0)
            handshake_age = max(0, now_ts - latest_hs) if latest_hs > 0 else 0
            peers.append({
                "member_id": m.get("member_id"),
                "ctfd_user_id": m.get("user_id"),
                "user_id": m.get("user_id"),
                "name": m.get("name"),
                "client_ip": m.get("client_ip"),
                "public_key": m.get("public_key"),
                "endpoint": t.get("endpoint"),
                "latest_handshake": handshake_age,
                "transfer_rx": t.get("transfer_rx", 0),
                "transfer_tx": t.get("transfer_tx", 0),
                "persistent_keepalive": t.get("persistent_keepalive", "off"),
                "online": t.get("online", False),
            })
        return peers

    # ==================== Container & Challenge Endpoints ====================

    def deploy_challenge(self, team_k: int, config: Dict[str, Any]) -> Dict[str, Any]:
        """POST /teams/{team_k}/machines - Deploys container topology to team bridge."""
        resp = self._request(
            "POST",
            f"/teams/{team_k}/machines",
            json_data=config,
            timeout=(self.connect_timeout, self.deploy_timeout),
        )
        return resp.json()

    def get_active_machines(self, team_k: int) -> List[Dict[str, Any]]:
        """GET /teams/{team_k}/machines - Lists running containers on team bridge."""
        resp = self._request("GET", f"/teams/{team_k}/machines")
        return resp.json()

    def teardown_machines(self, team_k: int, config: Optional[Dict[str, Any]] = None) -> None:
        """DELETE /teams/{team_k}/machines - Tears down containers for challenge or all team containers."""
        self._request(
            "DELETE",
            f"/teams/{team_k}/machines",
            json_data=config,
            timeout=(self.connect_timeout, 30.0),
        )

    # ==================== Verification Sandbox Endpoints ====================

    def verify_challenge(self, team_k: int, config: Dict[str, Any]) -> Dict[str, Any]:
        """POST /teams/{team_k}/verificator - Runs dark OVN verification checks against ZFS CoW clones."""
        resp = self._request(
            "POST",
            f"/teams/{team_k}/verificator",
            json_data=config,
            timeout=(self.connect_timeout, self.verify_timeout),
        )
        return resp.json()

    # ==================== Distrobuilder Image Catalog Endpoints ====================

    def list_images(self) -> List[Dict[str, Any]]:
        """GET /images - Lists registered Distrobuilder images and build statuses."""
        resp = self._request("GET", "/images")
        return resp.json()

    def get_image(self, alias: str) -> Dict[str, Any]:
        """GET /images/{alias} - Retrieves status and fingerprint of a specific image."""
        resp = self._request("GET", f"/images/{alias}")
        return resp.json()

    def build_image(
        self,
        definition_bytes: bytes,
        alias: Optional[str] = None,
        force: bool = False,
    ) -> Dict[str, Any]:
        """PUT /images/{alias} - Submits Distrobuilder YAML file or context archive to initiate async background build."""
        target_alias = alias
        if not target_alias:
            try:
                import yaml
                parsed = yaml.safe_load(definition_bytes.decode("utf-8", errors="ignore"))
                if isinstance(parsed, dict) and "image" in parsed and isinstance(parsed["image"], dict):
                    import re
                    desc = str(parsed["image"].get("description", "")).lower().replace(" ", "-")
                    target_alias = re.sub(r"[^a-z0-9-]", "", desc)[:32] or None
            except Exception:
                pass
            if not target_alias:
                import hashlib
                h = hashlib.sha256(definition_bytes).hexdigest()[:12]
                target_alias = f"ba-{h}"

        files = {"file": ("definition.yaml", definition_bytes, "application/octet-stream")}
        params = {"force": "true"} if force else {}
        resp = self._request(
            "PUT",
            f"/images/{target_alias}",
            params=params,
            files=files,
            timeout=(self.connect_timeout, 30.0),
        )
        return resp.json()

    def get_image_logs(self, alias: str) -> str:
        """GET /images/{alias}/logs - Retrieves real-time or completed build compilation logs."""
        resp = self._request(
            "GET",
            f"/images/{alias}/logs",
            timeout=(self.connect_timeout, 15.0),
        )
        return resp.text

    def delete_image(self, alias: str) -> None:
        """DELETE /images/{alias} - Removes container image from Incus storage and database."""
        self._request("DELETE", f"/images/{alias}", timeout=(self.connect_timeout, 30.0))
