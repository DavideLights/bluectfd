# BlueAgent CTFd Plugin (`blueagent_patch`)

First-party CTFd plugin integrating with **BlueAgent v2** (bare-metal cyber-defense orchestrator running on Debian 13 with Incus, ZFS, OVN, and WireGuard).

> For full architectural diagrams, database schema details, HTTP endpoint tables, and contributor guidelines, see [`../../../README.md`](../../../README.md) and [`../../../CONTRIBUTING.md`](../../../CONTRIBUTING.md).

---

## Key Features

1. **Automated Dark OVN Verification**:
   - Triggers instantaneous ZFS Copy-on-Write (`--instance-only`) clones of running team containers on BlueAgent.
   - Attaches clones to ephemeral Dark OVN logical switches isolated from player VPN tunnels.
   - Executes readiness gates and real Proof-of-Concept exploit scripts while sanitizing `verify_commands` from all player-facing API responses.
   - On `passed == True`, atomically records a solve in CTFd's `Solves` table, clears standings caches, and awards points.
2. **Deterministic WireGuard & Bridge Addressing**:
   - Allocates a deterministic integer `team_k` in range `1..254` (`0` reserved for host/infrastructure).
   - Per-team WireGuard VPN subnet: `10.k.0.0/24` (gateway `10.k.0.1`, peers `10.k.0.2..254`).
   - Per-team container challenge subnet: `10.k.1.0/24` (host bridge `br-team-k` `10.k.1.1`, containers `10.k.1.2..254`).
   - Ephemeral verifier probe container: default static IP `10.k.1.250`.
3. **Template Placeholders in Challenge Specs**:
   - Author reusable YAML/JSON specifications using `{{team_k}}`, `{{subnet}}`, `{{verifier_ip}}`, and `{{ip:<container_name>}}`.
   - Both the plugin (`spec.py`) and BlueAgent (`models.py`) natively interpolate team-specific IPs during deployment and verification.
4. **Collision & Concurrency Protections**:
   - Validates global uniqueness of container `name` and `ip_suffix` across all stored `blueagent_patch` challenges to prevent L2 IP and Incus naming clashes.
   - Enforces Redis-backed atomic verification cooldowns per team (default `30s`) and refunds cooldowns automatically on BlueAgent `HTTP 409 Conflict` or `5xx`/timeout errors.
5. **Administrative Control Center** (`/admin/plugins/blueagent`):
   - Live orchestrator health & readiness badges.
   - Teams & WireGuard management: manual provisioning, live peer telemetry (handshake timestamps, Rx/Tx bandwidth), WireGuard peer reset, and full team environment purge.
   - Running container topology monitor, bulk challenge deployer, and selective per-challenge teardown.
   - Distrobuilder image catalog management (upload `.yaml` or `.tar.gz`/`.zip` build contexts, inspect build logs, delete).
   - Paginated verification audit log and runtime plugin settings editor.

---

## File Structure

```text
CTFd/CTFd/plugins/blueagent/
├── __init__.py            # Plugin entrypoint: load(app), DB create_all, menu bar hook
├── config.py              # Configuration loader (DB config > Env vars > Defaults)
├── client.py              # Typed BlueAgent REST client covering health, teams, members, machines, images
├── models.py              # SQLAlchemy models: BlueAgentPatchChallenge, TeamMapping, Deployment, VerifyLog
├── spec.py                # IaC YAML/JSON parser, validator, collision checker, and template engine
├── locks.py               # Distributed Redis/cache cooldowns and team mutexes
├── services.py            # Business logic orchestration (lazy provisioning, deploy, verify & solve)
├── challenge.py           # BaseChallenge subclass (BlueAgentPatchChallengeType / blueagent_patch)
├── routes.py              # Flask Blueprint for player & admin endpoints
├── assets/
│   ├── blueagent.css      # Custom styling tokens and status badges
│   ├── create.html        # Admin challenge create modal (YAML/JSON spec + live validator)
│   ├── create.js          # Admin create preview & validation controller
│   ├── update.html        # Admin challenge edit modal
│   ├── update.js          # Admin edit controller
│   ├── view.html          # Player challenge modal (VPN download, targets, verify button)
│   ├── view.js            # Player view controller (polling, verification runner, countdown)
│   └── admin.js           # 5-tab Admin Control Center AJAX controller
├── templates/
│   └── blueagent/
│       └── admin_dashboard.html # Admin Control Center extending admin/base.html
└── tests/
    ├── conftest.py        # CTFd test harness and fixtures
    ├── test_spec.py       # Spec parsing, validation, collision, and placeholder tests
    ├── test_client.py     # Responses-mocked client error handling tests
    ├── test_mapping.py    # Deterministic team_k allocation and capacity tests
    ├── test_verify_flow.py# Atomic solve recording and cooldown refund tests
    ├── test_routes_admin.py# Admin and player route authorization tests
    └── test_use_cases.py  # End-to-end plugin workflow & edge-case tests
```

---

## Running Tests

Execute the automated pytest suite from the `CTFd/` root directory:

```bash
.venv/bin/pytest CTFd/plugins/blueagent/tests -v
```

All **28 tests** covering specification validation, client HTTP error mappings, deterministic `team_k` concurrency, RBAC routes, and verify-and-solve workflows run standalone without requiring a live BlueAgent host.
