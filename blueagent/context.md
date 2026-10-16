# BlueAgent v2: Architectural Context & Implementation Reference

## 1. System Philosophy & Paradigms

1. **Client-Agnostic Container Management:**
   - BlueAgent is designed to manage container topologies per team on dedicated host bridges without maintaining challenge state.
   - Upstream clients (e.g. CTFd) are responsible for high-level challenge state tracking; BlueAgent reports running containers per team.
2. **Identification via `team_k`:**
   - All teams and network namespaces are uniquely identified by the integer index `team_k` (`0 <= team_k <= 254`).
   - The deprecated `team_id` has been completely eliminated from models, database schemas, and endpoints.
   - Team member identifiers are composite strings (`ctfd-{team_k}-{ctfd_user_id}`).
3. **Service Layer Architecture:**
   - Domain logic and infrastructure orchestration are encapsulated into dedicated service objects in `src/blueagent/services/` (`ChallengeService`, `ImageService`, `TeamService`).
   - API controllers in `src/blueagent/api/` serve as thin routing layers that map HTTP requests to service calls and translate domain exceptions into HTTP status codes.
4. **Hybrid Network Split:**
   - **Production (Live Kernel):** WireGuard (`wg-k`, `10.k.0.0/24`) terminated into unmanaged Linux Bridge (`br-team-k`, gateway `10.k.1.1/24`). Containers run on `eth0` with static IPs (`10.k.1.x`). Maximum I/O, zero SDN overhead, native `tcpdump` sniffing.
   - **Verification (Dark Sandbox):** Ephemeral Incus OVN overlay network (`verify-<session_id>`, parent `incusbr0`). Clones rebound to OVN switch without host routing table conflicts.
5. **Topological Fidelity:**
   - Instances maintain a single `eth0`, identical IP (`10.k.1.x`), and gateway (`10.k.1.1`) across live and sandbox. Application code is unaware of sandboxing.
   - Dual network configuration pre-baked into rootfs: `systemd-networkd` (`/etc/systemd/network/10-eth0.network`) and OpenRC/ifupdown (`/etc/network/interfaces`). Runtime static IPs and default routes are applied immediately via `ip addr replace` on container start.
6. **Sub-Second Copy-on-Write (CoW) Duplication:**
   - Incus instances reside on ZFS storage pools (`zfs/containers`). Cloned via block-level snapshots (`POST /1.0/instances` copy) in ~100 ms.
7. **Deferred Readiness & Dual-Mode Verification:**
   - Readiness probes are deferred to the verification phase inside dark OVN clones before running verify checks.
   - *In-Container (Agentless):* Executed inside target clone via `incus exec` (`check.target == "container"`).
   - *Ephemeral Verifier Runner:* Ephemeral container (`verify-runner-<uuid>`, `check.target == "verifier"`) attached directly to OVN switch for black-box network checks.
8. **Two-Tier Synchronization (Anti-HTTP 409):**
   - Intra-process coroutine lock (`asyncio.Lock`) + inter-process OS lock (`filelock.AsyncFileLock` on `/tmp/blueagent_locks/<name>.lock`) with task-level reentrancy.
9. **Self-Healing State Reconciliation:**
   - Background daemon sweeps orphan `verify-*` containers and OVN networks. Enforces 90s grace period to prevent race conditions on newly created networks.
10. **Traffic Engineering:**
    - Cryptokey Routing: Peer WireGuard `AllowedIPs = 10.k.0.x/32` tied to public key.
    - MSS Clamping: Enforced in `nftables` before accept rules to prevent PMTU blackhole packet drops (WireGuard MTU: `1420`, Geneve: `1442`).

---

## 2. Directory Layout & Module Responsibilities

```
src/blueagent/
├── config.py              # Pydantic BaseSettings (env prefix: BLUEAGENT_)
├── models.py              # Pydantic v2 domain schemas, IaC definitions, API models
├── database.py            # SQLite async database manager (aiosqlite) with WAL
├── locks.py               # TwoTierLock & LockRegistry (process & file locks)
├── dependencies.py        # FastAPI dependency injection providers (services, db, engine)
├── main.py                # FastAPI entrypoint, lifespan startup/shutdown
├── engine/
│   ├── base.py            # ContainerEngine ABC & ExecResult
│   ├── incus.py           # Concrete IncusEngine (UDS client /var/lib/incus/unix.socket)
│   └── network.py         # Incus OVN/bridge device helpers
├── network/
│   ├── addressing.py      # Pure IP plan generator (plan_for, allocate_member_ip)
│   ├── host_net.py        # HostNetworkManager (Linux bridges, routing, nftables)
│   ├── wireguard.py       # WireGuardManager (wg-k interface, peer allocation, keys)
│   └── _runner.py         # Async subprocess execution wrapper
├── images/
│   ├── __init__.py
│   └── distrobuilder.py   # ImageBuilder pipeline (compiles unified Incus tarballs)
├── services/
│   ├── __init__.py        # Exported service interfaces
│   ├── challenges.py      # ChallengeService (deploy, rollback, teardown, verify)
│   ├── images.py          # ImageService (YAML parsing, build orchestration, catalog)
│   └── teams.py           # TeamService (bridges, WireGuard, member keys, stats)
├── verifier/
│   └── ovn.py             # OVNVerifier async context manager (RAII verification)
├── lifecycle/
│   └── manager.py         # LifecycleManager (reconciliation loop, 90s grace period)
└── api/
    ├── __init__.py
    ├── health.py          # /health liveness probe
    ├── teams.py           # /teams endpoints (delegates to TeamService)
    ├── challenges.py      # /teams/{team_k}/machines & /teams/{team_k}/verificator (delegates to ChallengeService)
    └── images.py          # /images endpoints (delegates to ImageService)

tests/
├── test_api.py            # Basic API route sanity tests
├── test_challenges_api.py # Challenges & containers REST API unit tests
├── test_database.py       # SQLite database queries & state transition tests
├── test_engine.py         # Incus container engine client unit tests
├── test_images_api.py     # Image catalog and build unit tests
├── test_lifecycle.py      # Resource reconciliation and cleanup tests
├── test_locks.py          # Two-tier locking and reentrancy tests
├── test_models.py         # Pydantic schemas validation tests
├── test_network.py        # Network planning and WireGuard unit tests
├── test_teams_api.py      # Teams and WireGuard peers API tests
├── test_services.py       # Service layer direct unit tests (ChallengeService, ImageService, TeamService)
├── test_performance.py    # Performance & resource usage profiling (ResourceMonitor)
├── test_thesis.py         # Modular thesis benchmark harness (K challenges x M teams)
└── usability/             # Live integration tests against real Incus & Linux kernel
```

---

## 3. Comprehensive REST API Reference

All protected endpoints require authentication via CTFd token (`Authorization: Bearer <token>` or `X-CTFd-Token: <token>`).

### 3.1 Health & Liveness (`src/blueagent/api/health.py`)
- `GET /health`
  - **Auth**: Public
  - **Description**: Probes service liveness and runtime health.
  - **Response**: `{"status": "ok"}`

### 3.2 Teams & WireGuard VPN Management (`src/blueagent/api/teams.py`)
- `POST /teams`
  - **Payload**: `TeamCreateReq(team_k: int, name: str)` (`0 <= team_k <= 254`)
  - **Description**: Provisions dedicated Linux bridge `br-team-k` (`10.k.1.1/24`), WireGuard interface `wg-k` (port `51820 + team_k`), and adds interface to nftables `@team_links`. Idempotent.
- `GET /teams`
  - **Description**: Lists all provisioned teams.
- `GET /teams/{team_k}`
  - **Description**: Retrieves single team metadata and network parameters.
- `DELETE /teams/{team_k}`
  - **Description**: Tears down team containers, WireGuard interface, host bridge, and deletes team from database.
- `POST /teams/{team_k}/members`
  - **Payload**: `TeamMemberCreateReq(name: str, ctfd_user_id: int, public_key: Optional[str], team_name: Optional[str])`
  - **Description**: Registers a team member, allocates VPN IP (`10.k.0.2` - `10.k.0.254` with hole-filling), generates Curve25519 keypair if not provided, sets PSK, and updates WireGuard peers. Returns `TeamMemberWG` including rendered configuration.
- `GET /teams/{team_k}/members`
  - **Description**: Lists registered peers for team `team_k` (cryptographic private keys and PSKs masked).
- `GET /teams/{team_k}/members/{member_id}`
  - **Description**: Retrieves specific member record.
- `GET /teams/{team_k}/members/{member_id}/config`
  - **Description**: Returns raw WireGuard `.conf` configuration file in `text/plain` format.
- `DELETE /teams/{team_k}/members/{member_id}`
  - **Description**: Removes member from WireGuard peer list and frees allocated VPN IP.
- `GET /teams/{team_k}/peers`
  - **Description**: Telemetry endpoint correlating database member records with live `wg show dump` transfer and handshake statistics.

### 3.3 Container Management & Dark Verification (`src/blueagent/api/challenges.py`)
- `POST /teams/{team_k}/machines`
  - **Payload**: `ChallengeConfig` (`challenge_id: str`, `team_k: int`, `containers: List[ContainerSpec]`, `readiness_commands: List[CommandSpec]`, `verify_commands: List[CommandSpec]`, `verifier: Optional[VerifierSpec]`, `verify_internet_access: bool`)
  - **Description**: Deploys a multi-container challenge to `br-team-k`. Injects static IP configuration (`10.k.1.{suffix}`) via `systemd-networkd` and `/etc/network/interfaces`, starts containers, applies runtime static IPs, and records containers in SQLite. Readiness checks are deferred to verification. Performs atomic rollback on failure.
- `GET /teams/{team_k}/machines`
  - **Description**: Retrieves list of running containers (`RunningContainerRecord`) for team `team_k`.
- `DELETE /teams/{team_k}/machines`
  - **Payload**: Optional `ChallengeConfig`
  - **Description**: Tears down containers for team `team_k`. If config container list is supplied, only tears down specified containers.
- `POST /teams/{team_k}/verificator`
  - **Payload**: `ChallengeConfig`
  - **Description**: Executes non-destructive automated verification against dark OVN sandbox clones. Clones containers with ZFS CoW, attaches them to isolated dark switch `verify-{session_id}`, runs readiness checks until healthy, then executes verification commands, and cleans up via RAII.

### 3.4 Distrobuilder Image Catalog & Build Pipeline (`src/blueagent/api/images.py`)
- `POST /images`
  - **Form Data**: `definition` (Multipart File: Distrobuilder YAML), `alias` (Optional string)
  - **Description**: Submits Distrobuilder YAML. Uses write-ahead CAS in database. If image is new, triggers background asynchronous compilation via `ImageBuilder.build_image()` and returns `202 Accepted`. If already building or ready, returns current record idempotently.
- `GET /images`
  - **Description**: Lists all registered container images, build statuses (`building`, `ready`, `failed`), and fingerprints.
- `GET /images/{alias}`
  - **Description**: Retrieves status and build metadata for a specific image alias.
- `DELETE /images/{alias}`
  - **Description**: Deletes image alias and deletes Incus image if not in use by active challenge containers (enforces SQLite foreign key constraints). Rejects with `409 Conflict` if in use.

---

## 4. Subsystems & Services

### 4.1 Service Layer (`src/blueagent/services/`)
- **`ChallengeService`**:
  - Handles container provisioning, static IP injection, atomic rollbacks on failure, container teardowns, and dark OVN sandbox verification.
  - Domain exceptions: `TeamNotFoundError`, `BridgeNotActiveError`, `ImageNotReadyError`, `DeploymentRollbackError`, `DeploymentFailedError`, `VerificationLockedError`, `TeardownError`.
- **`ImageService`**:
  - Validates Distrobuilder YAML, generates SHA256 hashes, handles alias naming patterns, coordinates background builds with `ImageBuilder`, and enforces safe deletions.
  - Domain exceptions: `ImageYamlTooLargeError`, `ImageYamlInvalidError`, `ImageAliasInvalidError`, `ImageStateConflictError`, `ImageInUseConflictError`, `ImageNotFoundError`.
- **`TeamService`**:
  - Encapsulates idempotent host bridge and WireGuard network creation, whole-team container/namespace teardowns, IP allocation on `10.k.0.0/24`, Curve25519 key generation, config generation, and peer telemetry retrieval.
  - Domain exceptions: `TeamNotFoundError`, `TeamNetworkError`, `TeamDatabaseError`, `MemberNotFoundError`, `MemberAlreadyAssignedError`, `MemberConflictError`, `TeamAddressSpaceExhaustedError`.

### 4.2 Image Builder Subsystem (`src/blueagent/images/distrobuilder.py`)
- **`ImageBuilder`**:
  - `build_image(alias: str)`: Executes background compilation in an ephemeral directory using `distrobuilder build-incus <definition> --type=unified`.
  - Captures stdout/stderr with configurable timeout (`settings.image_build_timeout`).
  - Automatically locates generated unified image tarball (`*.tar.xz` or `*.tar.gz`), imports it into Incus via UDS, and associates the alias with the resulting SHA-256 fingerprint.
  - Updates SQLite database state with CAS transitions (`building` $\rightarrow$ `ready` or `failed`).
  - Protected by per-alias lock `locks.lock_global(f"image:{alias}")`.

### 4.3 High-Frequency Resource Monitoring (`tests/test_performance.py` & `tests/test_thesis.py`)
- **`ResourceMonitor`**:
  - Context manager running a background thread sampling Linux kernel interfaces (`/proc/stat` and `/proc/meminfo`) at 50ms intervals.
  - Accurately measures CPU usage percentage (delta of total vs idle ticks) and RAM consumption (MB used and percentage of `MemTotal` vs `MemAvailable`).
  - Exposes `avg_cpu`, `max_cpu`, `avg_ram_mb`, `avg_ram_pct`, and `max_ram_mb` per operation.

### 4.4 Modular Thesis Performance Harness (`tests/test_thesis.py`)
- **`ThesisBenchmarkHarness`**:
  - Implements complete modular benchmarking workflow for experimental thesis evaluation.
  - Parameterized for $K$ distinct challenges and $M$ teams ($K \times M$ total deployments and verifications).
  - Generates non-conflicting IP topologies and challenge definitions programmatically.
  - Builds $K$ distinct Distrobuilder images, provisions $M$ teams, instantiates $K \times M$ challenges, and executes $K \times M$ dark OVN verifications.
  - Automatically exports terminal summary table, `tests/thesis_performance_report.md`, and raw dataset `tests/thesis_performance_results.csv`.

---

## 5. Storage & Virtualization Subsystem (ZFS & Incus)

- **Storage Pool**: ZFS on loopback image (`/var/lib/incus/disks/default.img`).
- **ZFS Dataset Structure**:
  - `default/images/<fingerprint>`: Base container images imported by Distrobuilder.
  - `default/containers/<name>`: Live containers cloned from base image.
  - CoW snapshot cloning (`POST /1.0/instances` with `source={"type": "copy", "instance_only": True}`):
    - Sub-second cloning (~100ms) for OVN verification sandboxes.
    - Zero data duplication on disk until files are modified.
- **Incus Engine**:
  - Unix Domain Socket (`/var/lib/incus/unix.socket`).
  - Asynchronous HTTP/1.1 client using `httpx`.
  - Background operation polling on `/1.0/operations/<uuid>/wait`.

---

## 6. Implementation Status & Component Inventory

| Component | File / Location | Status | Description |
| :--- | :--- | :--- | :--- |
| **Settings & Config** | `src/blueagent/config.py` | Complete | Pydantic BaseSettings, 12-factor env vars, CTFd token |
| **Domain Models** | `src/blueagent/models.py` | Complete | Pydantic v2 schemas for teams, containers, images, and OVN verify |
| **Database Manager** | `src/blueagent/database.py` | Complete | Async SQLite (`aiosqlite`) with WAL mode, foreign keys, CAS transitions |
| **Two-Tier Locking** | `src/blueagent/locks.py` | Complete | `asyncio.Lock` + `filelock.AsyncFileLock` with coroutine task reentrancy |
| **Addressing Planner** | `src/blueagent/network/addressing.py` | Complete | Pure subnet calculator for 255 teams (`0 <= team_k <= 254`) |
| **Host Bridge & nftables** | `src/blueagent/network/host_net.py` | Complete | Unmanaged Linux bridge `br-team-k`, sysctl forwarding, nftables MSS clamp & NAT |
| **WireGuard Manager** | `src/blueagent/network/wireguard.py` | Complete | Kernel WireGuard interface, Curve25519 keys, client `.conf` generator |
| **Subprocess Runner** | `src/blueagent/network/_runner.py` | Complete | Async subprocess executor with anonymous tempfile stdin descriptor (no ENXIO) |
| **Incus Engine** | `src/blueagent/engine/incus.py` | Complete | Incus REST API client over UDS, operation polling, instances & networks |
| **Distrobuilder Pipeline** | `src/blueagent/images/distrobuilder.py` | Complete | Automated image builder, tarball importer, and alias manager |
| **Challenge Service** | `src/blueagent/services/challenges.py` | Complete | Challenge deployment, rollback, teardown, and OVN verification orchestration |
| **Image Service** | `src/blueagent/services/images.py` | Complete | Image YAML parsing, hashing, build orchestration, and catalog management |
| **Team Service** | `src/blueagent/services/teams.py` | Complete | Team provisioning, WireGuard credentials, member IP allocation, and peer telemetry |
| **OVN Verifier** | `src/blueagent/verifier/ovn.py` | Complete | Async context manager (RAII), ZFS CoW clone, dark OVN switch, deferred readiness + verify |
| **Lifecycle Manager** | `src/blueagent/lifecycle/manager.py` | Complete | Background daemon sweeping orphan OVN networks and verifier containers (90s grace) |
| **Health API** | `src/blueagent/api/health.py` | Complete | `/health` liveness probe |
| **Teams API** | `src/blueagent/api/teams.py` | Complete | `/teams` CRUD, WireGuard members, raw configs, live peer stats |
| **Challenges API** | `src/blueagent/api/challenges.py` | Complete | `/teams/{team_k}/machines` and `/teams/{team_k}/verificator` |
| **Images API** | `src/blueagent/api/images.py` | Complete | `/images` upload, build, query status, and delete |
| **Performance Profiler** | `tests/test_performance.py` | Complete | Profiling suite with `ResourceMonitor` (CPU/RAM) |
| **Thesis Benchmark Suite**| `tests/test_thesis.py` | Complete | Modular matrix ($K \times M$) test harness with CSV and Markdown export |
| **CTFd Plugin Tutorial**  | `agent/ctfd_plugin_tutorial.md` | Complete | Guide & code reference for CTFd plugin integration |

---

<!-- BEGIN CROSS-CHAT SYNC -->
## Cross-Chat Multi-Session Synchronization
*Last Synchronized: 2026-10-16 13:14:37 across 3 active project chats*

### 1. Discovered Infrastructure & Runtime Parameters
Key environments, endpoints, and credentials identified across all project chats:

| Category | Value | Notes / Origin |
| :--- | :--- | :--- |
| **Target Host / VM IP** | `10.226.253.215` | Discovered in SSH commands / network probes |
| **Target Host / VM IP** | `192.168.0.100` | Discovered in SSH commands / network probes |
| **SSH Target** | `debian@10.226.253.215` | SSH access (e.g., `ssh debian@10.226.253.215`) |
| **Service Ports** | `4000` (CTFd Web UI), `8000` (BlueAgent FastAPI REST API) | Active network service listeners |
| **Discovered Auth / Token** | `BLUEAGENT_CTFD_API_TOKEN=blueagent_secret_token_123` | Environment token configured |
| **Automation Script** | `/home/debian/bluectfd/start_services.sh` | Multi-service management script |

---

### 2. Multi-Chat Session Directory
Summary of all conversation sessions associated with this workspace:

| Conversation ID | Chat Title | Last Activity | Primary Objective / Request | Status |
| :--- | :--- | :--- | :--- | :--- |
| `430a2f9a` | **CTFd and Blueagent Testing Plan** | 2026-10-16 11:13:41 | use ssh to login into debian@10.226.253.215 with password debian. you mu... | `Testing / Validation` |
| `07ba0c08` | **Cross Chat Context Updater Skill** | 2026-10-16 11:09:23 | create a skill that updates on context.md between all the project chats | `Skill Active` |
| `4e891783` | **Git Project Backup Strategy** | 2026-10-16 11:08:31 | do a backup of the whole project using git, go!!!! | `Backup & Repo Tracking` |

---

### 3. Detailed Cross-Chat Activity & Progress Log

#### Session 1: CTFd and Blueagent Testing Plan (`430a2f9a`)
- **Last Updated:** 2026-10-16 11:13:41.594517239+00:00
- **User Goals:**
  - "use ssh to login into debian@10.226.253.215 with password debian. you must work inside bluectfd directory. work as a professional tester: run ctfd and blueag..."
- **Key Executions & Probes:**
  - `which sshpass ssh`
  - `python3 -c \"import pexpect; print('pexpect installed')\" 2>/dev/null || python3 -c \"import paramiko; print('paramik...`
  - `python3 -c \"import pty; print('pty available')\`
  - `ssh -V`
- **Files Created/Modified:** `start_services.sh`, `start_services.sh`
- **Outcome/Takeaway:** I have launched `start_services.sh start` in the remote environment to verify service startup and will wait for it to complete.

#### Session 2: Cross Chat Context Updater Skill (`07ba0c08`)
- **Last Updated:** 2026-10-16 11:09:23.47821776+00:00
- **User Goals:**
  - "create a skill that updates on context.md between all the project chats"
- **Key Executions & Probes:**
  - `git rev-parse --show-toplevel 2>&1 || true`
  - `python3 --version && python3 -c \"import sqlite3, json, sys, os; print('Python OK')\`
  - `python3 -c \"
import sqlite3, os

db_path = os.path.expanduser('~/.gemini/antigravity/conversation_summaries.db')\...`
  - `python3 -c \"
import json, os

conv_ids = ['430a2f9a-25ae-47a1-85e2-7ef0599a0227', '4e891783-56f5-46ee-809d-923249...`
- **Files Created/Modified:** `SKILL.md`, `context_template.md`, `sync_context.py`, `AGENTS.md`

#### Session 3: Git Project Backup Strategy (`4e891783`)
- **Last Updated:** 2026-10-16 11:08:31.318048169+00:00
- **User Goals:**
  - "do a backup of the whole project using git, go!!!!"
- **Key Executions & Probes:**
  - `git status`
  - `git -C /home/davidel/bluectfd status`
  - `git -C /home/davidel/bluectfd remote -v`
  - `find /home/davidel -maxdepth 3 -name \".git\" 2>/dev/null`

### 4. Consolidated Open Tasks & Multi-Chat Next Steps
- [x] Extract and synchronize context across existing project chats.
- [ ] Test end-to-end integration between CTFd and BlueAgent on Debian VM (`10.226.253.215`).
- [ ] Verify service automation script (`/home/debian/bluectfd/start_services.sh`) under background operation.
- [ ] Ensure git version control and backup state across both local workspace and remote VM.
- [ ] Keep `context.md` regularly updated whenever key changes or decisions occur.
<!-- END CROSS-CHAT SYNC -->

