# Team Registration, Multi-Member WireGuard & CTFd Integration

## 1. Overview
The Team and WireGuard subsystem manages multi-member participant access to competition Cyber Ranges. It exposes REST APIs consumed by upstream competition platforms (such as CTFd) with Bearer token authentication.

---

## 2. API Endpoints

### 2.1 Team Management (`/teams`)

| Method | Endpoint | Description | Status Codes |
|---|---|---|---|
| `PUT` | `/teams/{team_k}` | Provisions Linux bridge (`br-team-k`) and WireGuard interface (`wg-k`) for `team_k`. Idempotent. | `201 Created` (new), `200 OK` (exists), `409 Conflict`, `503 Service Unavailable` |
| `GET` | `/teams` | Lists all registered teams ordered by `team_k`. | `200 OK` |
| `GET` | `/teams/{team_k}` | Retrieves metadata for a specific team. | `200 OK`, `404 Not Found` |
| `DELETE` | `/teams/{team_k}` | Tears down all running containers for `team_k`, `wg-k`, `br-team-k`, removes nftables pairings, and cascades deletion in SQLite. Idempotent. | `204 No Content`, `503 Service Unavailable` |

### 2.2 WireGuard Peer Management (`/teams/{team_k}/members`)

| Method | Endpoint | Description | Status Codes |
|---|---|---|---|
| `PUT` | `/teams/{team_k}/members/{user_id}` | Allocates client IP on `10.k.0.0/24` (filling holes), generates Curve25519 keypair + PSK, configures kernel peer, and returns `.conf`. Idempotent on re-clicks. | `201 Created` (new), `200 OK` (re-click), `404 Not Found`, `409 Conflict`, `422 Validation Error` |
| `GET` | `/teams/{team_k}/members` | Lists team members with real-time WireGuard telemetry (online status, endpoint, handshake, bytes rx/tx) with private keys masked. | `200 OK`, `404 Not Found` |
| `GET` | `/teams/{team_k}/members/{user_id}` | Retrieves metadata and real-time WireGuard telemetry for an individual member. | `200 OK`, `404 Not Found` |
| `GET` | `/teams/{team_k}/members/{user_id}/config` | Downloads raw WireGuard `.conf` configuration text. | `200 OK` (`text/plain`), `404 Not Found` |
| `DELETE` | `/teams/{team_k}/members/{user_id}` | Removes peer from kernel `wg-k` and deletes DB record, freeing the client IP. | `204 No Content`, `404 Not Found` |

---

## 3. Key Design Patterns

### 3.1 Idempotency & Re-Click Safety
- When a user re-clicks the download button in CTFd, `PUT /teams/{team_k}/members/{user_id}` looks up the existing member (`member_id = "ctfd-{team_k}-{user_id}"`).
- Instead of allocating a new IP or generating new keys, it re-renders the exact same configuration from the securely stored private key and PSK in SQLite.
- Returns `HTTP 200 OK` without wasting IPs or breaking active connections.

### 3.2 Dynamic IP Allocation with Hole-Filling
- Member IPs are dynamically assigned on `10.k.0.0/24` within the range `.2` to `.254` (`.1` is reserved for the host gateway, `.255` is broadcast).
- Maximum team capacity is **253 members**.
- If member `.3` leaves (is deleted), their IP is immediately recycled and assigned to the next joining member.

### 3.3 Kernel Interface Teardown & Lifecycle
- `DELETE /teams/{team_k}` (orchestrated by `TeamService.teardown_team`):
  1. Deletes and stops all running challenge containers for `team_k`.
  2. `wg.teardown_team_interface(team_k)`: Brings down `wg-k`, deletes device from kernel, unlinks private key.
  3. `host_net.teardown_team_bridge(team_k)`: Unregisters `(wg-k, br-team-k)` pair from nftables `team_links` set, brings down bridge, deletes device.
  4. `db.delete_team(team_k)`: Cascades deletion to all team members in SQLite.
- Both `wg-k` and `br-team-k` are completely eliminated from the kernel.

### 3.4 Boot Re-Synchronization
- On FastAPI application startup (`lifespan`), BlueAgent:
  1. Applies base nftables rules and MSS clamping.
  2. Reads all teams from SQLite.
  3. Re-creates bridges and WireGuard interfaces.
  4. Restores all peers and cryptographic bindings from SQLite.

### 3.5 Real-Time Telemetry Integration
- Instead of maintaining a separate `/peers` query endpoint, `GET /teams/{team_k}/members` and `GET /teams/{team_k}/members/{user_id}` directly correlate registered database members with live `wg show <iface> dump` metrics.
- Each member response includes a `telemetry` payload with:
  - `online`: boolean indicating active handshake within the last 180 seconds.
  - `endpoint`: client IP and ephemeral port.
  - `latest_handshake`: timestamp in epoch seconds.
  - `transfer_rx` / `transfer_tx`: total bytes transferred.

### 3.6 CTFd Authentication
- Upstream requests from CTFd authenticate via `require_ctfd_token` dependency.
- Accepts secret token either through `Authorization: Bearer <token>` or `X-CTFd-Token: <token>`.
- Token configured via `BLUEAGENT_CTFD_API_TOKEN` environment variable.

### 3.7 Subprocess Stdin Pipe Handling
- In Linux, invoking `wg set ... private-key /dev/stdin` with standard `asyncio.subprocess.PIPE` can trigger `ENXIO: No such device or address` if `/proc/self/fd/0` is opened across process boundaries before flush.
- `SubprocessRunner` writes input bytes into an anonymous unlinked `tempfile.TemporaryFile()` and passes its physical file descriptor directly to child process `stdin`.

---

## 4. Live System Verification

The Team and WireGuard API was verified against a live Linux kernel:

1. **Team 0 & User 0 Creation:**
   - Issued `PUT /teams/0` with `name="Team 0"`.
   - Verified kernel bridge creation: `br-team-0` on `10.0.1.1/24`.
   - Issued `PUT /teams/0/members/0` with `name="User 0"`.
   - Verified WireGuard interface: `wg-0` listening on UDP `51820`.
   - Downloaded generated client configuration file `team0_user0.conf` (`10.0.0.2/32`).
2. **Kernel Teardown & Destruction:**
   - Issued `DELETE /teams/0` with CTFd bearer token.
   - Verified `HTTP 204 No Content`.
   - Verified interface destruction: `ip link show wg-0` and `ip link show br-team-0` confirmed completely absent from kernel.
