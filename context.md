# REPO CONTEXT: BLUECTFD MONOREPO (BLUEAGENT + CTFD PLUGIN)

## WORKSPACE MAP
- Monorepo root: `/home/debian/bluectfd`
- Backend Orchestrator: `/home/debian/bluectfd/blueagent`
- Game Engine: `/home/debian/bluectfd/CTFd`
- CTFd BlueAgent Plugin: `/home/debian/bluectfd/CTFd/CTFd/plugins/blueagent`
- Integration Test Script: `/home/debian/bluectfd/test_ui_fix.py`
- BlueAgent Docs: `/home/debian/bluectfd/blueagent/agent/blueagent_documentation`
- CTFd Plugin Docs: `/home/debian/bluectfd/CTFd/context/CTFd_plugin_documentation`

---

## SYSTEM DIVISION & INTERACTION CONTRACT

```
[CTFd Core Engine] (Port 4000/8000)
       │
       │ Internal Plugin: blueagent
       │ (models.py, client.py, services.py, spec.py)
       │
       ▼ REST HTTP Calls (Bearer Token)
[BlueAgent FastApi] (Host Port 8000)
       │
       ├── Incus Engine (UDS /var/lib/incus/unix.socket)
       ├── Dark OVN SDN Switches (Ephemeral Verifier)
       ├── Linux Kernel WireGuard (wg-k) & Bridges (br-team-k)
       └── SQLite DB (blueagent.db)
```

| Domain | CTFd Plugin Responsibility | BlueAgent Orchestrator Responsibility |
|---|---|---|
| Identity | Maps user/team account ID to `team_k` (1..254) | Consumes integer `team_k` |
| Infrastructure | Triggers lazy provision / teardown | Configures `br-team-k`, `wg-k`, Incus containers |
| VPN Access | Serves `.conf` download to browser | Generates Curve25519 keys, programs kernel peers |
| Challenge IaC | Parses YAML/JSON, checks collisions, interpolates IPs | Launches Incus instances, sets CPU/RAM limits |
| Verification | Enforces cooldowns, locks team, records solves/fails | Clones containers (ZFS CoW), runs OVN probe |
| Scoreboard | Commits to `Solves`/`Fails`, busts cache | Returns JSON `passed: bool` + details |

---

## INTEGRATION SYNC MATRIX (WHEN EDITING CODE)

### 1. API Changes in BlueAgent -> CTFd Plugin Sync
- If modifying route in `blueagent/src/blueagent/api/`:
  - Update matching method in `CTFd/CTFd/plugins/blueagent/client.py`.
  - Update route caller in `CTFd/CTFd/plugins/blueagent/services.py` or `routes.py`.
  - Update mock responses in `CTFd/CTFd/plugins/blueagent/tests/test_client.py` and `test_use_cases.py`.

### 2. Challenge Schema Changes -> IaC Engine Sync
- If modifying `ChallengeConfig` schema in `blueagent/src/blueagent/models.py`:
  - Update `validate_spec_structure()` in `CTFd/CTFd/plugins/blueagent/spec.py`.
  - Update template placeholders in `render_spec_for_team()` (`{{team_k}}`, `{{ip:<name>}}`).
  - Update UI create/update modals: `assets/create.html` and `assets/create.js`.

### 3. Verification Contract Sync
- BlueAgent verification route: `POST /teams/{team_k}/verificator`.
- Expected return schema:
  `{"success": bool, "passed": bool, "message": str, "details": dict}`.
- Error codes contract:
  - `404`: Team environment missing -> CTFd prompts "Start Environment".
  - `409`: Sandbox busy / locked -> CTFd refunds cooldown, returns retry countdown.
  - `503`: Orchestrator down -> CTFd refunds cooldown, informs player.

### 4. Distrobuilder Image Management Sync
- Endpoints:
  - `POST /images` or `PUT /images/{alias}` (supports single YAML or `.tar.gz`/`.zip` build context).
  - `GET /images` (catalog list).
  - `GET /images/{alias}/logs` (real-time build output streaming).
  - `DELETE /images/{alias}` (purge from Incus + DB).
- When altering image endpoints in BlueAgent, update:
  - `client.py` methods: `build_image()`, `get_image_logs()`, `delete_image()`.
  - Admin dashboard routes in `routes.py`: `/admin/plugins/blueagent/images/*`.
  - UI controller `assets/admin.js` log streaming modal.

---

## NETWORK ARCHITECTURE & ADDRESS SCHEME
- `team_k` integer: `1..254` (Slot `0` = host/infra).
- WireGuard VPN: `10.k.0.0/24`. Host endpoint `10.k.0.1`, peers `10.k.0.2..254`.
- Container Bridge: `10.k.1.0/24`. Host bridge `br-team-k` `10.k.1.1`, containers `10.k.1.2..249`.
- Ephemeral Verifier IP: Fixed `10.k.1.250` on dark OVN logical switch.
- Host routing during dev:
  - Local mode: CTFd talks to `http://127.0.0.1:8000`.
  - Docker mode: CTFd talks to host gateway `http://172.17.0.1:8000` or `http://host.docker.internal:8000`.

---

## DATABASE TOUCHPOINTS
- **CTFd Database** (`CTFd.db` / MySQL):
  - Table `challenges`: Core metadata (name, value, category, state).
  - Table `blueagent_challenges`: Polymorphic extension (FK `challenges.id` CASCADE). Stored spec, cooldown, toggles.
  - Table `blueagent_team_mappings`: Maps CTFd account ID to `team_k` (1..254).
  - Table `blueagent_deployments`: Tracks status per team (`idle`, `deploying`, `running`, `failed`, `torn_down`).
  - Table `blueagent_verify_logs`: Verification run history.
  - Tables `solves` / `fails`: CTFd scoreboard records.
- **BlueAgent Database** (`blueagent/blueagent.db` SQLite):
  - Table `teams`: Provisioned `br-team-k`, `wg-k` states.
  - Table `team_members`: Curve25519 public keys, assigned VPN IPs (`10.k.0.x`).
  - Table `images`: Distrobuilder image aliases, hashes, build status (`building`, `ready`, `failed`).
  - Table `containers`: Active running container records.

---

## CRITICAL CROSS-PROJECT TRAPS & INVARIANTS
1. **Never Skip Cooldown Refund**: If BlueAgent returns 409 conflict, 404, or 503, CTFd plugin MUST call `refund_cooldown(team_k, challenge_id)` so player attempt count and timer remain unpenalized.
2. **Global Collision Prevention**: Before submitting `ChallengeConfig` to BlueAgent, CTFd `spec.py` must verify container names and IP suffixes do not collide with any other active challenge.
3. **Exploit Command Leak Protection**: BlueAgent `ChallengeConfig` contains exploit scripts in `verify_commands`. CTFd `challenge.py:read()` must sanitize these fields before returning JSON to players.
4. **Tarball Memory Leak Prevention**: When uploading context archives to BlueAgent `/images`, stream chunks via async generator (`chunk_size=1MB`) to prevent RAM exhaustion.
5. **Verifier Image Verification**: Before deploying containers, verify `config.verifier.image` exists and status is `ready`.
6. **CSRF Nonce Mandatory**: All AJAX requests sent to CTFd routes require header `"CSRF-Token": window.init.csrfNonce`.
7. **Safe jQuery Loading**: CTFd scripts must avoid bare `$`. Wrap DOM handlers in `jQuery(function($) { ... })`.

---

## COMMON DEVELOPMENT WORKFLOW COMMANDS

### BlueAgent Backend
```bash
# Enter BlueAgent directory
cd /home/debian/bluectfd/blueagent

# Run tests
pytest tests/ -v

# Start BlueAgent daemon locally (Port 8000)
python3 -m blueagent.main
# Or via uvicorn directly:
uvicorn blueagent.main:app --host 0.0.0.0 --port 8000 --reload
```

### CTFd & BlueAgent Plugin
```bash
# Enter CTFd directory
cd /home/debian/bluectfd/CTFd

# Activate virtual environment
source .venv/bin/activate

# Run plugin unit & integration test suite (28 tests)
pytest CTFd/plugins/blueagent/tests/ -v

# Run CTFd server locally (Port 4000)
python serve.py

# Or run CTFd via Docker Compose
docker compose up -d
```

### End-to-End System Integration Test
```bash
# Test complete flow: login, admin dashboard, challenge creation API, types endpoint
python3 /home/debian/bluectfd/test_ui_fix.py
```

---

## BlueAgent Hardening & Optimization Changelog (October 2026)

- **Reliability & Concurrency (L1–L6)**:
  - **L1**: Lifespan startup reconciliation in `main.py` restores `br-team-k`, `wg-k` WireGuard peers from SQLite, restarts stopped containers, and reapplies runtime IPs.
  - **L2**: `LifecycleManager` checks `locks.is_verify_locked(team_k)` before purging OVN switches, eliminating verification race conditions.
  - **L3**: Immediate clone registration in `self.clones` prevents CoW zombie containers upon partial failure.
  - **L4**: Deterministic single execution on `verify_commands` (no more blind 3x loop).
  - **L5**: Dynamic OVN NAT and uplink configuration respecting `config.verify_internet_access`.
  - **L6**: Concurrency fix in `LockRegistry` preventing timeout mutation on shared locks.
- **Performance & I/O (P1–P4)**:
  - **P1**: Parallelized container deploy, teardown, and OVN clone provisioning via `asyncio.gather`.
  - **P2**: Added `capture_output: bool = True` to `incus exec`, dropping HTTP roundtrips from 6 to 2 per network command when output capture is unneeded.
  - **P4**: Switched `SubprocessRunner` from disk tempfiles to in-memory async pipes (`communicate(input=stdin)`), eliminating disk I/O and securing keys.
- **Client & Usability**:
  - Native template placeholder rendering (`{{team_k}}`, `{{subnet}}`, `{{verifier_ip}}`, `{{ip:<name>}}`) directly in BlueAgent.
  - Clean `DELETE /teams/{team_k}/machines?containers=web,db` query params support.
  - Dynamic host IP detection in `start_services.sh` from interface `enp0s3`.
- **Unit Test Coverage**:
  - 111 passed tests (`pytest tests/test_*.py -v`).
