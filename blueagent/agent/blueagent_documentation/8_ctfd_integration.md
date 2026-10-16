# CTFd Integration & Interface Reference

## 1. Authentication
All requests from CTFd must include the shared secret token:
```http
Authorization: Bearer <BLUEAGENT_CTFD_API_TOKEN>
```
Alternative header: `X-CTFd-Token: <token>`.

---

## 2. Team & WireGuard VPN Lifecycle

### 2.1 Explicit Team Provisioning
- **Endpoint:** `PUT /teams/{team_k}`
- **Payload:** `{"name": "Team Alpha"}`
- **Action:** Allocates `10.k.0.0/24` VPN subnet, creates Linux bridge `br-team-k` (`10.k.1.1/24`), sets up WireGuard interface `wg-k` (UDP `51820 + team_k`), applies nftables NAT/MSS rules.
- **Response:** `201 Created` or `200 OK` (idempotent).

### 2.2 Member Join / Config Download
- **Endpoint:** `PUT /teams/{team_k}/members/{user_id}`
- **Payload:** `{"name": "user1", "public_key": null}`
- **Action:**
  - Dynamically assigns client IP on `10.k.0.x/24` (host range `.2`–`.254`, capacity 253, automatic hole filling).
  - Generates Curve25519 keypair and WireGuard preshared key (PSK) if client key omitted.
  - Adds peer to kernel `wg-k`.
- **Response:** `201 Created` with `.conf` text in `config_text`. Idempotent on re-click (`200 OK` returns existing config without re-allocating IP).
- **Direct Download Endpoint:** `GET /teams/{team_k}/members/{user_id}/config` (`text/plain`).

### 2.3 Peer Telemetry & Member Audit
- **Endpoint:** `GET /teams/{team_k}/members`
- **Action:** Correlates registered database members with live `wg show <wg-k> dump` metrics (last handshake timestamp, transfer rx/tx bytes, online status).
- **Response:** `200 OK` list of `TeamMemberResp` with embedded `telemetry: PeerTelemetry`.

### 2.4 Team Teardown
- **Endpoint:** `DELETE /teams/{team_k}`
- **Action:** Tears down all running challenge containers, deletes `wg-k` interface, deletes `br-team-k` bridge, clears nftables `@team_links` pairings, cascades member deletion in database.
- **Response:** `204 No Content`.

---

## 3. Challenge Lifecycle & Container Management

### 3.1 Container Deployment (`machines`)
- **Endpoint:** `POST /teams/{team_k}/machines`
- **Payload:** `ChallengeConfig` JSON
  ```json
  {
    "challenge_id": "web-sqli",
    "verify_internet_access": true,
    "verifier": {
      "image": "alpine-verifier",
      "ip_suffix": 250,
      "limits_cpu": "1",
      "limits_memory": "256MB"
    },
    "containers": [
      {
        "name": "web",
        "image": "alpine-3.20-nginx",
        "ip_suffix": 2,
        "limits_cpu": "1",
        "limits_memory": "256MB"
      }
    ],
    "readiness_commands": [
      {
        "target": "container",
        "container_name": "web",
        "cmd": ["nc", "-z", "127.0.0.1", "80"],
        "timeout": 10
      }
    ],
    "verify_commands": [
      {
        "target": "verifier",
        "cmd": ["curl", "-sf", "http://10.k.1.2/health"],
        "timeout": 5
      }
    ]
  }
  ```
- **Action:**
  1. Verifies bridge `br-team-k` exists and is active.
  2. Verifies all container images and `verifier.image` exist in catalog and are marked `ready`.
  3. Provisions containers with `eth0` bound to target team bridge `br-team-k`.
  4. Injects static IP configuration (`10.k.1.x/24`, gateway `10.k.1.1`) via `systemd-networkd` and `/etc/network/interfaces`.
  5. Starts containers and applies immediate runtime static routes via `ip addr replace`.
  6. Records containers in SQLite database. Readiness checks are deferred to verification phase.
  7. On failure: executes atomic rollback (deletes created instances and records).
- **Response:** `201 Created` (`DeployResponse` / `ActiveChallengeRecord`) including `step_timings`.

### 3.2 Automated Verification (`verificator`)
- **Endpoint:** `POST /teams/{team_k}/verificator`
- **Payload:** `ChallengeConfig` JSON
- **Action:**
  1. Enforces lock: rejects concurrent verifications for the same team (`409 Conflict`).
  2. Initializes `OVNVerifier`:
     - Creates isolated dark OVN L2 switch (`verify-{team_k}`).
     - Performs CoW block clones of live challenge containers (<100ms).
     - Binds clone `eth0` to dark OVN switch and sets static IP in container namespace.
     - Optionally spawns ephemeral verifier runner container on dark network.
     - Executes `readiness_commands` in a retry loop until container services are listening.
     - Dispatches `verify_commands` and collects return codes/output.
     - Performs strict reverse LIFO cleanup (stops/deletes verifier container, stops/deletes clones, deletes OVN network).
- **Response:** `200 OK`
  ```json
  {
    "success": true,
    "passed": true,
    "message": "All verification commands passed.",
    "details": {
      "step_timings": {
        "sandbox_setup": { "total_setup_sec": 0.85 },
        "readiness_sec": 0.42,
        "verify_commands_sec": 0.35,
        "cleanup": { "total_cleanup_sec": 0.28 },
        "total_verify_lifecycle_sec": 1.90
      }
    }
  }
  ```
- **Timeouts:** Reported cleanly as `{"success": false, "passed": false, "message": "Command timed out: ..."}`.

### 3.3 Query Active Running Containers
- **Endpoint:** `GET /teams/{team_k}/machines`
- **Response:** `200 OK` list of `RunningContainerRecord` with container instance names, spec names, IP addresses, image aliases, and timestamps.

### 3.4 Container Teardown
- **Endpoint:** `DELETE /teams/{team_k}/machines`
- **Payload:** Optional `ChallengeConfig` JSON (if omitted or empty, tears down all containers for `team_k`)
- **Action:** Stops and deletes containers associated with the team on `br-team-k`, clears database records.
- **Response:** `204 No Content`.

---

## 4. Container Image Catalog API (`/images`)

BlueAgent requires base images before deploying challenges. Images are built with Distrobuilder.

### 4.1 Image Build or Update Submit
- **Endpoint:** `PUT /images/{alias}`
- **Query Parameters:** `force` (optional boolean, default `false`)
- **Header:** `Authorization: Bearer <TOKEN>`
- **Body:** Multipart form data:
  - `file` (or `definition`): Single Distrobuilder YAML file (`definition.yaml`, max 256KB) **OR** a compressed build context archive (`.tar.gz` / `.zip`, max 100MB) containing `definition.yaml` at its root plus auxiliary challenge source files (`files/`, `overlay/`).
- **Workflow:**
  1. Detects archive format (.tar.gz / .zip) or raw UTF-8 YAML; validates `definition.yaml` root dictionary structure.
  2. Computes SHA-256 hash of the submitted payload.
  3. Claims image in database (`status: building`) using CAS logic.
  4. If hash is identical and image is already `ready` (and `force=false`), returns `200 OK` without rebuilding.
  5. If hash changed, previous status was `failed`, or `force=true`, starts background asynchronous compilation:
     - Unpacks context archive into ephemeral build directory if provided.
     - Injects network directories pre-bake (`mkdir -p /etc/systemd/network /etc/network`) into `definition.yaml` post-files actions.
     - Executes `distrobuilder build-incus definition.yaml --type=unified`, streaming stdout/stderr in real time to `{image_build_dir}/{alias}.log`.
     - Streams unified image tarball into Incus in 1 MB chunks to prevent RAM bloat.
     - Sets or updates Incus alias to the new fingerprint. If an older fingerprint was replaced, cleans it up from Incus.
     - Updates database (`status: ready`).
- **Response:**
  - `202 Accepted`: Build started in background.
  - `200 OK`: Image already ready and hash matched (idempotent cache hit).
  - `409 Conflict`: Image currently building.

### 4.2 Query Image Status & Catalog
- **Endpoint:** `GET /images` -> list all registered images.
- **Endpoint:** `GET /images/{alias}` -> inspect single image.
- **Response:** `200 OK`
  ```json
  {
    "alias": "thesis-img-1",
    "status": "ready",
    "fingerprint": "ca00725d6545...",
    "error": null
  }
  ```

### 4.3 Real-Time Build Logs
- **Endpoint:** `GET /images/{alias}/logs`
- **Response:** `200 OK` (`text/plain` stream/tail of distrobuilder compilation output). Useful for debugging build failures, broken debootstrap steps, or package dependency errors.

### 4.4 Delete Image
- **Endpoint:** `DELETE /images/{alias}`
- **Action:** Deletes alias, removes image from Incus pool and database, unlinks local log and context files.
- **Guard:** Rejects with `409 Conflict` if running containers currently reference the image! Returns `404 Not Found` if alias is missing.

---

## 5. CTFd Master Orchestration Guide

CTFd coordinates the competition while BlueAgent executes container and networking tasks:

### 5.1 Team Management Flow
1. **Team Register:**
   - CTFd sends `PUT /teams/{team_k}` with `name`.
   - BlueAgent creates kernel bridge `br-team-k` (`10.k.1.1/24`), WireGuard interface `wg-k` (UDP port `51820 + k`), and nftables NAT + MSS clamp.
2. **Player Join / Get VPN:**
   - Player clicks "Download VPN" button in CTFd UI.
   - CTFd sends `PUT /teams/{team_k}/members/{user_id}` with `name`.
   - BlueAgent allocates VPN IP `10.k.0.x/24` (hole-filling .2 to .254), generates Curve25519 keys, registers peer in `wg-k`.
   - Returns `.conf` text. Player connects to VPN and reaches containers directly on `10.k.1.x`.
   - Repeated requests return the existing configuration without re-allocating IPs.
3. **Telemetry & Activity Check:**
   - CTFd calls `GET /teams/{team_k}/members` or `GET /teams/{team_k}/members/{user_id}`.
   - Inspects bytes rx/tx, last handshake timestamp, and online status directly from the embedded `telemetry` object.
4. **Team Teardown:**
   - CTFd sends `DELETE /teams/{team_k}`.
   - BlueAgent tears down containers, `br-team-k`, `wg-k`, nftables rules, and database records.

### 5.2 Challenge Deployment Flow
1. **Image Pre-Flight Check:**
   - CTFd checks `GET /images/{image_alias}` for all target container images AND `verifier.image` (if defined). If not ready, calls `PUT /images/{image_alias}` and waits for `status: ready`.
2. **Deploy Challenge Containers:**
   - CTFd sends `POST /teams/{team_k}/machines` with `ChallengeConfig`.
   - BlueAgent verifies bridge `br-team-k` is active, checks that all container images and verifier images are ready, creates Incus containers on `br-team-k`, injects static network configs (`systemd-networkd` + `/etc/network/interfaces`), starts containers, and runs immediate `ip addr replace`.
   - Result: Containers live on team LAN `10.k.1.x`.
3. **Challenge Teardown:**
   - CTFd sends `DELETE /teams/{team_k}/machines`.
   - BlueAgent stops and deletes containers and clears database records.

### 5.3 Automated Verification & Scoring Flow
1. **Trigger Verify:**
   - Player submits flag or requests service check.
   - CTFd sends `POST /teams/{team_k}/verificator`.
2. **ZFS CoW Dark Sandbox Execution:**
   - BlueAgent acquires team lock (returns `409 Conflict` if verification is already in progress).
   - Creates dark OVN L2 switch `verify-{team_k}` (`ipv4.address: none`).
   - Clones containers via ZFS block snapshot (<100ms).
   - Rebinds clone `eth0` to dark OVN switch and starts clones.
   - Runs `readiness_commands` until services are listening.
   - Runs `verify_commands` against clones or via ephemeral verifier runner.
   - LIFO cleanup: stops/deletes verifier container, stops/deletes clones, deletes OVN switch.
   - Production containers remain untouched throughout verification.
3. **Process Result:**
   - `passed == true`: Award score / flag valid.
   - `passed == false`: Report broken service / cheat detected.
