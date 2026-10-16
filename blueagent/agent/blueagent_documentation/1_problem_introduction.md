# BlueAgent v2: Problem & Hybrid Architecture

## 1. Problem & Requirements
Attack-Defense and Patch Management Cyber Ranges require stateful, multi-tier system environments (system containers running full OS, systemd, web, DB) rather than stateless Jeopardy tasks.

Key engineering requirements:
1. **Sub-Second Provisioning:** Rapid deployment vs slow (minutes) VM setup.
2. **Deterministic Topology & Addressing:** Identical private IP topology per team ($k$) with static IPs (`10.k.1.x`), without cross-team collisions.
3. **Non-Destructive, Leak-Free Verification:** Automated exploit/patch checkers must never run on live production (risking crashes, flag tampering, or payload sniffing via `tcpdump`).
4. **Minimal Footprint:** High participant density without full VM virtualization overhead.

---

## 2. Core Architectural Principles

- **Topological Fidelity:** Containers maintain single `eth0`, unchanged IP (`10.k.1.x`), and gateway (`10.k.1.1`) across both live and sandbox. Challenge applications are completely unaware of sandbox rebinding.
- **Dual-Mode Verification:**
  1. *In-Container (Agentless)*: Executed directly inside cloned target instances via hypervisor exec (`incus exec`) for health, configuration, and white-box checks.
  2. *Ephemeral Verifier Container*: Test runner container (`verify-runner-{id}`) attached directly to the sandbox OVN logical switch for black-box network exploits without host routing.
- **Occam’s Razor for Live Production:** Live path uses native kernel primitives (WireGuard + Linux Bridges) for bare-metal speed and transparent observability (`tcpdump`).
- **Cryptokey Routing:** WireGuard peers have `AllowedIPs = 10.k.0.x/32` bound biunivoquely to participant public keys, preventing cross-team IP spoofing.

---

## 3. Technology Stack

| Component | Technology | Operational Role |
| :--- | :--- | :--- |
| **Language & API** | Python 3.12 (Async) + FastAPI | Asynchronous REST control plane and event loop |
| **IPC Transport** | `httpx` (Unix Domain Sockets) | Non-blocking communication via `/var/lib/incus/unix.socket` |
| **Container Engine** | Incus (Primary) / Docker (Secondary) | System containers (Incus/LXC) and application containers (OCI) |
| **SDN Provider** | OVN / Open vSwitch (OVS) | Geneve-encapsulated logical switches and routers |
| **CoW Storage** | ZFS / Btrfs | Block-level Copy-on-Write for sub-second (~100 ms) cloning |
| **L3 VPN** | WireGuard | Isolated participant network access with Cryptokey Routing |

---

## 4. Hybrid Architecture Topology

```
                         +-----------------------------------------------+
                         |                 BLUEAGENT HOST                |
[Participant] ---------> | wg-k (WireGuard L3: 10.k.0.0/24)              |
 (WireGuard VPN)         |   └─> br-team-k (Linux Bridge L2: 10.k.1.1/24)|
                         |        ├─> Web Container (10.k.1.2)           |
                         |        └─> DB Container  (10.k.1.3)           |
                         +-----------------------------------------------+
                                                |
                                 CoW Snapshot (ZFS/Btrfs ~100ms)
                                                v
                         +-----------------------------------------------+
                         |     INCUS + OVN SANDBOX (verify-ovn-<uuid>)   |
                         |  OVN Logical Router & Switch (Geneve Overlay) |
                         |  Assigned Gateway: 10.k.1.1/24 (Parent: incusbr0)
                         |    ├─> Web Clone (10.k.1.2 - eth0)            |
                         |    ├─> DB Clone  (10.k.1.3 - eth0)            |
                         |    └─> Verifier Container (verify-runner)     |
                         +-----------------------------------------------+
```

---

## 5. Architectural Trade-Offs

- **Strengths:**
  - *Topological Fidelity:* Single `eth0`, identical IPs and default gateways.
  - *Zero Side-Channel Leakage:* Payloads never traverse `br-team-k`.
  - *Zero Live Downtime Risk:* Exploits execute strictly on ephemeral CoW clones.
  - *Cluster Ready:* OVN natively supports Geneve tunnels across multi-node Incus clusters.
- **Limitations:**
  - Requires host-level OVS/OVN background daemons (`ovs-vswitchd`, `ovsdb-server`, `ovn-northd`, `ovn-controller`).
  - Dual networking logic (`iproute2`/WireGuard for live; Incus OVN API for verify).
  - Packet inspection inside OVN sandboxes requires OVS tooling (`ovn-trace`, `ovs-dpctl`).