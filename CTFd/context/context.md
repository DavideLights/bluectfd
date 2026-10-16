# PROJECT CONTEXT: BLUECTFD / BLUEAGENT PLUGIN

## PROJECT ROOTS & STRUCTURE
- Workspace root: `/home/debian/bluectfd`
- CTFd engine root: `/home/debian/bluectfd/CTFd`
- CTFd plugin path: `/home/debian/bluectfd/CTFd/CTFd/plugins/blueagent`
- BlueAgent orchestrator root: `/home/debian/bluectfd/blueagent`
- Plugin docs path: `/home/debian/bluectfd/CTFd/context/CTFd_plugin_documentation`

---

## SYSTEM ARCHITECTURE
- CTFd = Web game engine. Player auth, team accounts, scoring, flags, solve logging.
- BlueAgent = Bare-metal orchestrator Debian 13. Controls Incus, OVN switches, WireGuard kernel, ZFS storage.
- Communication = REST API HTTP `http://172.17.0.1:8000` (Docker bridge) or `http://127.0.0.1:8000` (local). Bearer token `BLUEAGENT_TOKEN`.

---

## NETWORK ADDRESSING MAP
- Team index `team_k` = Integer `1..254`. Slot `0` reserved host.
- VPN Subnet = `10.k.0.0/24`. Gateway `10.k.0.1` (`wg-k`). Peers `10.k.0.2..254`.
- Container Subnet = `10.k.1.0/24`. Gateway bridge `br-team-k` (`10.k.1.1`). Containers `10.k.1.2..249`.
- Verifier probe = Static IP `10.k.1.250` on dark OVN switch.

---

## PLUGIN CODE DIRECTORY MAP
- `__init__.py`: Plugin entrypoint. Runs `app.db.create_all()`, registers `BlueAgentPatchChallengeType`, hooks blueprint, mounts static assets `/plugins/blueagent/assets/`, adds admin menu link.
- `config.py`: Resolves settings. Hierarchy: CTFd DB config -> env vars -> fallback defaults.
- `client.py`: Complete REST client. 20 endpoints. Pools connections, handles retries, translates HTTP errors (`BlueAgentConflict`, `TeamNotFound`, `BlueAgentUnavailable`).
- `models.py`: 4 SQLAlchemy models.
  - `BlueAgentPatchChallenge`: Polymorphic `Challenges` child. FK `ondelete="CASCADE"`. Stored spec JSON, cooldown, toggles.
  - `BlueAgentTeamMapping`: Allocates `team_k` `1..254`. Nested transaction retry against race condition.
  - `BlueAgentDeployment`: Tracks container state per `team_k` and `challenge_id`.
  - `BlueAgentVerifyLog`: Verification audit trail.
- `spec.py`: IaC parser. Supports YAML/JSON `<= 256KB`. Schema validation. Global container name / IP suffix collision blocker across active challenges. Deep template placeholder engine (`{{team_k}}`, `{{subnet}}`, `{{ip:<name>}}`, `{{verifier_ip}}`).
- `locks.py`: Redis atomic cooldowns + team mutex locks (`team_verify_lock`, `team_deploy_lock`). In-process fallback. Refunds cooldown on 409 conflict.
- `services.py`: Orchestrator service. Context resolution (Teams vs Users mode). Lazy provisioning (`POST /teams`, `POST /teams/{k}/members`). Deploy/teardown coordinator. Verification executor & solve committer.
- `challenge.py`: Subclasses `BaseChallenge`. Handles challenge create/update/delete/attempt. Redacts secret exploit commands from player JSON. Cascades container deletion on challenge delete.
- `routes.py`: Flask blueprint. Player endpoints (`status`, `vpn/config`, `deploy`, `verify`). Admin endpoints (`admin_dashboard`, `teams`, `purge`, `peers`, `images`, `logs`, `deploy-all`, `settings`).
- `assets/`:
  - `blueagent.css`: UI tokens, dark styling, status badges, timers.
  - `create.html` / `create.js`: Admin challenge creation modal. Tabbed editor, spec validator.
  - `update.html` / `update.js`: Admin challenge editor modal.
  - `view.html` / `view.js`: Player modal. VPN config download, target IP list, deploy button, verify runner with countdown.
  - `admin.js`: Admin dashboard AJAX logic. Loaded `defer`. Safe jQuery wrapper.
- `templates/blueagent/admin_dashboard.html`: Admin panel extending `admin/base.html`. Health check, team table, live WireGuard peer telemetry, Distrobuilder image catalog, bulk deployer, verify audit logs.
- `tests/`: 7 test files, 28 tests passing (`test_spec.py`, `test_client.py`, `test_mapping.py`, `test_verify_flow.py`, `test_routes_admin.py`, `test_use_cases.py`, `conftest.py`).

---

## VERIFICATION & SOLVE COMMIT PIPELINE
1. Check existing solve in `Solves` table. Exit early if solved.
2. Check `check_and_set_cooldown(team_k, challenge_id)`. Return 429 if active.
3. Acquire `team_verify_lock(team_k)`. Return 409 + refund cooldown if busy.
4. Call BlueAgent `POST /teams/{k}/verificator`.
   - Snapshot container via ZFS CoW clone.
   - Attach clone to dark OVN switch.
   - Run readiness check.
   - Run exploit probe from `10.k.1.250`.
   - Destroy clone & switch LIFO.
   - Return pass/fail verdict.
5. If BlueAgent reports conflict/error -> refund cooldown.
6. Write `BlueAgentVerifyLog`.
7. If passed:
   - Insert `Solves(provided="VERIFIED_PATCH")`.
   - Clear standings & challenge caches (`clear_standings()`, `clear_challenges()`).
   - Catch teammate race `IntegrityError` -> rollback cleanly.
8. If failed:
   - Insert `Fails(provided="FAILED_PATCH")`.
   - Return sanitized output if `show_verify_details` enabled.

---

## CRITICAL GOTCHAS & RULES
- **CSRF Token Required**: CTFd blocks `POST`/`PUT`/`DELETE` missing header `"CSRF-Token": window.init.csrfNonce`.
- **Modal HTML Format**: Modals (`create.html`, `update.html`, `view.html`) injected via AJAX. Never use `<!DOCTYPE html>`, `<html>`, `<body>`. Begin directly `<div class="modal-dialog">`.
- **Safe jQuery Init**: Never use bare `$()`. Wrap JS in `jQuery(function($) { ... })` or wait check loop.
- **Defer Admin Script**: Admin dashboard must load `admin.js` with `<script defer src="...">`.
- **SQLAlchemy Polymorphism**: Extension model needs `__mapper_args__ = {"polymorphic_identity": "blueagent_patch"}` and `db.ForeignKey("challenges.id", ondelete="CASCADE")`.
- **Cooldown Refund**: Always refund cooldown on infrastructure errors (409 conflict, 503 unavailable, connection drop) to avoid player penalties.

---

## FAST DEVELOPMENT COMMANDS
- Run virtualenv activation:
  ```bash
  source /home/debian/bluectfd/CTFd/.venv/bin/activate
  ```
- Run plugin test suite:
  ```bash
  pytest /home/debian/bluectfd/CTFd/CTFd/plugins/blueagent/tests/ -v
  ```
- Run CTFd server locally:
  ```bash
  python /home/debian/bluectfd/CTFd/serve.py
  ```
- Run Docker compose CTFd:
  ```bash
  docker compose -f /home/debian/bluectfd/CTFd/docker-compose.yml up -d
  ```
- Test CTFd endpoints & UI login:
  ```bash
  python3 /home/debian/bluectfd/test_ui_fix.py
  ```
