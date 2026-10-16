# Hybrid Lifecycle & OVN Sandbox Workflow

## 1. End-to-End Operational Lifecycle

| Phase | Endpoint / Trigger | Operations Performed | Output State |
| :--- | :--- | :--- | :--- |
| **1. Team Provisioning** | `PUT /teams/{team_k}` | Allocates `wg-k` (`10.k.0.0/24`), creates `br-team-k` (`10.k.1.1/24`), sets nftables NAT/MSS rules. | Team VPN & network ready. |
| **2. Container Deployment** | `POST /teams/{team_k}/machines` | Launches system containers from Distrobuilder images, binds `eth0` to `br-team-k`, sets static IPs (`10.k.1.x`), records in DB. Readiness checks deferred to verification. | Live container services active. |
| **3. Automated Verification** | `POST /teams/{team_k}/verificator` | Executes non-destructive tests inside ephemeral OVN sandbox (see sequence below). | JSON verification report (`VerifyResultResp`). |
| **4. Teardown / Cleanup** | `DELETE /teams/{team_k}/machines` or `DELETE /teams/{team_k}` | Stops/deletes containers, tears down bridges, removes WireGuard peers. | Resources released. |

---

## 2. Verification Execution Sequence (`OVNVerifier`)

1. **Allocate Dark OVN L2 Switch:**
   - Network name is strictly capped to 11 characters by Incus: `verify-{session_id[:4]}`.
   - Configured as isolated dark L2 switch (`type: ovn`, `ipv4.address: none`, without uplink `network` parameter).
   - Prevents `External subnet overlaps with another network or NIC` conflict with live `br-team-k` (`10.k.1.1/24`).
2. **CoW Snapshot (~100 ms):** Clones all live challenge containers using block-level storage (ZFS/Btrfs).
3. **Re-bind `eth0`:** Overrides clone NIC device to target `verify-{session_id[:4]}` without setting `ipv4.address` in device config (avoiding Incus DHCP-disabled errors).
4. **Boot Sandbox Clones & Apply Static IP:** Clones boot into the isolated OVN switch without network collisions with live production. Static IP is preserved/applied directly inside container namespace.
5. **Optional Verifier Container:** If `verifier` is defined in config, spawns `verify-runner-{session_id[:4]}` on the same dark OVN broadcast domain for black-box network checks.
6. **Run Checkers/Exploits:**
   - *In-Container Checks:* Executed via hypervisor inside target clones (`check.target == "container"`).
   - *Black-Box Checks:* Executed from verifier runner (`check.target == "verifier"`).
   - *Timeout Handling:* Incus operation timeouts are caught and reported as `VerifyResultResp(success=False, passed=False, message="Command timed out...")`.
7. **Atomic Reverse LIFO Teardown:**
   - Stop and delete verifier container.
   - Stop and delete target clones.
   - Delete `verify-{session_id[:4]}` network once all ports are freed.

---

## 3. Key Operational Rules & Fixes

1. **Host Configuration Safety:** Never inject directly into Incus SQLite DB. Use standard CLI/API: `incus config set network.ovn.northbound_connection tcp:127.0.0.1:6641`.
2. **Reliable Async Polling:** Avoid infinite sleep loops and base URL stripping. Always use Incus native long-polling `/operations/{uuid}/wait` with explicit timeouts and status validation.
3. **Dark OVN L2 Switch vs Uplink Routing:** Live bridge `br-team-k` holds `10.k.1.1/24` in the host kernel routing table. Attaching an OVN network with an overlapping subnet to `incusbr0` causes routing rejection. Creating an isolated dark L2 switch (`ipv4.address: none`) bypasses host route table collisions completely.
4. **Incus Network Name Limit:** Incus limits OVN network names to 11 characters max. Verifier session IDs must use at most 4 hex characters (`verify-{session_id[:4]}`).
5. **Concurrency Control:** Concurrent verification requests for the same team are rejected with `HTTP 409 Conflict` using `locks.acquire("verify", str(team_k), timeout=0.01)`.
6. **Dual Verification Support:** Support both in-container execution and out-of-band network checks via ephemeral runner containers.
7. **Universal Engine Interface:** Decouple logic through `ContainerEngine` so both Incus (system containers) and Docker (app containers) can be targeted.
8. **Reconciliation Grace Period:** `LifecycleManager` enforces a 600-second grace period (`resource_grace_period_seconds = 600`) before pruning empty OVN networks or unattached instances to avoid race conditions during provisioning.