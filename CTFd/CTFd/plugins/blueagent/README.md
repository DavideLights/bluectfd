# BlueAgent CTFd Plugin

Battle-tested CTFd plugin integrating with **BlueAgent v2** (Bare-Metal Cyber-Defense Orchestrator running on Debian 13 with Incus, OVN, and WireGuard).

---

## 🌟 Key Features

1. **Automated Dark OVN Verification**:
   - Creates ZFS Copy-on-Write (CoW) instantaneous clones of running team containers.
   - Attaches clones to ephemeral dark OVN logical switches isolated from players and the internet.
   - Executes functional readiness probes and real exploit scripts.
   - On `passed == True`, immediately records solve in CTFd database, updates scoreboard standings, and awards points.
2. **Deterministic WireGuard Network Addressing**:
   - Deterministic integer mapping `team_k` in range `1..254` (0 reserved for host/infrastructure).
   - Per-team WireGuard VPN subnet: `10.k.0.0/24` (gateway `10.k.0.1`, peers `10.k.0.2..254`).
   - Per-team container challenge subnet: `10.k.1.0/24` (host bridge `br-team-k` `10.k.1.1`, containers `10.k.1.2..254`).
   - Ephemeral verifier probe container: static IP `10.k.1.250`.
3. **Template Placeholders in Challenge Specs**:
   - Write reusable IaC YAML/JSON specifications using `{{team_k}}`, `{{subnet}}`, `{{verifier_ip}}`, and `{{ip:<container_name>}}`.
   - Plugin dynamically interpolates target IPs for each specific team upon deploy and verification.
4. **Collision & Concurrency Protections**:
   - Validates that container names and IP suffixes are globally unique across all active BlueAgent challenges to prevent Incus naming clashes.
   - Enforces Redis-backed atomic verification cooldowns per team (default 30 seconds).
   - Acquires team-level mutexes; gracefully handles BlueAgent HTTP 409 Conflict with countdown timers without penalizing player attempt counts.
5. **Administrative Control Center** (`/admin/plugins/blueagent`):
   - Real-time orchestrator health badge.
   - Teams & WireGuard management: manual provisioning, live WireGuard peer telemetry (handshake timestamps, Rx/Tx bandwidth), peer reset, and complete team environment purge.
   - Running container topology monitor and bulk challenge deployer.
   - Distrobuilder image catalog management (upload YAML to build images async, inspect build status, delete).
   - Verification history audit log.

---

## 📁 File Structure

```
CTFd/CTFd/plugins/blueagent/
├── __init__.py            # Plugin entrypoint: load(app), DB create_all, menu bar hook
├── config.py              # Configuration loader with precedence hierarchy
├── client.py              # Production BlueAgent REST client covering 20 endpoints
├── models.py              # SQLAlchemy models: BlueAgentPatchChallenge, TeamMapping, Deployment, VerifyLog
├── spec.py                # IaC parser, validator, collision checker, and template engine
├── locks.py               # Distributed Redis/cache cooldowns and team mutexes
├── services.py            # Business logic orchestration (lazy provisioning, deploy, verify & solve)
├── challenge.py           # BaseChallenge subclass (BlueAgentPatchChallengeType)
├── routes.py              # Flask Blueprint for player & admin endpoints
├── assets/
│   ├── blueagent.css      # Custom styling tokens and animations
│   ├── create.html        # Admin challenge create modal (tabbed IaC upload)
│   ├── create.js          # Admin create preview controller
│   ├── update.html        # Admin challenge edit modal
│   ├── update.js          # Admin edit controller
│   ├── view.html          # Player challenge modal (VPN download, targets, verify button)
│   ├── view.js            # Player view controller (polling, verification runner, countdown)
│   └── admin.js           # Admin dashboard AJAX controller
├── templates/
│   └── blueagent/
│       └── admin_dashboard.html # Admin dashboard extending admin/base.html
└── tests/
    ├── conftest.py        # CTFd test harness and fixtures
    ├── test_spec.py       # Spec parsing, validation, and placeholder tests
    ├── test_client.py     # Responses-mocked client error handling tests
    ├── test_mapping.py    # Deterministic team_k allocation and capacity tests
    ├── test_verify_flow.py# Atomic solve recording and cooldown tests
    └── test_routes_admin.py# Admin and player route authorization tests
```

---

## 🚀 Docker Compose Setup

When running CTFd in Docker Compose and BlueAgent natively on the Debian host:

1. **BlueAgent Host Configuration**:
   Ensure BlueAgent listens on `0.0.0.0:8000` or the Docker bridge IP (`172.17.0.1:8000`):
   ```bash
   cd /home/debian/bluectfd/blueagent
   python3 -m blueagent.main
   ```

2. **Run CTFd**:
   ```bash
   cd /home/debian/bluectfd/CTFd
   docker compose up -d
   ```
   CTFd connects to BlueAgent via `http://172.17.0.1:8000` using the bearer token configured in `BLUEAGENT_TOKEN`.

---

## 🧪 Running Tests

Execute the automated pytest suite using the test harness:
```bash
/home/debian/bluectfd/CTFd/.venv/bin/pytest /home/debian/bluectfd/CTFd/CTFd/plugins/blueagent/tests -v
```
All 19 tests covering specification validation, client HTTP error mappings, team mapping concurrency, and verify-and-solve flows pass with 100% green status.
