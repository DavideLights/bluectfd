# Contributing to BlueCTFd

Thank you for contributing to **BlueCTFd**! This repository is a monorepo combining a bare-metal Linux cyber-range orchestrator (**BlueAgent**) with an integrated competition platform (**CTFd + `blueagent` plugin**).

---

## 1. Monorepo Structure & Component Guides

Before opening an issue or pull request, consult the architecture and contributor guide for the subproject you are modifying:

| Component | Path | Architecture & Setup | Contributor Guide |
| :--- | :--- | :--- | :--- |
| **BlueAgent Orchestrator** | [`blueagent/`](blueagent/) | [`blueagent/README.md`](blueagent/README.md) | [`blueagent/CONTRIBUTING.md`](blueagent/CONTRIBUTING.md) |
| **CTFd Platform & Plugin** | [`CTFd/`](CTFd/) · [`CTFd/CTFd/plugins/blueagent/`](CTFd/CTFd/plugins/blueagent/) | [`CTFd/README.md`](CTFd/README.md) | [`CTFd/CONTRIBUTING.md`](CTFd/CONTRIBUTING.md) |
| **Host Bootstrap & E2E Tests** | [`scripts/`](scripts/) · [`yaml/`](yaml/) | [`README.md`](README.md) | This document |

---

## 2. Cross-Component Synchronization Contract

Because **CTFd (`CTFd/plugins/blueagent`)** acts as an authenticated REST client to **BlueAgent (`blueagent`)**, changes across the boundary must stay strictly synchronized:

| Contract Surface | BlueAgent Source of Truth | CTFd Plugin Counterpart | Sync Rule |
| :--- | :--- | :--- | :--- |
| **REST Endpoints & HTTP Verbs** | `blueagent/src/blueagent/api/*.py` | `CTFd/CTFd/plugins/blueagent/client.py` | Keep paths (`PUT /teams/{team_k}`, `PUT /teams/{team_k}/members/{user_id}`, `DELETE /teams/{team_k}/machines?containers=...`, etc.) and status codes aligned. |
| **Challenge Spec Schema** | `ChallengeConfig` in `blueagent/src/blueagent/models.py` | `parse_and_validate_spec()` in `CTFd/CTFd/plugins/blueagent/spec.py` | Any new field, regex constraint (`challenge_id`, container `name`), or `ip_suffix` bound (`2..254`) must be validated in both places. |
| **Template Placeholders** | `render_challenge_config()` in `blueagent/src/blueagent/models.py` | `render_spec_for_team()` in `CTFd/CTFd/plugins/blueagent/spec.py` | Both support `{{team_k}}`, `{{subnet}}`, `{{verifier_ip}}`, and `{{ip:<name>}}`. Add new placeholders to both functions + unit tests. |
| **Authentication Headers** | `require_ctfd_token()` in `blueagent/src/blueagent/dependencies.py` | `BlueAgentClient._headers()` in `CTFd/CTFd/plugins/blueagent/client.py` | Supports `Authorization: Bearer <token>` and `X-CTFd-Token: <token>` via constant-time comparison. |
| **Lock / Cooldown Semantics** | `LockAcquisitionError` → `HTTP 409` in `blueagent` | `BlueAgentConflictError` in `services.py` / `locks.py` | `HTTP 409` and `5xx` errors **must never** consume a player's verification cooldown or attempt counter in CTFd. |
| **`team_k` Addressing Range** | `0..254` (`0` reserved for host/infra tests) | `allocate_team_k()` allocates `1..254` | Player teams in CTFd must always map to `1 <= team_k <= 254` (`10.k.0.0/24` VPN, `10.k.1.0/24` containers). |

---

## 3. Development Workflow

1. **Fork & Branch**: Create a focused topic branch from `main`:
   - `feat/<scope>-<short-description>`
   - `fix/<scope>-<short-description>`
   - `docs/<scope>-<short-description>`
   - `test/<scope>-<short-description>`
2. **Conventional Commits**: Format commit messages following [Conventional Commits](https://www.conventionalcommits.org/):
   - Scopes: `blueagent`, `plugin`, `ctfd`, `scripts`, `docs`, `ci`
   - Examples:
     - `feat(blueagent): support selective container teardown via query param`
     - `fix(plugin): refund verification cooldown when orchestrator returns 409`
     - `docs(readme): document hybrid Docker and multi-worker uvicorn startup`
3. **Keep Host State Clean**: When running live integration or usability tests on a Debian 13 VM, verify afterward that no orphan interfaces (`br-team-*`, `wg-*`), OVN switches (`verify-ovn-*`), or test containers (`c-*`) remain unless intentionally testing persistence.

---

## 4. Required Test Gates Before Opening a Pull Request

All pull requests must pass both fast unit test suites (no root or Incus daemon required):

```bash
# 1. BlueAgent unit test suite (mocked engine, network, locks, API, and services)
cd blueagent
.venv/bin/pytest tests/test_*.py \
    --ignore=tests/test_performance.py \
    --ignore=tests/test_thesis.py -v

# 2. CTFd BlueAgent plugin test suite (spec validation, client, mapping, verify/solve, routes)
cd ../CTFd
.venv/bin/pytest CTFd/plugins/blueagent/tests -v
```

If your change touches **Incus, ZFS, WireGuard, Linux bridges, `nftables`, OVN, or `distrobuilder`**, also run the live hardware verification suite on a bootstrapped Debian 13 VM:

```bash
# Verify host prerequisites
sudo ./scripts/bootstrap_host.sh --check

# Run BlueAgent live dataplane & OVN usability tests
cd blueagent
sudo .venv/bin/pytest tests/usability/ -v
```

---

## 5. Security Reporting & Exploit Hygiene

- **Never expose `verify_commands`** in any player-accessible CTFd API endpoint or template. Exploit payloads execute exclusively inside isolated Dark OVN verification sandboxes.
- **Subprocess safety**: Never pass untrusted user input to `shell=True`. Always construct argument vectors (`List[str]`) via `blueagent/src/blueagent/network/commands.py` and execute through `CommandRunner`.
- **Archive & YAML validation**: Preserve path-traversal checks (`..` / absolute paths / symlinks escaping build context) in `DistrobuilderManager` when modifying image upload flows.
