# Alternatives & Comparative Analysis

## 1. Context & Competition Constraints
Attack-Defense and Patch Management Cyber Ranges require:
- **Stateful System Containers:** Full OS (systemd, SSH, DB, web) per team with persistent runtime modifications.
- **Deterministic Topology:** Identical private IP subnets (`10.k.1.0/24`) across all teams without collision.
- **Side-Channel-Free Verification:** Automated verification without revealing exploit payloads to live traffic (`tcpdump`) or risking live service crashes.

---

## 2. Alternatives & Limitations

1. **Google kCTF (GKE + nsjail):**
   - Stateless OCI app containers. Cannot handle multi-tier stateful system containers or custom persistent network topologies.
2. **OpenStack / KYPO / SecGen (KVM VMs):**
   - Full VMs guarantee isolation but incur prohibitive provisioning latency (30s – 5 min) and heavy resource footprints (1–4 GB RAM/VM).
3. **Docker Compose / CTFd (Application Containers):**
   - Dynamic port mapping breaks deterministic L2/L3 network topology. Inability to clone block-level state in sub-second time.
4. **Namespace Injection (`nsenter` / Live In-band):**
   - Injects checkers directly into live container network namespaces. Zero isolation: risks crashing live instances and exposes exploit payloads directly to competitors.

---

## 3. Comparative Evaluation Matrix

| Metric | BlueAgent Hybrid (Linux + OVN) | BlueAgent Std (Linux + netns) | Google kCTF (GKE + nsjail) | OpenStack / KYPO (KVM) | Docker Compose (CTFd) | Namespace Injection (`nsenter`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Category** | Attack-Defense / Patch Mgmt | Attack-Defense / Patch Mgmt | Jeopardy / Web / Pwn | Cyber Range / SOC | Jeopardy / Basic Web | Checking Hook |
| **Runtime** | Incus System Container | Incus System Container | OCI App Container | Full VM (KVM) | OCI App Container | Process Injection |
| **Clone Latency** | **~500 ms** (CoW + OVN) | **~100 ms** (CoW + netns) | ~10 ms (nsjail fork) | 30s – 5 min (Nova) | 1s – 5s (docker run) | < 10 ms (`setns`) |
| **Statefulness** | Stateful & Persistent | Stateful & Persistent | Stateless / Ephemeral | Stateful & Persistent | Semi-stateful | Shared Live State |
| **RAM / Team** | Low (~100–300 MB) | Low (~100–300 MB) | Minimal (tmpfs) | High (1–4 GB) | Low | 0 MB (shared) |
| **Test Net Isolation**| **Total (OVN Geneve VPC)**| **Total (Kernel netns)** | Connection-level | Total (Neutron Tenant)| Poor (Port mapping) | None (Live socket) |
| **Side-Channel Shield**| **Yes (Isolated clone)** | **Yes (Isolated clone)** | Yes (Sandbox fork) | Yes (VM clone) | No | **No (Sniffable live)** |
| **Live Crash Risk** | **Zero (Test on clone)** | **Zero (Test on clone)** | Zero | Zero | High | **Critical (Live crash)**|
| **Topology Fidelity**| **Perfect (Single eth0)** | **Perfect (Single eth0)** | N/A (single port) | High | Low | High (same netns) |
| **Host Complexity** | Medium (OVS/OVN daemons) | Low (Pure kernel) | High (K8s cluster) | Extreme (OpenStack) | Low | Minimal |