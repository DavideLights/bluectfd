# Contributing to BlueAgent

Thank you for helping improve **BlueAgent**! Because BlueAgent operates as a privileged, asynchronous bare-metal control plane managing Linux kernel networking (`iproute2`, `wg`, `nftables`), ZFS storage, Incus containers, and OVN logical switches, contributors should follow the architectural invariants and testing workflows documented below.

---

## 1. Architectural Invariants (Must Not Break)

1. **Strict Layer Separation**:
   - **`api/`**: HTTP request/response serialization, query/body parsing, auth dependency injection (`Depends(require_ctfd_token)`), and mapping domain exceptions to HTTP status codes. **Never invoke `ip`, `wg`, `nft`, or Incus directly inside `api/`.**
   - **`services/`**: Business workflows (`TeamService`, `ChallengeService`, `ImageService`), lock acquisition (`LockRegistry`), SQLite state transitions, and compensating rollbacks on partial failure.
   - **`engine/` & `network/` & `verifier/` & `images/`**: Low-level system adapters (`IncusEngine`, `HostNetworkManager`, `WireGuardManager`, `OvnVerifier`, `DistrobuilderManager`).
2. **Never Block the Asyncio Event Loop**:
   - Use `aiosqlite` (`DatabaseManager`), `httpx.AsyncClient` over Unix Domain Sockets (`IncusEngine`), `filelock.AsyncFileLock` wrapped cleanly (`LockRegistry`), and `CommandRunner` (`asyncio.create_subprocess_exec`) for all I/O.
3. **Subprocess & Shell Safety (`CommandRunner`)**:
   - Never invoke `subprocess.run(..., shell=True)` or interpolate user strings into shell commands.
   - Add pure argument-list builders (`List[str]`) to `src/blueagent/network/commands.py` and execute them via `CommandRunner.run()`.
   - Preserve the anonymous `tempfile.TemporaryFile()` stdout/stderr pattern in `src/blueagent/network/_runner.py` — it prevents 64 KB OS pipe deadlocks when background daemons inherit descriptors and avoids `/proc/self/fd` `ENXIO` failures inside `chroot` environments.
4. **Lock Discipline & Multi-Worker Safety**:
   - Wrap team topology or container mutations in `async with locks.lock_team(team_k):`.
   - Wrap Dark OVN verification runs in `async with locks.lock_verify(team_k, timeout=0.1):` so concurrent verification attempts fail fast with `LockAcquisitionError` (`HTTP 409 Conflict`) without blocking VPN peer downloads on `lock_team(team_k)`.
5. **Guaranteed Resource Cleanup & Compensating Rollbacks**:
   - In `OvnVerifier.run_verification()`, ephemeral clones (`c-{k}-{name}-v-{uuid}`), verifier runners, and OVN switches (`verify-ovn-{uuid}`) **must always** be destroyed inside a `finally:` block, even on cancellation or timeout.
   - In `TeamService` and `ChallengeService`, if multi-step provisioning fails mid-flight, roll back any bridges, WireGuard interfaces, or containers created earlier in the transaction before re-raising.

---

## 2. Step-by-Step Guide: Adding or Modifying a Feature

When introducing a new capability (for example, adding a new container limit or endpoint):

1. **Define Schemas (`src/blueagent/models.py`)**:
   - Add or extend Pydantic v2 request/response models with explicit `Field(...)` constraints, regex patterns, and docstrings.
   - If new fields inside `ChallengeConfig` can contain dynamic IP/team tokens, verify `render_challenge_config()` traverses them.
2. **Update Persistence (`src/blueagent/database.py`)**:
   - Update table DDL in `DatabaseManager.init_db()` (keep schema initialization idempotent with `CREATE TABLE IF NOT EXISTS` or safe column migrations) and add async CRUD methods.
3. **Implement System Adapter Logic (`engine/`, `network/`, `verifier/`, or `images/`)**:
   - Add abstract methods to `src/blueagent/engine/base.py` (`ContainerEngine` / `NetworkEngine`) before implementing concrete logic in `IncusEngine` so unit tests can mock the interface cleanly.
4. **Orchestrate in `src/blueagent/services/`**:
   - Acquire the appropriate `LockRegistry` scope, coordinate engine + DB calls, and record `step_timings` where helpful for observability.
5. **Expose via FastAPI Router (`src/blueagent/api/`)**:
   - Attach `dependencies=[Depends(require_ctfd_token)]` (unless explicitly public like `/health`).
   - Map domain exceptions consistently:
     - `KeyError` → `404 Not Found`
     - `LockAcquisitionError` → `409 Conflict`
     - `ValueError` → `400 Bad Request` or `422 Unprocessable Entity`
     - `TimeoutError` → `504 Gateway Timeout`
     - `RuntimeError` → `500 Internal Server Error`
6. **Sync with CTFd Plugin (If API Contract Changed)**:
   - Check [`../CONTRIBUTING.md`](../CONTRIBUTING.md) and update `CTFd/CTFd/plugins/blueagent/client.py` + `spec.py` if request/response schemas changed.

---

## 3. Writing & Running Tests

### 3.1 Unit & API Contract Tests (`tests/test_*.py`)

Unit tests run anywhere (macOS/Linux CI without `root`, Incus, ZFS, or WireGuard) in ~5 seconds.

- **How to Mock**:
  - Mock `CommandRunner` (`AsyncMock`) when testing `HostNetworkManager`, `WireGuardManager`, or `DistrobuilderManager` to assert exact CLI argument vectors.
  - Mock `ContainerEngine` / `NetworkEngine` when testing `ChallengeService`, `OvnVerifier`, and `LifecycleManager`.
  - Use `tmp_path` pytest fixtures for real `DatabaseManager(str(tmp_path / "test.db"))` and `LockRegistry(lock_dir=str(tmp_path / "locks"))` instances so SQLite WAL queries and OS file locking are tested for real.
- **Run Command**:
  ```bash
  .venv/bin/pytest tests/test_*.py \
      --ignore=tests/test_performance.py \
      --ignore=tests/test_thesis.py -v
  ```

### 3.2 Live Hardware Usability Suite (`tests/usability/`)

When modifying kernel networking, Incus REST payloads, `distrobuilder`, or OVN switches, run the live end-to-end suite on a bootstrapped Debian 13 VM:

```bash
# Check host readiness first
sudo ../scripts/bootstrap_host.sh --check

# Execute ordered hardware usability tests
sudo .venv/bin/pytest tests/usability/ -v
```

After running live tests, confirm clean teardown:

```bash
sudo incus list
sudo incus network list
ip -br link show type bridge
ip -br link show type wireguard
```

---

## 4. Code Style & Pull Request Checklist

- **Python Version**: Target Python `3.11+` (`from __future__ import annotations` / standard union types).
- **Type Annotations**: Annotate all public functions, service methods, and FastAPI route handlers.
- **Docstrings**: Keep module and class docstrings accurate; preserve existing architectural comments explaining kernel/Incus/OVN subtleties.
- **PR Checklist**:
  - [ ] All mocked unit tests (`pytest tests/test_*.py ...`) pass with zero warnings/regressions.
  - [ ] New endpoints or `Settings` fields are documented in [`README.md`](README.md).
  - [ ] Compensating rollback and `finally:` cleanup paths are covered by unit tests.
  - [ ] If any REST route or model changed, `CTFd/CTFd/plugins/blueagent` was updated and tested.
