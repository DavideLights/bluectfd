# BlueAgent v2: Architectural Context & Implementation Reference

## 1. System Philosophy & Paradigms

1. **Client-Agnostic Container Management:**
   - BlueAgent is designed to manage container topologies per team on dedicated host bridges without maintaining competition state.
   - Upstream clients (e.g. CTFd) are responsible for high-level challenge state tracking; BlueAgent reports running containers per team.
2. **Identification via `team_k`:**
   - All teams and network namespaces are uniquely identified by the integer index `team_k` (`0 <= team_k <= 254`).
   - The deprecated `team_id` has been completely eliminated from models, database schemas, and endpoints.
   - Team member identifiers are composite strings (`ctfd-{team_k}-{user_id}`).
3. **Service Layer Architecture:**
   - Domain logic and infrastructure orchestration are encapsulated into dedicated service objects in `src/blueagent/services/` (`ChallengeService`, `ImageService`, `TeamService`).
   - API controllers in `src/blueagent/api/` serve as thin routing layers that map HTTP requests to service calls and translate domain exceptions into HTTP status codes.
4. **Hybrid Network Split:**
   - **Production (Live Kernel):** WireGuard (`wg-k`, `10.k.0.0/24`) terminated into unmanaged Linux Bridge (`br-team-k`, gateway `10.k.1.1/24`). Containers run on `eth0` with static IPs (`10.k.1.x`). Maximum I/O, zero SDN overhead, native `tcpdump` sniffing.
   - **Verification (Dark Sandbox):** Ephemeral Incus OVN overlay network (`verify-<team_k>`, parent `incusbr0`). Clones rebound to OVN switch without host routing table conflicts (`ipv4.address: none`).
5. **Topological Fidelity:**
   - Instances maintain a single `eth0`, identical IP (`10.k.1.x`), and gateway (`10.k.1.1`) across live and sandbox. Application code is unaware of sandboxing.
   - Dual network configuration pre-baked into rootfs: `systemd-networkd` (`/etc/systemd/network/10-eth0.network`) and OpenRC/ifupdown (`/etc/network/interfaces`). Runtime static IPs and default routes are applied immediately via `ip addr replace` on container start.
6. **Sub-Second Copy-on-Write (CoW) Duplication:**
   - Incus instances reside on ZFS storage pools (`zfs/containers`). Cloned via block-level snapshots (`POST /1.0/instances` copy) in ~100 ms.
7. **Deferred Readiness & Dual-Mode Verification:**
   - Readiness probes are deferred to the verification phase inside dark OVN clones before running verify checks.
   - *In-Container (Agentless):* Executed inside target clone via `incus exec` (`check.target == "container"`).
   - *Ephemeral Verifier Runner:* Ephemeral container (`verify-runner-<team_k>`, `check.target == "verifier"`) attached directly to OVN switch for black-box network checks.
   - Detailed per-phase latency profiling is recorded in `step_timings` for all deployments and verifications.
8. **Two-Tier Synchronization (Anti-HTTP 409):**
   - Intra-process coroutine lock (`asyncio.Lock`) + inter-process OS lock (`filelock.AsyncFileLock` on `/tmp/blueagent_locks/<name>.lock`) with task-level reentrancy.
9. **Self-Healing State Reconciliation:**
   - Background daemon sweeps orphan `verify-*` containers and OVN networks. Enforces 600s grace period (`resource_grace_period_seconds`) to prevent race conditions on active sandbox executions.
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
│   ├── incus.py           # Concrete IncusEngine (UDS client /var/lib/incus/unix.socket, 1MB streaming tarball import)
│   └── network.py         # Incus OVN/bridge device helpers
├── network/
│   ├── addressing.py      # Pure IP plan generator (plan_for, allocate_member_ip)
│   ├── host_net.py        # HostNetworkManager (Linux bridges, routing, nftables)
│   ├── wireguard.py       # WireGuardManager (wg-k interface, peer allocation, keys)
│   └── _runner.py         # Async subprocess execution wrapper
├── images/
│   ├── __init__.py
│   └── distrobuilder.py   # ImageBuilder pipeline (compiles unified Incus tarballs, context archives, real-time logs)
├── services/
│   ├── __init__.py        # Exported service interfaces
│   ├── challenges.py      # ChallengeService (deploy, rollback, teardown, verify, verifier image pre-check)
│   ├── images.py          # ImageService (YAML/archive parsing, build orchestration, catalog, logs, prune)
│   └── teams.py           # TeamService (bridges, WireGuard, member keys, telemetry, stats)
├── verifier/
│   └── ovn.py             # OVNVerifier async context manager (RAII verification, step timings)
├── lifecycle/
│   └── manager.py         # LifecycleManager (reconciliation loop, 600s grace period)
└── api/
    ├── __init__.py
    ├── health.py          # /health & /health/ready diagnostic probes
    ├── teams.py           # /teams endpoints (PUT/GET/DELETE teams & members with live telemetry)
    ├── challenges.py      # /teams/{team_k}/machines & /teams/{team_k}/verificator (delegates to ChallengeService)
    └── images.py          # /images endpoints (PUT image/archive, GET catalog/logs, DELETE)

agent/
├── context.md             # This comprehensive architecture reference
├── NETWORK_RECAP.md       # Deterministic IP topology & subnet mapping reference
├── TODO.md                # Completed Distrobuilder image management design & improvements
├── blueagent_documentation/
│   ├── 1_problem_introduction.md
│   ├── 2_alternatives.md
│   ├── 3_network_architecture.md
│   ├── 4_asynch_management.md
│   ├── 5_ovn_sandbox_creation.md
│   ├── 6_yaml_configuration.md
│   ├── 7_team_and_wireguard_api.md
│   ├── 8_ctfd_integration.md
│   └── ctfd_plugin_tutorial.md
└── rest-api-doc/          # Simple cave man language REST API reference (20 endpoint files)
```

---

## 3. Comprehensive REST API Reference

All protected endpoints require authentication via CTFd token (`Authorization: Bearer <token>` or `X-CTFd-Token: <token>`).

### 3.1 Health & Diagnostic Probes (`src/blueagent/api/health.py`)
- `GET /health`
  - **Auth**: Public
  - **Description**: Probes service liveness and control plane availability.
  - **Response**: `{"status": "ok"}`
- `GET /health/ready`
  - **Auth**: Public
  - **Description**: Deep readiness probe verifying SQLite database connectivity, Incus container engine, and lifecycle reconciliation worker.
  - **Response**: `{"status": "ready", "database": "connected", "lifecycle_worker": "running"}`

### 3.2 Teams & WireGuard VPN Management (`src/blueagent/api/teams.py`)
- `PUT /teams/{team_k}`
  - **Path Parameter**: `team_k` (int, `0 <= team_k <= 254`)
  - **Payload**: `TeamPutReq(name: str)`
  - **Description**: Idempotently provisions or updates dedicated Linux bridge `br-team-k` (`10.k.1.1/24`), WireGuard interface `wg-k` (UDP port `51820 + team_k`), and adds interface to nftables `@team_links`. Returns `201 Created` if newly created, `200 OK` if already exists.
- `GET /teams`
  - **Description**: Lists all provisioned teams (`List[TeamRecord]`).
- `GET /teams/{team_k}`
  - **Description**: Retrieves single team metadata and network parameters (`TeamRecord`). Returns `404 Not Found` if missing.
- `DELETE /teams/{team_k}`
  - **Description**: Tears down team containers, WireGuard interface, host bridge, clears nftables pairings, and cascades deletion in database. Status: `204 No Content`.
- `PUT /teams/{team_k}/members/{user_id}`
  - **Path Parameters**: `team_k` (int), `user_id` (str or int, player identifier)
  - **Payload**: `TeamMemberPutReq(name: str, public_key: Optional[str])`
  - **Description**: Registers or updates a team member, allocates VPN IP (`10.k.0.2` - `10.k.0.254` with hole-filling), generates Curve25519 keypair if not provided, sets PSK, and updates WireGuard peers. Returns `TeamMemberWG` including rendered `.conf` configuration text. Returns `201 Created` for new member, `200 OK` if existing.
- `GET /teams/{team_k}/members`
  - **Description**: Lists registered peers for team `team_k` with real-time WireGuard telemetry (`List[TeamMemberResp]`). Sensitive private keys and PSKs are omitted. Telemetry includes `online`, `endpoint`, `latest_handshake`, `transfer_rx`, `transfer_tx`, `persistent_keepalive`.
- `GET /teams/{team_k}/members/{user_id}`
  - **Description**: Retrieves metadata and real-time WireGuard telemetry for a specific team member (`TeamMemberResp`).
- `GET /teams/{team_k}/members/{user_id}/config`
  - **Description**: Returns raw WireGuard `.conf` configuration file in `text/plain` format.
- `DELETE /teams/{team_k}/members/{user_id}`
  - **Description**: Removes member from WireGuard peer list and frees allocated VPN IP for reuse. Status: `204 No Content`.

### 3.3 Container Management & Dark Verification (`src/blueagent/api/challenges.py`)
- `POST /teams/{team_k}/machines`
  - **Path Parameter**: `team_k` (int, `0 <= team_k <= 254`)
  - **Payload**: `ChallengeConfig` (`challenge_id: str`, `containers: List[ContainerSpec]`, `readiness_commands: List[CommandSpec]`, `verify_commands: List[CommandSpec]`, `verifier: Optional[VerifierSpec]`, `verify_internet_access: bool = True`)
  - **Description**: Deploys a multi-container challenge to `br-team-k`. Validates that all container images AND optional `verifier.image` exist in the Distrobuilder catalog (`status: ready`). Injects static IP configuration (`10.k.1.{suffix}`) via `systemd-networkd` and `/etc/network/interfaces`, starts containers, applies runtime static IPs, and records containers in SQLite. Readiness checks are deferred to verification. Performs atomic rollback on failure. Returns `DeployResponse` (`ActiveChallengeRecord`) including `step_timings`. Status: `201 Created`.
- `GET /teams/{team_k}/machines`
  - **Description**: Retrieves list of running containers (`List[RunningContainerRecord]`) for team `team_k`. Returns `404 Not Found` if team does not exist.
- `DELETE /teams/{team_k}/machines`
  - **Payload**: Optional `ChallengeConfig`
  - **Description**: Tears down containers for team `team_k`. If config container list is supplied, only tears down specified containers; otherwise tears down all containers for the team. Returns `404 Not Found` if team does not exist. Status: `204 No Content`.
- `POST /teams/{team_k}/verificator`
  - **Path Parameter**: `team_k` (int, `0 <= team_k <= 254`)
  - **Payload**: `ChallengeConfig`
  - **Description**: Executes non-destructive automated verification against dark OVN sandbox clones. Clones containers with ZFS CoW, attaches them to isolated dark switch `verify-{team_k}`, runs readiness checks until healthy, then executes verification commands, and cleans up via RAII. Returns `VerifyResultResp` with comprehensive `step_timings`. Rejects with `409 Conflict` if verification is already in progress for team `team_k`.

### 3.4 Distrobuilder Image Catalog & Build Pipeline (`src/blueagent/api/images.py`)
- `PUT /images/{alias}`
  - **Path Parameter**: `alias` (required URL path parameter, regex `^[a-z0-9][a-z0-9-]{0,31}$`)
  - **Query Parameter**: `force` (optional boolean, default `false`)
  - **Multipart Form Data**: `file` (or `definition`: raw YAML definition up to 256KB **OR** compressed `.tar.gz`/`.zip` build context archive up to 100MB containing `definition.yaml` + auxiliary challenge source files).
  - **Description**: Submits Distrobuilder definition or context archive under the target alias. Uses write-ahead CAS in database. If image is new, failed, hash changed, or `?force=true`, triggers background asynchronous compilation via `ImageBuilder.build_image()` and returns `202 Accepted`. If already ready with identical hash, returns current record idempotently (`200 OK`). Superseded image fingerprints are pruned from Incus upon successful rebuild.
- `GET /images`
  - **Description**: Lists all registered container images, build statuses (`building`, `ready`, `failed`), and fingerprints (`List[ImageRecord]`).
- `GET /images/{alias}`
  - **Description**: Retrieves status and build metadata for a specific image alias (`ImageRecord`). Returns `404 Not Found` if missing.
- `GET /images/{alias}/logs`
  - **Description**: Retrieves real-time or completed build compilation logs (`text/plain`) recorded directly from `distrobuilder build-incus`. Returns `404 Not Found` if alias is not registered.
- `DELETE /images/{alias}`
  - **Description**: Deletes image alias, removes Incus storage image, and unlinks local log and context files if not in use by active challenge containers (enforces SQLite foreign key constraints). Returns `404 Not Found` if missing, or rejects with `409 Conflict` if in use. Status: `204 No Content`.

---

## 4. Subsystems & Services

### 4.1 Service Layer (`src/blueagent/services/`)
- **`ChallengeService`**:
  - Validates container images and `verifier.image` against the Distrobuilder catalog before deploying.
  - Handles container provisioning, static IP injection, atomic rollbacks on failure, container teardowns, and dark OVN sandbox verification.
  - Domain exceptions: `TeamNotFoundError`, `BridgeNotActiveError`, `ImageNotReadyError`, `DeploymentRollbackError`, `DeploymentFailedError`, `VerificationLockedError`, `TeardownError`.
- **`ImageService`**:
  - Validates Distrobuilder YAML definitions or context archives (`.tar.gz`/`.zip`), computes SHA256 hashes, handles alias naming patterns, caches build context, coordinates background builds with `ImageBuilder`, prunes superseded fingerprints, and enforces safe deletions with foreign key checks.
  - Exposes `get_image_logs(alias)` to read build compilation logs from disk.
  - Domain exceptions: `ImageYamlTooLargeError`, `ImageYamlInvalidError`, `ImageAliasInvalidError`, `ImageStateConflictError`, `ImageInUseConflictError`, `ImageNotFoundError`.
- **`TeamService`**:
  - Encapsulates idempotent host bridge and WireGuard network creation, whole-team container/namespace teardowns, IP allocation on `10.k.0.0/24`, Curve25519 key generation, config generation, and real-time peer telemetry retrieval directly from `wg show <iface> dump`.
  - Domain exceptions: `TeamNotFoundError`, `TeamNetworkError`, `TeamDatabaseError`, `MemberNotFoundError`, `MemberAlreadyAssignedError`, `MemberConflictError`, `TeamAddressSpaceExhaustedError`.

### 4.2 Image Builder Subsystem (`src/blueagent/images/distrobuilder.py`)
- **`ImageBuilder`**:
  - `build_image(alias: str)`: Executes background compilation in an ephemeral directory under global per-alias lock `locks.lock_global(f"image:{alias}")`.
  - Unpacks auxiliary challenge build context archive (`{alias}-context.bin`) if provided (`files/`, `overlay/`).
  - Automatically pre-bakes `/etc/systemd/network` and `/etc/network` directories into Distrobuilder YAML post-files actions.
  - Executes `distrobuilder build-incus <definition> --type=unified`, streaming stdout/stderr in real time to `{image_build_dir}/{alias}.log`.
  - Imports generated unified image tarball (`*.tar.xz` or `*.tar.gz`) into Incus via a constant-memory 1 MB streaming generator (`_file_stream`), associating the alias with the resulting SHA-256 fingerprint.
  - Automatically prunes superseded older image fingerprints from Incus storage upon successful rebuild.
  - Updates SQLite database state with CAS transitions (`building` $\rightarrow$ `ready` or `failed`).

### 4.3 Container Engine & Incus Client (`src/blueagent/engine/incus.py`)
- **`IncusEngine`**:
  - Communicates directly with Incus via Unix Domain Socket (`/var/lib/incus/unix.socket`) using `httpx.AsyncClient`.
  - Handles asynchronous operation polling via `/1.0/operations/<uuid>/wait` with configurable timeouts (`incus_operation_timeout`, default 240s).
  - Imports unified tarballs using 1 MB chunks without buffering whole files into RAM.
  - Manages instance creation, device patching, execution, snapshots, and OVN networks.

### 4.4 Lifecycle & Self-Healing Daemon (`src/blueagent/lifecycle/manager.py`)
- **`LifecycleManager`**:
  - Runs a background task every `reconciliation_interval_seconds` (default 120s).
  - Discovers orphan `verify-*` instances and OVN networks.
  - Applies `resource_grace_period_seconds` (default 600s / 10 minutes) based on instance creation timestamp to ensure active verification sessions are never interrupted.
  - Gracefully stops and deletes expired orphan sandbox containers and networks.

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
| **Settings & Config** | `src/blueagent/config.py` | Complete | Pydantic BaseSettings, 12-factor env vars, CTFd token, timeouts (240s incus, 120s interval, 600s grace) |
| **Domain Models** | `src/blueagent/models.py` | Complete | Pydantic v2 schemas for `TeamPutReq`, `TeamMemberPutReq`, `TeamMemberResp`, `PeerTelemetry`, containers, images, and OVN verify |
| **Database Manager** | `src/blueagent/database.py` | Complete | Async SQLite (`aiosqlite`) with WAL mode, foreign keys, CAS transitions, reboot reconciliation |
| **Two-Tier Locking** | `src/blueagent/locks.py` | Complete | `asyncio.Lock` + `filelock.AsyncFileLock` with coroutine task reentrancy |
| **Addressing Planner** | `src/blueagent/network/addressing.py` | Complete | Pure subnet calculator for 255 teams (`0 <= team_k <= 254`) |
| **Host Bridge & nftables** | `src/blueagent/network/host_net.py` | Complete | Unmanaged Linux bridge `br-team-k`, sysctl forwarding, nftables MSS clamp & NAT |
| **WireGuard Manager** | `src/blueagent/network/wireguard.py` | Complete | Kernel WireGuard interface, Curve25519 keys, client `.conf` generator, peer dump stats |
| **Subprocess Runner** | `src/blueagent/network/_runner.py` | Complete | Async subprocess executor with anonymous tempfile stdin descriptor (no ENXIO) |
| **Incus Engine** | `src/blueagent/engine/incus.py` | Complete | Incus REST API client over UDS, operation polling, 1MB streaming image import |
| **Distrobuilder Pipeline** | `src/blueagent/images/distrobuilder.py` | Complete | Automated image builder, context archive unpacking, network pre-baking, real-time log streaming, superseded pruning |
| **Challenge Service** | `src/blueagent/services/challenges.py` | Complete | Challenge deployment, verifier image pre-validation, rollback, teardown, and OVN verification with step timings |
| **Image Service** | `src/blueagent/services/images.py` | Complete | Image YAML/archive parsing, hashing, build orchestration, catalog management, log retrieval, safe deletion |
| **Team Service** | `src/blueagent/services/teams.py` | Complete | Team provisioning (`PUT`), WireGuard credentials, member IP allocation, and peer telemetry in responses |
| **OVN Verifier** | `src/blueagent/verifier/ovn.py` | Complete | Async context manager (RAII), ZFS CoW clone, dark OVN switch, deferred readiness + verify, step timings profiling |
| **Lifecycle Manager** | `src/blueagent/lifecycle/manager.py` | Complete | Background daemon sweeping orphan OVN networks and verifier containers (600s grace period) |
| **Health API** | `src/blueagent/api/health.py` | Complete | `/health` liveness probe and `/health/ready` deep readiness probe |
| **Teams API** | `src/blueagent/api/teams.py` | Complete | `PUT /teams/{team_k}`, member CRUD (`PUT /teams/{team_k}/members/{user_id}`), raw configs, embedded telemetry |
| **Challenges API** | `src/blueagent/api/challenges.py` | Complete | `POST/GET/DELETE /teams/{team_k}/machines` and `POST /teams/{team_k}/verificator` |
| **Images API** | `src/blueagent/api/images.py` | Complete | `PUT /images/{alias}` (YAML or archive), `GET /images`, `GET /images/{alias}`, `GET /images/{alias}/logs`, `DELETE` |
| **Performance Profiler** | `tests/test_performance.py` | Complete | Profiling suite with `ResourceMonitor` (CPU/RAM) |
| **Thesis Benchmark Suite**| `tests/test_thesis.py` | Complete | Modular matrix ($K \times M$) test harness with CSV and Markdown export |
| **REST API Documentation**| `agent/rest-api-doc/` | Complete | 20 endpoint reference files written in simple cave man language |
