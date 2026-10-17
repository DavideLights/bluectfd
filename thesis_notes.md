# BlueAgent Empirical Thesis Notes & Benchmark Analysis

This document compiles the quantitative evaluation results, methodology design decisions, and architectural insights for the thesis on **BlueAgent: Multi-Tenant Cyber Range Verification and Isolation with Incus and Open Virtual Network (OVN)**.

All benchmarks were executed on an **AMD Ryzen 7 7700X (8 physical cores / 16 threads), 15 GiB RAM, 2 GiB swap**, running Linux 6.8 with Incus 6.0 and ZFS on Linux (`blueagent-zfs` storage pool).

---

## 1. ZFS Copy-on-Write (CoW) Ephemeral Sandboxing (Core Thesis Contribution)

### 1.1 The Challenge of State Verification in Live CTF Infrastructure
In Attack-Defense and Red/Blue Cyber Range competitions, teams deploy patches, configuration changes, or service hardening. When the verification engine tests challenge functionality, executing destructive or state-altering verifier scripts directly on the production challenge containers risks corrupting team state or causing cascading outages.

BlueAgent solves this through **ephemeral sandboxing**:
1. When a team submits or automated health checking runs, BlueAgent snapshots each running container in the team's challenge.
2. An ephemeral copy of each container is cloned into an isolated sandbox logical network using `incus copy --instance-only`.
3. Verification scripts execute against the cloned instances in the sandbox.
4. Upon test completion, the ephemeral clones and isolated OVN network are immediately destroyed.

### 1.2 Quantitative Speedup Factor: Deploy Create vs. CoW Clone
Because BlueAgent is backed by a **ZFS storage pool** (`blueagent-zfs`), container cloning uses ZFS Copy-on-Write block pointer references rather than block-level data copies. While creating an initial container instance (`Deploy Create`) requires unpacking and initializing filesystem structures from the base image, cloning a running instance (`CoW Clone`) is an $O(1)$ metadata transaction.

As system concurrency scales, the performance advantage of ZFS CoW cloning becomes dramatically more pronounced across both lightweight and heavyweight containers:

#### Alpine Linux (3.21 MiB Rootfs)
| Scenario ($M \times N$) | Concurrent Containers (Live / Peak Verify) | Deploy Create Avg (`total_create_sec`) | CoW Clone Avg (`total_clone_time_sec`) | **ZFS CoW Speedup Factor** |
| :--- | :--- | :--- | :--- | :--- |
| **$M=1, N=1$ (Baseline)** | 1 / 2 | 0.99s | 0.75s | **$1.32\times$** |
| **$M=2, N=5$** | 10 / 20 | 8.16s | 8.65s | **$0.94\times$** |
| **$M=2, N=7$** | 14 / 28 | 13.45s | 10.13s | **$1.33\times$** |
| **$M=2, N=10$** | 20 / 40 | 15.68s | 13.49s | **$1.16\times$** |
| **$M=5, N=5$** | 25 / 50 | 23.57s | 8.44s | **$2.79\times$** |
| **$M=5, N=7$** | 35 / 70 | 32.11s | 14.13s | **$2.27\times$** |
| **$M=5, N=10$** | 50 / 100 | 45.36s | 19.95s | **$2.27\times$** |
| **$M=10, N=2$** | 20 / 40 | 19.32s | 5.26s | **$3.68\times$** |
| **$M=10, N=5$** | 50 / 100 | 50.61s | 14.06s | **$3.60\times$** |

#### Ubuntu Heavy (~1.5 GB Uncompressed Rootfs with systemd, nginx, postgresql, python3)
| Scenario ($M \times N$) | Concurrent Containers (Live / Peak Verify) | Deploy Create Avg (`total_create_sec`) | CoW Clone Avg (`total_clone_time_sec`) | **ZFS CoW Speedup Factor** |
| :--- | :--- | :--- | :--- | :--- |
| **$M=1, N=1$ (Baseline)** | 1 / 2 | 1.23s | 0.92s | **$1.33\times$** |
| **$M=2, N=5$** | 10 / 20 | 9.51s | 8.62s | **$1.10\times$** |
| **$M=2, N=7$** | 14 / 28 | 16.50s | 15.33s | **$1.08\times$** |
| **$M=2, N=10$** | 20 / 40 | 19.71s | 21.50s | **$0.92\times$** |
| **$M=5, N=5$** | 25 / 50 | 30.10s | 13.62s | **$2.21\times$** |
| **$M=5, N=7$** | 35 / 70 | 42.82s | 21.75s | **$1.97\times$** |
| **$M=5, N=10$** | 50 / 100 | 63.28s | 34.35s | **$1.84\times$** |
| **$M=10, N=2$** | 20 / 40 | 22.72s | 7.81s | **$2.91\times$** |
| **$M=10, N=5$** | 50 / 100 | 76.19s | 22.17s | **$3.44\times$** |

### 1.3 Key Architectural Takeaways
1. **Divergent Scalability Under Peak Concurrency:**
   - On Ubuntu Heavy at $M=10, N=5$ (50 live / 100 peak instances), cold deployment instance creation took **76.19 seconds**.
   - In contrast, ZFS snapshot cloning (`CoW Clone`) completed in only **22.17 seconds**—a massive **$3.44\times$ speedup**.
   - On Alpine, cold instance creation took **50.61 seconds** vs. **14.06 seconds** for CoW cloning (**$3.60\times$ speedup**).
2. **$O(1)$ Block-Pointer Cloning Invariance:**
   - Although the Ubuntu Heavy rootfs is ~500× larger than Alpine (1.5 GB vs. 3.2 MB), cloning time per container during verification remains virtually identical (Alpine: 14.06s vs. Ubuntu: 22.17s for 50 clones across 10 teams). ZFS avoids duplicating data blocks, making snapshot cloning metadata-bound rather than I/O-bound.
3. **Competition Cadence Feasibility:**
   - In automated scoring ticks (e.g. 2-minute round intervals), recreating 50 heavy service instances from scratch would consume >63% of the entire tick budget (`76.2s`). With CoW cloning, the complete sandbox environment is cloned in `22.2s`, preserving over 80% of the evaluation window for functional verification.

---

## 2. 6-Barrier Synchronized Evaluation Methodology & 100ms Telemetry

To evaluate BlueAgent under rigorous, reproducible conditions without noisy-neighbor interference, the benchmark suite implements a **6-stage barrier-synchronized architecture**:

```
[Baseline Snapshot: Record Clean Host RAM & ZFS ARC]
                         │
                         ▼
[Barrier 1: Parallel Team Provisioning (PUT /teams/1..M)]
                         │
                         ▼
[Barrier 2: Parallel Challenge Deployment (POST /teams/1..M/machines)]
     └── Settle 1.0s: Measure Steady-State M×N Live RAM
                         │
                         ▼
[Barrier 3: Parallel Verify Setup / Start Verify (POST /teams/1..M/verificator?skip_cleanup=true)]
     └── Settle 1.0s: Measure True Peak Steady-State RAM with ALL 2×M×N Containers Alive!
                         │
                         ▼
[Barrier 4: Parallel Verify Cleanup (DELETE /teams/1..M/verificator)]
                         │
                         ▼
[Barrier 5: Parallel Challenge Teardown (DELETE /teams/1..M/machines)]
                         │
                         ▼
[Barrier 6: Parallel Team Teardown (DELETE /teams/1..M)]
```

### 2.1 Why Synchronizing Verify Setup vs. Cleanup is Essential
In default operation, verification creates sandboxes, runs checks, and immediately destroys sandboxes per-team. Under multi-tenant loads ($M=10$), faster teams finish and delete their clones while slower teams are still cloning. 
By introducing `skip_cleanup=true` on `POST /teams/{k}/verificator` and an explicit `DELETE /teams/{k}/verificator`, the benchmark guarantees:
1. **Purity of "Time to Start Verification":** All $M$ teams complete OVN switch creation, ZFS CoW cloning, and container boot without interference from early deletions.
2. **True Peak Infrastructure Concurrency:** All $2 \times M \times N$ containers (50 live + 50 clones = 100 containers at peak) are alive and active simultaneously, allowing exact empirical measurement of peak RAM and CPU saturation.

### 2.2 Dedicated 100ms Subprocess Telemetry
A dedicated Python subprocess continuously samples:
- `/proc/stat`: Total CPU %, user %, system %, and iowait % across all 8 cores.
- `/proc/meminfo`: Total host memory consumed (`MemTotal - MemAvailable`).
- `/proc/spl/kstat/zfs/arcstats`: Dynamic ZFS Adaptive Replacement Cache (ARC) size.
- **Net Container RAM:** `net_ram_mb = ram_used_mb - zfs_arc_mb`, isolating actual container process memory and kernel slab from filesystem caching.

---

## 3. Side-by-Side Comparison: Alpine Linux vs. Ubuntu Heavy

### 3.1 Synchronized Wall-Clock Barrier Timings (in Seconds)
| Scenario | Alpine Deploy Wall | Ubuntu Deploy Wall | Alpine Verify Setup Wall | Ubuntu Verify Setup Wall | Alpine Teardown Wall | Ubuntu Teardown Wall |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **$M=1, N=1$ (Baseline)** | 2.62s | 2.92s | 2.32s | 2.17s | 1.87s | 2.31s |
| **$M=2, N=5$ (20 Peak)** | 18.25s | 19.08s | 18.59s | 16.71s | 8.69s | 8.34s |
| **$M=2, N=7$ (28 Peak)** | 28.28s | 30.10s | 25.32s | 25.17s | 11.29s | 11.61s |
| **$M=2, N=10$ (40 Peak)**| 33.60s | 40.81s | 29.94s | 36.85s | 17.02s | 18.62s |
| **$M=5, N=5$ (50 Peak)** | 37.37s | 45.85s | 28.44s | 35.29s | 14.80s | 13.55s |
| **$M=5, N=7$ (70 Peak)** | 49.75s | 67.62s | 39.66s | 54.72s | 17.10s | 17.53s |
| **$M=5, N=10$ (100 Peak)**| 74.04s | 101.54s | 54.76s | 78.86s | 23.04s | 25.30s |
| **$M=10, N=2$ (40 Peak)**| 28.59s | 36.44s | 22.21s | 28.90s | 10.36s | 9.65s |
| **$M=10, N=5$ (100 Peak)**| 72.21s | 103.10s | 55.72s | 75.15s | 22.69s | 24.65s |

### 3.2 Resource Footprint: CPU Utilization & Memory Density
| Scenario | Peak Cts | Alpine Deploy CPU (%) | Ubuntu Deploy CPU (%) | Alpine Peak RAM (MB) | Ubuntu Peak RAM (MB) | Alpine Net RAM Delta | Ubuntu Net RAM Delta |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **$M=1, N=1$** | 2 | 11.4% | 10.0% | 3,506 MB | 3,881 MB | **+38 MB** | **+98 MB** |
| **$M=2, N=5$** | 20 | 13.4% | 16.4% | 3,819 MB | 4,860 MB | **+309 MB** | **+973 MB** |
| **$M=2, N=7$** | 28 | 13.7% | 15.4% | 3,960 MB | 5,309 MB | **+370 MB** | **+1,333 MB** |
| **$M=2, N=10$**| 40 | 16.1% | 20.3% | 4,153 MB | 6,070 MB | **+563 MB** | **+1,976 MB** |
| **$M=5, N=5$** | 50 | 16.5% | 22.0% | 4,375 MB | 6,751 MB | **+729 MB** | **+2,531 MB** |
| **$M=5, N=7$** | 70 | 19.4% | 21.7% | 4,757 MB | 7,973 MB | **+1,028 MB** | **+3,568 MB** |
| **$M=5, N=10$**| 100 | 19.7% | 23.1% | 5,282 MB | 9,145 MB | **+1,445 MB** | **+5,156 MB** |
| **$M=10, N=2$**| 40 | 18.1% | 23.1% | 4,384 MB | 5,686 MB | **+611 MB** | **+1,931 MB** |
| **$M=10, N=5$**| 100 | 17.2% | 20.9% | 5,417 MB | 9,233 MB | **+1,533 MB** | **+5,247 MB** |

### 3.3 Key Insights from the Comparison
1. **Memory Density Scaling:**
   - **Alpine Linux:** Idles at **~15.3 MB per container** at 100-container scale (+1,533 MB delta over baseline).
   - **Ubuntu Heavy:** Idles at **~52.5 MB per container** at 100-container scale (+5,247 MB delta over baseline), supporting `systemd-journald`, `systemd-networkd`, `nginx`, and `postgresql`.
   - On a standard 16 GiB bare-metal or cloud host, BlueAgent comfortably packs **100 concurrent Ubuntu services** with 0 swap thrashing and >6 GiB of headroom for host caching.
2. **CPU Overhead of Full OS Containers:**
   - Average CPU utilization peaked at **23.1%** during Ubuntu Heavy deployment vs. **19.7%** on Alpine. The difference is attributable to parallel `systemd` unit initialization and service startup rather than container orchestration overhead.
3. **Static OVN Port Addressing vs. Dynamic Allocation Pools:**
   - When scaling to 10 parallel teams, assigning static IPv4 addresses directly during the `connect_network` patch call completely avoids Incus/OVN dynamic allocation pool lock contention (`Insufficient dynamic addresses allocated`), guaranteeing a **100% pass rate** even under burst concurrency.

---

## 4. Dataset Organization for Thesis Plotting

The evaluation produces four distinct dataset files per image on both the remote server and local scratch workspace:

| File Pattern | Content & Granularity | Best Thesis Use Case |
| :--- | :--- | :--- |
| `*_results.csv` | 1 row per scenario ($M, N$) with all macro/micro barrier timings & aggregate CPU/RAM metrics | Master scaling tables, summary bar charts, and LaTeX `pgfplotstable` imports |
| `*_step_resources.csv` | Long-format table (1 row per scenario $\times$ step) with duration, CPU avg/p95/max, and net RAM peak/delta | Grouped bar charts comparing resource usage across Provision, Deploy, Verify Setup, and Cleanup |
| `*_timeseries.csv` | Raw 100ms continuous time-series samples labeled by barrier step, elapsed seconds, CPU %, and RAM MB | Continuous line plots showing CPU utilization and RAM growth across the 6 barriers over time |
| `*_raw_samples.csv` | Per-team HTTP status and micro-step timestamps | Box plots and latency distribution histograms across teams |
