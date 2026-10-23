# Contributing to BlueCTFd & the BlueAgent Plugin

Thank you for contributing to **BlueCTFd**! Most custom functionality in this repository lives inside the self-contained plugin package [`CTFd/plugins/blueagent/`](CTFd/plugins/blueagent/) so that upstream [CTFd](https://github.com/CTFd/CTFd) upgrades remain clean and conflict-free.

---

## 1. Golden Rules & Security Invariants

1. **Isolate Customizations Inside `CTFd/plugins/blueagent/`**:
   - Avoid modifying core CTFd files (`CTFd/challenges.py`, `CTFd/teams.py`, `CTFd/themes/core-beta`, etc.) whenever CTFd's plugin hooks (`BaseChallenge`, Flask Blueprints, `register_plugin_assets_directory`, `register_admin_plugin_menu_bar`) can achieve the goal.
2. **Never Leak `verify_commands` or Exploit Payloads to Players**:
   - `BlueAgentPatchChallenge.spec_yaml` and `spec_json` contain the exact `verify_commands` (exploit scripts, secret flags, internal checker arguments) executed inside BlueAgent's Dark OVN sandbox.
   - `BlueAgentPatchChallengeType.read()` and all `/api/v1/plugins/blueagent/*` player routes **must strip `verify_commands` and raw specs**. Only expose `details` from `VerifyResultResp` to non-admins when `challenge.show_verify_details == True`.
3. **Always Refund Cooldowns on Infrastructure / Contention Errors**:
   - In `services.py`, if `BlueAgentClient.verify_challenge()` raises `BlueAgentConflictError` (`HTTP 409`), `BlueAgentTimeoutError` (`HTTP 504`), or `BlueAgentUnavailableError` (`5xx` / connection refused), **always call `clear_verify_cooldown(team_k, challenge_id)`**. Players must only incur a cooldown after a real verification run completes.
4. **Preserve Global Container Name & `ip_suffix` Uniqueness**:
   - Every active challenge for a team shares `br-team-{k}` (`10.{k}.1.0/24`) and Incus instance namespace `c-{team_k}-{container_name}`. Always run `check_global_collisions()` in `spec.py` when creating or updating challenges (`exclude_challenge_id=challenge.id` on updates).
5. **Frontend JavaScript Compatibility (Core-Beta & Admin Themes)**:
   - CTFd's `core-beta` player theme uses Alpine.js + `CTFd.fetch`, whereas the Admin theme uses jQuery / Bootstrap modals.
   - In `assets/create.js`, `assets/update.js`, and `assets/admin.js`, guard DOM initialization properly (`CTFd.plugin.run(...)` or `document.readyState` / `jQuery(function($){ ... })`) and **always** include `CSRF-Token: window.init.csrfNonce` on mutating `POST`/`PUT`/`DELETE` requests.

---

## 2. Common Contributor Workflows

### 2.1 Adding a New Field to `blueagent_patch` Challenges

1. **Model (`CTFd/plugins/blueagent/models.py`)**: Add the column with a safe default on `BlueAgentPatchChallenge`.
2. **Challenge Type (`CTFd/plugins/blueagent/challenge.py`)**:
   - Handle the attribute in `create()` and `update()`.
   - Expose it in `read()` *only* if safe for players to inspect.
3. **Admin Templates (`assets/create.html` & `assets/update.html`)**: Add the form control with matching `name="..."` attribute.
4. **Tests (`tests/test_use_cases.py` or `tests/test_verify_flow.py`)**: Add coverage verifying create, update, and player serialization.

### 2.2 Adding or Updating a BlueAgent API Call

1. **Client (`CTFd/plugins/blueagent/client.py`)**:
   - Add a typed method to `BlueAgentClient` using `_request(...)` with the appropriate timeout profile (`connect_timeout`, `deploy_timeout`, or `verify_timeout`).
   - Map `404` → `BlueAgentNotFoundError`, `409` → `BlueAgentConflictError`, `400`/`422` → `BlueAgentValidationError`, timeouts → `BlueAgentTimeoutError`.
2. **Mocked Client Test (`tests/test_client.py`)**:
   - Register expected request URL, method, auth header (`Authorization: Bearer ...`), and status mappings using `@responses.activate`.
3. **Cross-Repo Check**:
   - Verify the exact route path, HTTP verb, and Pydantic schema against `../blueagent/src/blueagent/api/` (see [`../CONTRIBUTING.md`](../CONTRIBUTING.md)).

### 2.3 Extending the Admin Control Center (`/admin/plugins/blueagent`)

1. Add the backend JSON endpoint under `/admin/plugins/blueagent/api/...` in `routes.py` decorated with `@admins_only` (and `@bypass_csrf_protection` **never** used on admin mutating endpoints — rely on `CSRF-Token` header).
2. Update `templates/blueagent/admin_dashboard.html` and `assets/admin.js` (using `window.init.csrfNonce` for AJAX calls).
3. Add an authorization test in `tests/test_routes_admin.py` confirming both admin success (`200`) and non-admin rejection (`403`/`302`).

---

## 3. Running & Debugging Tests

All plugin tests live in `CTFd/CTFd/plugins/blueagent/tests/` and execute in ~4 seconds without requiring Docker or a live BlueAgent host:

```bash
cd CTFd

# Run the entire 28-test suite
.venv/bin/pytest CTFd/plugins/blueagent/tests -v

# Run a single test module or test case with stdout visible
.venv/bin/pytest CTFd/plugins/blueagent/tests/test_verify_flow.py -v -s
```

### Key Test Fixtures (`tests/conftest.py`)
- `app`: Creates an isolated in-memory/temporary SQLite CTFd instance with the `blueagent` plugin loaded in **Teams Mode**.
- `admin_client`: Authenticated Flask test client with admin privileges and valid CSRF nonce helper.
- `player_client_factory`: Creates authenticated CTFd users + teams for multi-team concurrency and solve-recording tests.

---

## 4. Pull Request Checklist

- [ ] Custom code is confined to `CTFd/CTFd/plugins/blueagent/` (or documented in the PR description if touching `docker-compose*.yml` / root config).
- [ ] `.venv/bin/pytest CTFd/plugins/blueagent/tests -v` passes 100%.
- [ ] Player-facing endpoints do **not** leak `verify_commands`, `spec_yaml`, or `spec_json`.
- [ ] Cooldowns are refunded on `409`/`5xx`/timeout responses from BlueAgent.
- [ ] [`README.md`](README.md) and [`CTFd/plugins/blueagent/README.md`](CTFd/plugins/blueagent/README.md) are updated if routes, settings, or challenge spec fields changed.
