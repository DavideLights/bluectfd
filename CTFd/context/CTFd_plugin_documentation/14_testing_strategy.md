# BlueAgent CTFd Plugin: Testing Strategy & Test Harness

## Directory: `CTFd/CTFd/plugins/blueagent/tests/`

The test suite validates the plugin without requiring a running Incus/OVN hypervisor by using response mocking and CTFd's internal test harness.

---

## 1. Test Suite Structure

```
CTFd/CTFd/plugins/blueagent/tests/
├── conftest.py          # CTFd application fixture & database lifecycle setup
├── test_spec.py         # IaC parser, schema validation, and template rendering
├── test_client.py       # HTTP client mocked responses, timeouts, and error mappings
├── test_mapping.py      # Deterministic team_k allocation (1..254) and race conditions
├── test_verify_flow.py  # Dark OVN verification, atomic solve commit, and cooldowns
├── test_routes_admin.py # Endpoint authorization (admins_only vs authed_only)
└── test_use_cases.py    # Complete real-world end-to-end integration scenarios
```

---

## 2. Test File Breakdown

| Test File | Focus | Key Assertions |
|---|---|---|
| `test_spec.py` | Schema & Templates | Rejects invalid YAML, missing containers, colliding IP suffixes; verifies `{{team_k}}` and `{{ip:app}}` interpolation. |
| `test_client.py` | HTTP Client Resilience | Maps HTTP 401, 404, 409 (with `is_verify_busy` flags), 422, and 503 to explicit Python exceptions. |
| `test_mapping.py` | Subnet Allocator | Ensures sequential mapping in `[1, 254]`; tests savepoint rollback on concurrent allocation; verifies `CapacityError` at 255. |
| `test_verify_flow.py` | Solve Pipeline | Verifies `Solves` table insertion on `passed=True`, `Fails` insertion on `False`, and 429 response on active cooldown. |
| `test_routes_admin.py` | Route Security | Ensures unauthenticated users receive 403; verifies admin endpoints reject non-admin users. |
| `test_use_cases.py` | Full Lifecycle E2E | 3 multi-stage scenarios: Team VPN & Telemetry, Image Build & Deployment, and Challenge Verification with Flag Fallback. |

---

## 3. Execution Instructions

Run all 28 automated tests from the CTFd directory:

```bash
cd /home/debian/bluectfd/CTFd
source .venv/bin/activate
pytest CTFd/plugins/blueagent/tests/ -v
```

All 28 tests pass cleanly.
