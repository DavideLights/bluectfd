# BlueCTFd — Automated Patch-and-Defense Cyber Range Platform

[![OS: Debian 13 (Trixie)](https://img.shields.io/badge/OS-Debian%2013%20%28Trixie%29-A81D33?logo=debian)](https://www.debian.org/)
[![Python: 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Orchestrator: Incus + ZFS + OVN](https://img.shields.io/badge/Runtime-Incus%20%7C%20ZFS%20%7C%20OVN-009688)](https://linuxcontainers.org/incus/)
[![VPN: WireGuard](https://img.shields.io/badge/VPN-WireGuard-88171A?logo=wireguard&logoColor=white)](https://www.wireguard.com/)

**BlueCTFd** is an open-source, bare-metal **Blue-Team & Patch-and-Defense CTF platform** that enables players to connect over dedicated WireGuard VPN tunnels, inspect and live-patch vulnerable multi-container topologies via SSH, and trigger zero-downtime automated SLA & exploit verification directly from **CTFd**.

---

## Architecture Overview

BlueCTFd splits control-plane orchestration and competition management into two tightly integrated components:

- **BlueAgent (`blueagent/`)** — A privileged, asynchronous FastAPI control plane running natively on a Linux host (Debian 13). On the **player hot path**, BlueAgent provisions deterministic per-team unmanaged Linux L2 bridges (`br-team-k`, `10.k.1.0/24`), kernel WireGuard interfaces (`wg-k`, `10.k.0.0/24`), `nftables` forwarding/MSS-clamping rules, and ZFS-backed Incus system containers. On the **verification path**, BlueAgent takes instantaneous (~100 ms) **ZFS Copy-on-Write (`--instance-only`) clones** of a team's live patched containers, attaches them to an isolated **Dark OVN (`Open Virtual Network`) logical switch** preserving identical IP addresses (`10.k.1.x`), runs readiness probes and real proof-of-concept exploits without interrupting player SSH sessions, and tears down the sandbox automatically.
- **CTFd BlueAgent Plugin (`CTFd/CTFd/plugins/blueagent/`)** — A full-stack extension for CTFd introducing the `blueagent_patch` challenge type. It manages deterministic `team_k` (`1..254`) allocation, lazy team & per-user WireGuard VPN provisioning, declarative YAML/JSON challenge specifications with dynamic IP placeholders (`{{ip:<name>}}`, `{{subnet}}`, `{{verifier_ip}}`), Redis-backed verification locks and anti-spam cooldowns, exploit payload sanitization (preventing PoC leakage to players), atomic solve/award recording, and a 5-tab **Administrative Control Center** (`/admin/plugins/blueagent`).

```mermaid
flowchart LR
    subgraph Players["Blue Team Players"]
        WGClient["WireGuard + SSH Client<br/>(10.k.0.2..254)"]
        Browser["Web Browser"]
    end

    subgraph CTFdStack["CTFd Platform (:4000 / :80)"]
        CTFdApp["CTFd + blueagent Plugin<br/>(Flask / SQLAlchemy / Redis)"]
    end

    subgraph Host["Debian 13 Bare-Metal Host"]
        BA["BlueAgent Control Plane<br/>(FastAPI / Uvicorn :8000)"]
        subgraph HotPath["Live Team Environment (Hot Path)"]
            WG["Kernel wg-k + nftables"]
            BR["Linux Bridge br-team-k<br/>(10.k.1.1/24)"]
            CT["Live Incus Containers<br/>(ZFS Rootfs 10.k.1.x)"]
        end
        subgraph ColdPath["Ephemeral Verification Sandbox"]
            OVN["Dark OVN Switch<br/>(verify-ovn-uuid, 10.k.1.1/24)"]
            Clone["ZFS CoW Clones + Verifier<br/>(Same IPs: 10.k.1.x / .250)"]
        end
    end

    Browser <-->|"HTTPS / REST"| CTFdApp
    CTFdApp <-->|"Bearer Auth API"| BA
    WGClient <-->|"UDP 51820+k"| WG
    WG <--> BR <--> CT
    BA -->|"Provision / Exec"| HotPath
    CT -.->|"~100ms ZFS CoW Copy"| Clone
    BA -->|"SLA + Exploit Probes"| ColdPath
    OVN --- Clone
```

### Repository Layout

| Path | Description | Documentation |
| :--- | :--- | :--- |
| [`blueagent/`](blueagent/) | FastAPI bare-metal orchestrator (Incus, ZFS, WireGuard, Linux Bridges, OVN, Distrobuilder) | [`blueagent/README.md`](blueagent/README.md) · [`blueagent/CONTRIBUTING.md`](blueagent/CONTRIBUTING.md) |
| [`CTFd/`](CTFd/) | BlueCTFd web platform fork & [`CTFd/plugins/blueagent/`](CTFd/CTFd/plugins/blueagent/) plugin | [`CTFd/README.md`](CTFd/README.md) · [`CTFd/CONTRIBUTING.md`](CTFd/CONTRIBUTING.md) · [`Plugin README`](CTFd/CTFd/plugins/blueagent/README.md) |
| [`scripts/`](scripts/) | Production host bootstrap (`bootstrap_host.sh`) and end-to-end integration test suites | [`scripts/bootstrap_host.sh`](scripts/bootstrap_host.sh) |
| [`yaml/`](yaml/) | Reference `distrobuilder` container image definitions (`alpine-3.20.yaml`, `ubuntu.yaml`) | — |
| [`start_services.sh`](start_services.sh) | Unified daemon manager (`start`/`stop`/`restart`/`status`/`logs`) for native non-Docker runs | See [Without Docker](#3-starting-ctfd-and-blueagent-without-docker) |

---

## 1. Host Installation & Bootstrap

### System Requirements

- **OS:** Debian 13 (*Trixie*) `x86_64` (bare-metal server or nested-capable KVM/VirtualBox VM)
- **Resources:** Minimum 4 vCPU, 8 GB RAM, 30+ GB disk (8+ vCPU and 16+ GB RAM recommended for 20+ concurrent teams)
- **Privileges:** `root` or passwordless `sudo` (required by BlueAgent to manage kernel `wg-k` interfaces, `br-team-k` bridges, `nftables`, `/var/lib/incus/unix.socket`, and `distrobuilder`)
- **APT Repositories:** Ensure `contrib` is enabled in `/etc/apt/sources.list` (or `/etc/apt/sources.list.d/debian.sources`) so `zfsutils-linux` and `zfs-dkms` can be installed.

---

### Option A: Automated Host Bootstrap (Recommended)

Clone the repository and execute the idempotent production bootstrap script:

```bash
git clone <YOUR_REPO_URL> bluectfd
cd bluectfd

# Run full host installation and configuration (packages, sysctl, OVS/OVN, ZFS, Incus)
sudo ./scripts/bootstrap_host.sh

# Verify host readiness at any time without modifying state
sudo ./scripts/bootstrap_host.sh --check
```

---

### Option B: Manual Step-by-Step Host Bootstrap

If you prefer to provision the Debian 13 host manually or integrate steps into Ansible/Terraform:

#### Step 1 — Install System, Networking, ZFS, and Image-Builder Packages

```bash
sudo apt-get update
sudo apt-get install -y \
    openvswitch-switch ovn-central ovn-host \
    incus wireguard wireguard-tools iproute2 nftables \
    "linux-headers-$(uname -r)" \
    distrobuilder debootstrap squashfs-tools gpg \
    curl git jq python3 python3-venv python3-pip

# Install ZFS kernel module & userspace tools (requires Debian contrib archive)
sudo apt-get install -y zfsutils-linux
sudo modprobe wireguard
sudo modprobe zfs
```

#### Step 2 — Enable Persistent IPv4 & IPv6 Kernel Forwarding

```bash
cat << 'EOF' | sudo tee /etc/sysctl.d/99-blueagent.conf
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sudo sysctl --system
```

#### Step 3 — Configure Open vSwitch (OVS) & OVN Control Plane

```bash
sudo systemctl enable --now openvswitch-switch ovn-central ovn-host

sudo ovs-vsctl set open_vswitch . \
    external_ids:ovn-remote=unix:/run/ovn/ovnsb_db.sock \
    external_ids:ovn-encap-type=geneve \
    external_ids:ovn-encap-ip=127.0.0.1

sudo ovn-nbctl set-connection ptcp:6641:127.0.0.1
```

#### Step 4 — Initialize Incus, ZFS Pool (`blueagent-zfs`), and Underlay Bridge (`incusbr0`)

```bash
sudo systemctl enable --now incus.socket incus.service
sudo incus admin init --auto

# Connect Incus to the local OVN Northbound database
sudo incus config set network.ovn.northbound_connection tcp:127.0.0.1:6641

# Create ZFS Copy-on-Write storage pool and bind it to the default profile
sudo incus storage show blueagent-zfs &>/dev/null || sudo incus storage create blueagent-zfs zfs
sudo incus profile device set default root pool=blueagent-zfs 2>/dev/null \
    || sudo incus profile device add default root disk path=/ pool=blueagent-zfs

# Configure incusbr0 with non-overlapping DHCP and OVN router uplink ranges
sudo incus network set incusbr0 \
    ipv4.address=172.16.0.1/24 \
    ipv4.nat=true \
    ipv4.dhcp.ranges=172.16.0.2-172.16.0.99 \
    ipv4.ovn.ranges=172.16.0.100-172.16.0.199
```

#### Step 5 — Create Runtime Directories & Install BlueAgent Python Environment

```bash
sudo mkdir -p /var/lib/blueagent/builds /etc/wireguard /tmp/blueagent_locks
sudo chmod 700 /etc/wireguard

cd blueagent
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[dev]"
cd ..
```

---

## 2. Starting CTFd and BlueAgent Using Docker

> [!IMPORTANT]
> **Why Hybrid Architecture (Native BlueAgent + Dockerized CTFd)?**
> In production, **CTFd, MariaDB 10.11, Redis 4, and Nginx** run isolated inside Docker Compose, whereas **BlueAgent intentionally runs natively on the Debian host**. BlueAgent directly creates host kernel WireGuard interfaces (`wg-k`), unmanaged Linux bridges (`br-team-k`), `nftables` rules (`inet blueagent`), chroot image builds (`distrobuilder`), and communicates over `/var/lib/incus/unix.socket`. Running BlueAgent bare-metal avoids nested `--privileged` / `--net=host` container namespace hazards while CTFd reaches it cleanly over the Docker bridge gateway (`http://172.17.0.1:8000`).

### Step 1 — Ensure Docker Does Not Block WireGuard Forwarding

Installing Docker Engine sets the kernel `iptables` `FORWARD` chain policy to `DROP`, which will silently drop player packets routed between `wg-k` and `br-team-k` unless explicitly permitted in `DOCKER-USER`:

```bash
sudo iptables -C DOCKER-USER -j ACCEPT 2>/dev/null || sudo iptables -I DOCKER-USER -j ACCEPT
```

### Step 2 — Start BlueAgent on the Host

Choose a strong shared API token (`BLUEAGENT_CTFD_API_TOKEN`) and specify the host's reachable IP or DNS name (`BLUEAGENT_WG_PUBLIC_ENDPOINT`) that will be embedded in downloaded player WireGuard `.conf` files:

```bash
cd blueagent
sudo env \
    BLUEAGENT_CTFD_API_TOKEN="blueagent_secret_token_123" \
    BLUEAGENT_HOST="0.0.0.0" \
    BLUEAGENT_PORT="8000" \
    BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
    .venv/bin/uvicorn blueagent.main:app --host 0.0.0.0 --port 8000
```

### Step 3 — Start the CTFd Stack via Docker Compose

In a second terminal, launch the Dockerized CTFd stack (`CTFd/docker-compose.yml` + `CTFd/docker-compose.override.yml` automatically merge):

```bash
cd CTFd

# Ensure BLUEAGENT_TOKEN in docker-compose.override.yml matches BLUEAGENT_CTFD_API_TOKEN
docker compose up -d --build
```

What this starts:
- **`ctfd`** — Exposed on `http://<HOST_IP>:4000` (configured with `BLUEAGENT_URL=http://172.17.0.1:8000` and live-mounted `CTFd/plugins/blueagent`)
- **`nginx`** — Reverse proxy exposed on `http://<HOST_IP>:80`
- **`db`** — MariaDB `10.11` persistent relational database (`.data/mysql`)
- **`cache`** — Redis `4` distributed lock, verification cooldown, and session store (`.data/redis`)

### Step 4 — Verify Connectivity & Manage Stack

```bash
# Check BlueAgent readiness (Incus + SQLite)
curl -s http://127.0.0.1:8000/health/ready | jq .

# Follow CTFd container logs
docker compose -f CTFd/docker-compose.yml -f CTFd/docker-compose.override.yml logs -f ctfd

# Stop the Dockerized CTFd stack
cd CTFd && docker compose down
```

---

## 3. Starting CTFd and BlueAgent Without Docker

For local development, single-VM testing, or lightweight deployments without Docker Engine, both BlueAgent and CTFd can run natively using Python virtual environments (SQLite + local Flask/Waitress server).

### Prerequisite — Prepare CTFd Virtual Environment (One-Time)

```bash
cd CTFd
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cd ..
```

### Option A: Automated Background Daemon Script (`start_services.sh`)

The root [`start_services.sh`](start_services.sh) helper manages PID files (`run/`), log files (`logs/`), Docker forwarding rules, and HTTP readiness checks automatically:

```bash
# Start both BlueAgent (:8000) and CTFd (:4000) in the background
BLUEAGENT_TOKEN="blueagent_secret_token_123" \
BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
./start_services.sh start

# Inspect live service health and PIDs
./start_services.sh status

# Tail recent BlueAgent and CTFd logs
./start_services.sh logs

# Restart or stop both services cleanly
./start_services.sh restart
./start_services.sh stop
```

### Option B: Manual Foreground Execution (Two Terminals)

**Terminal 1 — Start BlueAgent (`:8000`):**

```bash
cd blueagent
sudo env \
    BLUEAGENT_CTFD_API_TOKEN="blueagent_secret_token_123" \
    BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
    .venv/bin/uvicorn blueagent.main:app --host 0.0.0.0 --port 8000
```

**Terminal 2 — Start CTFd (`:4000`):**

```bash
cd CTFd
env \
    BLUEAGENT_URL="http://127.0.0.1:8000" \
    BLUEAGENT_TOKEN="blueagent_secret_token_123" \
    PYTHONPATH="$(pwd)" \
    .venv/bin/python serve.py --host 0.0.0.0 --port 4000
```

Then open **`http://<HOST_IP>:4000`** to complete the initial CTFd setup wizard (select **Teams Mode** for multi-player blue-team competitions) and visit **`http://<HOST_IP>:4000/admin/plugins/blueagent`** for the BlueAgent Control Center.

---

## 4. Running BlueAgent with Multiple Worker Processes

By default, Uvicorn launches a single asynchronous event-loop process (`--workers 1`). You can scale BlueAgent across multiple OS worker processes using Uvicorn's `--workers <N>` flag:

### Single Process (Default — Development & Small Events)

```bash
cd blueagent
sudo env \
    BLUEAGENT_CTFD_API_TOKEN="blueagent_secret_token_123" \
    BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
    .venv/bin/uvicorn blueagent.main:app \
    --host 0.0.0.0 --port 8000 --workers 1
```

### Multi-Process Mode (Fixed Worker Count, e.g., 4 Workers)

```bash
cd blueagent
sudo env \
    BLUEAGENT_CTFD_API_TOKEN="blueagent_secret_token_123" \
    BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
    .venv/bin/uvicorn blueagent.main:app \
    --host 0.0.0.0 --port 8000 --workers 4
```

### CPU-Scaled Worker Count

```bash
cd blueagent
sudo env \
    BLUEAGENT_CTFD_API_TOKEN="blueagent_secret_token_123" \
    BLUEAGENT_WG_PUBLIC_ENDPOINT="172.20.10.2" \
    .venv/bin/uvicorn blueagent.main:app \
    --host 0.0.0.0 --port 8000 --workers "$(nproc)"
```

### Multi-Process Architecture & Sizing Guidelines

1. **Cross-Process Mutual Exclusion (`LockRegistry`)**: BlueAgent coordinates critical sections using OS-level `fcntl`/`flock` file locks via `filelock.AsyncFileLock` under `BLUEAGENT_LOCK_DIRECTORY` (`/tmp/blueagent_locks`). Concurrent requests routed to *different* Uvicorn worker processes safely serialize per-team mutations (`team_{k}.lock`), OVN verifications (`verify_team_{k}.lock`), and global IP allocation (`global.lock`), immediately returning `HTTP 409 Conflict` when a conflicting verification is already active.
2. **SQLite WAL Concurrency**: `DatabaseManager` initializes SQLite with `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000`, allowing concurrent multi-worker reads and serialized writes on `blueagent.db`.
3. **Recommended Worker Count (`2`–`4`)**: Because heavy operations (ZFS CoW cloning, Incus REST over UDS, OVN switch creation, `distrobuilder`) execute asynchronously in external system daemons and non-blocking subprocesses, **2 to 4 workers** saturate HTTP/JSON serialization without excessive duplicate background `LifecycleManager` sweeps (each worker runs its own FastAPI `lifespan` reconciliation loop guarded by file locks).
4. **Incompatibility with `--reload`**: Uvicorn does not support combining `--workers <N>` (`N > 1`) with `--reload` (`BLUEAGENT_DEBUG=true`). Use `--reload` with a single worker during local code editing.

---

## 5. Quick Verification & Test Suites

```bash
# 1. Run BlueAgent fast mocked unit test suite (no Incus/root required — 111 tests)
cd blueagent && .venv/bin/pytest tests/test_*.py --ignore=tests/test_performance.py --ignore=tests/test_thesis.py -v

# 2. Run CTFd BlueAgent plugin test suite (28 tests)
cd ../CTFd && .venv/bin/pytest CTFd/plugins/blueagent/tests -v

# 3. Build a reference Alpine 3.20 base image on a bootstrapped host
curl -X PUT http://127.0.0.1:8000/images/alpine-base \
  -H "Authorization: Bearer blueagent_secret_token_123" \
  -F "file=@yaml/alpine-3.20.yaml"
```

---

## Documentation & Contributing

- **Monorepo Contribution Guide:** [`CONTRIBUTING.md`](CONTRIBUTING.md)
- **BlueAgent Architecture & Developer Guide:** [`blueagent/README.md`](blueagent/README.md) · [`blueagent/CONTRIBUTING.md`](blueagent/CONTRIBUTING.md)
- **BlueCTFd & Plugin Developer Guide:** [`CTFd/README.md`](CTFd/README.md) · [`CTFd/CONTRIBUTING.md`](CTFd/CONTRIBUTING.md)
- **Plugin Quick Reference:** [`CTFd/CTFd/plugins/blueagent/README.md`](CTFd/CTFd/plugins/blueagent/README.md)
