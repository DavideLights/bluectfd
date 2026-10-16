# BlueAgent CTFd Plugin: Architecture Overview

## System Boundaries

```
+-------------------------------------------------------+
| CTFd Core (Flask + SQLAlchemy + Redis)                |
| - Authentication, Team/User Accounts                  |
| - Scoreboard, Solves, Fails, Flags                    |
| - BlueAgent CTFd Plugin                               |
|   * Custom Challenge Type: blueagent_patch            |
|   * Deterministic Address Allocator (team_k 1..254)   |
|   * REST API Client (FastAPI Orchestrator)            |
|   * Atomic Locks & Per-Team Cooldown Engine           |
+-------------------------------------------------------+
                           |
            REST HTTP / JSON + Bearer Token
            Target: http://172.17.0.1:8000
                           v
+-------------------------------------------------------+
| BlueAgent v2 Orchestrator (Debian 13 Bare-Metal)      |
| - Linux Bridges: br-team-k (10.k.1.1/24)              |
| - WireGuard Kernel Interface: wg-k (10.k.0.1/24)      |
| - Incus Engine: Challenge Containers (10.k.1.x)       |
| - Dark OVN Sandbox: Ephemeral Switch + ZFS CoW Clones |
| - Verifier Runner Container: Static 10.k.1.250        |
+-------------------------------------------------------+
```

## Role Division

| Responsibility | CTFd Plugin | BlueAgent v2 Core |
|---|---|---|
| User / Team Identity | Manages account sessions, maps to `team_k` (1..254) | Consumes integer `team_k` |
| Challenge Metadata | Stores name, category, points, state, spec | Consumes parsed `ChallengeConfig` |
| Network Addressing | Allocates subnets deterministically via `team_k` | Configures host bridge `br-team-k` and `wg-k` |
| WireGuard VPN Keys | Triggers member allocation, serves client `.conf` | Generates Curve25519 keys, programs kernel peers |
| Container Lifecycle | Sends deploy/teardown requests based on user state | Executes Incus container launches, stops, and deletes |
| Verification Execution | Coordinates cooldowns, triggers verify endpoint | Runs ZFS CoW clone, OVN attach, readiness & exploits |
| Score Tracking | Commits to `Solves`/`Fails`, invalidates cache | Returns Boolean `passed` + status details |

## Core Invariants

1. **State Engine Decoupling**: BlueAgent has no knowledge of CTFd user passwords, sessions, or scores. It only knows `team_k` and `ChallengeConfig`.
2. **Network Isolation**: Each team operates in isolated `/24` subnets (`10.k.0.0/24` VPN, `10.k.1.0/24` containers). Host router prevents inter-team cross-talk.
3. **Verification Isolation**: Verifier tests never execute against live containers; tests run against ephemeral ZFS CoW clones attached to dark OVN logical switches.
4. **Idempotency**: All management actions (`create_team`, `add_member`, `deploy_challenge`) are idempotent.
