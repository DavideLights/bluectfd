# BlueAgent CTFd Plugin: REST API Client

## Module: `CTFd/CTFd/plugins/blueagent/client.py`

Full-featured HTTP client implementing the contract between CTFd and BlueAgent v2 FastAPI orchestrator.

---

## 1. Client Configuration & Retries

- **Base URL**: `settings.url` (Default: `http://127.0.0.1:8000` or Docker gateway `http://172.17.0.1:8000`).
- **Auth Header**: Bearer token via `Authorization: Bearer <token>` and fallback `X-CTFd-Token: <token>`.
- **Session Pooling**: Uses `requests.Session` with `HTTPAdapter` configured for connection pooling and automated retries (3 retries, backoff factor 0.3) for idempotent GET requests.
- **Timeouts**: Split connect timeout (default 5.0s) and read timeout (default 10.0s, extended to 300.0s for image builds and verification).

---

## 2. API Endpoints Map (20 Endpoints)

| Category | Method & Path | Client Method | Purpose |
|---|---|---|---|
| **Health** | `GET /health` | `get_health()` | Basic liveness check |
| | `GET /health/ready` | `get_health_ready()` | Deep readiness probe (Incus, OVN, bridges) |
| **Teams** | `POST /teams` | `create_team(team_k, name)` | Provisions `br-team-k` & `wg-k` |
| | `GET /teams` | `list_teams()` | Lists all provisioned teams |
| | `GET /teams/{k}` | `get_team(team_k)` | Detailed team metadata & network status |
| | `DELETE /teams/{k}` | `delete_team(team_k)` | Complete teardown of team containers & networks |
| **WireGuard** | `POST /teams/{k}/members` | `add_member(...)` | Allocates member IP & generates WG config |
| | `GET /teams/{k}/members` | `list_members(team_k)` | Lists registered team members |
| | `GET /teams/{k}/members/{id}` | `get_member(team_k, id)` | Member details & assigned VPN IP |
| | `GET /teams/{k}/members/{id}/config` | `get_member_config(team_k, id)` | Downloads raw `.conf` WireGuard file |
| | `DELETE /teams/{k}/members/{id}` | `delete_member(team_k, id)` | Revokes peer key and frees VPN IP |
| | `GET /teams/{k}/peers` | `get_peer_stats(team_k)` | Live telemetry (handshake, Rx/Tx, online status) |
| **Machines** | `POST /teams/{k}/machines` | `deploy_challenge(team_k, config)` | Deploys container topology to team bridge |
| | `GET /teams/{k}/machines` | `list_machines(team_k)` | Lists active containers for team |
| | `DELETE /teams/{k}/machines` | `teardown_machines(team_k, config)`| Stops and destroys containers |
| **Verification** | `POST /teams/{k}/verificator` | `verify_challenge(team_k, config)`| Executes Dark OVN verification test |
| **Images** | `POST /images` | `build_image(definition_bytes, alias)`| Async Distrobuilder image compilation |
| | `GET /images` | `list_images()` | Image catalog and build statuses |
| | `GET /images/{alias}` | `get_image(alias)` | Specific image build status |
| | `DELETE /images/{alias}` | `delete_image(alias)` | Deletes image from Incus & database |
| | `GET /images/{alias}/logs`| `get_image_logs(alias)` | Fetches raw compiler build stdout/stderr |

---

## 3. Exception Hierarchy

```
BlueAgentError (Base)
├── BlueAgentUnavailable (Connection refused, network timeout)
├── BlueAgentAuthError (401 Unauthorized - invalid token)
├── TeamNotFound (404 Not Found)
├── BlueAgentConflict (409 Conflict)
│   ├── is_verify_busy: Verification already running on team
│   ├── is_bridge_not_active: Team bridge not yet created
│   ├── is_member_assigned: User already registered to VPN
│   └── is_image_state: Image still building
└── BlueAgentValidation (422 Unprocessable Entity)
```
