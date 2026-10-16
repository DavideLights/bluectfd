# BlueAgent CTFd Plugin: API Routes & Endpoints Reference

## Blueprint: `blueagent_bp` in `routes.py`

---

## 1. Player Endpoints (`@authed_only`)

| Endpoint | Method | Params / Body | Description |
|---|---|---|---|
| `/api/v1/plugins/blueagent/status` | `GET` | `?challenge_id=<id>` | Returns `team_k`, subnet, WireGuard status, deployment state (`running`/`deploying`/`idle`), and target container list with concrete IPs. |
| `/api/v1/plugins/blueagent/vpn/config` | `GET` | None | Generates and downloads player's individual WireGuard `.conf` profile as file attachment. |
| `/api/v1/plugins/blueagent/deploy` | `POST` | `{"challenge_id": int}` | Manually triggers lazy provisioning and container deployment for the current team. |
| `/api/v1/plugins/blueagent/challenges/<id>/verify` | `POST` | None | Triggers Dark OVN patch verification, enforces cooldowns, and commits score on pass. |

---

## 2. Admin Management Endpoints (`@admins_only`)

| Endpoint | Method | Body / Params | Description |
|---|---|---|---|
| `/admin/plugins/blueagent` | `GET` | None | Renders full administrative control dashboard. |
| `/admin/plugins/blueagent/teams` | `GET` | None | Lists all registered teams, `team_k` mappings, and bridge statuses. |
| `/admin/plugins/blueagent/teams` | `POST` | `{"team_k": int, "name": str}` | Manually provisions Linux bridge `br-team-k` and WireGuard `wg-k`. |
| `/admin/plugins/blueagent/teams/<team_k>/purge` | `POST` | None | Destroys all containers, tears down bridge and WG tunnel, and clears mappings for team. |
| `/admin/plugins/blueagent/teams/<team_k>/peers` | `GET` | None | Returns live WireGuard telemetry (last handshake, Rx/Tx bandwidth, peer status). |
| `/admin/plugins/blueagent/teams/<team_k>/peers/<member_id>` | `DELETE` | None | Revokes player's WireGuard peer key from kernel interface. |
| `/admin/plugins/blueagent/images` | `GET` | None | Lists registered Incus/Distrobuilder images and compilation states. |
| `/admin/plugins/blueagent/images` | `POST` | Multipart form (`definition_file`, `alias`) | Submits YAML definition or build context tarball to compile image async. |
| `/admin/plugins/blueagent/images/<alias>` | `DELETE` | None | Deletes image from Incus storage pool and orchestrator database. |
| `/admin/plugins/blueagent/images/<alias>/logs` | `GET` | None | Streams raw stdout/stderr compiler log for the image build process. |
| `/admin/plugins/blueagent/deploy-all` | `POST` | `{"challenge_id": int}` | Bulk deploys a challenge topology across all active teams simultaneously. |
| `/admin/plugins/blueagent/settings` | `GET`, `POST` | `{"url": str, "token": str, "cooldown": int}` | Inspects or updates BlueAgent connectivity and operational parameters. |
