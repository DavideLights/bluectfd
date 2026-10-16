# BlueAgent CTFd Plugin: Custom Challenge Type Implementation

## Module: `CTFd/CTFd/plugins/blueagent/challenge.py`

Subclasses `CTFd.plugins.challenges.BaseChallenge` to integrate patch-defense challenges into CTFd core challenge lifecycle.

```python
class BlueAgentPatchChallengeType(BaseChallenge):
    id = "blueagent_patch"
    name = "blueagent_patch"
    challenge_model = BlueAgentPatchChallenge

    templates = {
        "create": "/plugins/blueagent/assets/create.html",
        "update": "/plugins/blueagent/assets/update.html",
        "view": "/plugins/blueagent/assets/view.html",
    }
    scripts = {
        "create": "/plugins/blueagent/assets/create.js",
        "update": "/plugins/blueagent/assets/update.js",
        "view": "/plugins/blueagent/assets/view.js",
    }
    route = "/plugins/blueagent/assets/"
```

---

## Lifecycle Methods

### 1. `create(request)`
- Accepts multipart form data or JSON payload from admin modal.
- Extracts specification from uploaded file (`spec_file`) or raw text editor (`spec_text`).
- Validates syntax and schema via `parse_spec_text()` and `validate_spec_structure()`.
- Runs `check_global_collisions()` to ensure container names and IP suffixes are globally unique.
- Optionally triggers async Distrobuilder image build if `distrobuilder_file` is attached.
- Saves `BlueAgentPatchChallenge` record to database.

### 2. `read(challenge)`
- Serializes challenge properties for frontend rendering.
- **Security Redaction**: When requested by an unprivileged player, strips `verify_commands`, `readiness_commands`, and internal verification probes from the serialized JSON payload. Admin users receive complete unsanitized spec.

### 3. `update(challenge, request)`
- Updates challenge metadata, scoring value, and configuration toggles.
- If specification is updated, re-executes validation and collision detection (excluding current challenge ID from collision set).

### 4. `delete(challenge)`
- Iterates over all active team deployments (`BlueAgentDeployment.query.filter_by(challenge_id=challenge.id)`).
- Sends teardown requests to BlueAgent for each deployed team bridge to prevent orphaned running containers.
- Deletes `BlueAgentPatchChallenge` row (cascading deletes clean up deployment and log records).

### 5. `attempt(challenge, request)`
- If `allow_flag_fallback` is active, checks submitted flag string against CTFd `Flags` table.
- If patch verification is primary, standard text input is bypassed in favor of `/api/v1/plugins/blueagent/challenges/<id>/verify`.
