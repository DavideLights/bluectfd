# BlueAgent CTFd Plugin: Challenge Specification & IaC Engine

## Module: `CTFd/CTFd/plugins/blueagent/spec.py`

Manages Infrastructure-as-Code (IaC) challenge definitions: parsing, syntax validation, global collision checking, and dynamic template rendering.

---

## 1. Specification Format (YAML/JSON)

```yaml
challenge_id: "sec-patch-webapp"
team_k: 1                          # Optional in template; injected at runtime
verify_internet_access: false

verifier:
  image: "curlimages/curl:latest"
  ip_suffix: 250
  limits_cpu: "1"
  limits_memory: "256MB"

containers:
  - name: "app"
    image: "alpine-nginx-sec"
    ip_suffix: 10
    limits_cpu: "1"
    limits_memory: "512MB"

readiness_commands:
  - target: "container"
    container_name: "app"
    cmd: ["pgrep", "nginx"]
    timeout: 5
    interval: 1
    retry_timeout: 10

verify_commands:
  # Probe 1: Functional check (must succeed with exit code 0)
  - target: "verifier"
    cmd: ["curl", "-sf", "http://{{ip:app}}:80/"]
    timeout: 10
  # Probe 2: Exploit payload (must fail / return non-zero after player patch)
  - target: "verifier"
    cmd: ["curl", "-sf", "-X", "POST", "-d", "user=' OR 1=1--", "http://{{ip:app}}:80/login"]
    timeout: 15
```

---

## 2. Validation Constraints (`validate_spec_structure`)

| Field | Rule | Error |
|---|---|---|
| `challenge_id` | Match `^[a-zA-Z0-9][a-zA-Z0-9_\-\.]{0,63}$` | `Invalid 'challenge_id'` |
| `verifier.image` | Required string | `'verifier.image' is required` |
| `verifier.ip_suffix` | Integer `2..254` (default `250`) | `must be between 2 and 254` |
| `containers` | Non-empty list | `Must define at least one container` |
| `container.name` | Match `^[a-z0-9][a-z0-9-]{0,20}$` | `must be 1-20 lowercase alphanumeric` |
| `container.ip_suffix`| Integer `2..254`, unique within spec | `Collision detected` |
| `limits_memory` | Match `^\d+(MB\|GB)$` | `Invalid memory limit format` |
| File size limit | `<= 256 KB` | `Exceeds maximum size limit` |

---

## 3. Global Collision Protection (`check_global_collisions`)

Incus hosts all containers on host bridge `br-team-k`. If two active challenges share the same container name or IP suffix, containers collide upon concurrent deployment.

- `check_global_collisions(container_names, ip_suffixes, exclude_challenge_id=None)`:
  - Iterates over all active `BlueAgentPatchChallenge` rows in database.
  - Compares set intersections of `parsed_container_names` and `parsed_ip_suffixes`.
  - Rejects challenge creation/update with `SpecValidationError` if collision found.

---

## 4. Template Interpolation (`render_spec_for_team`)

Challenge authors write generic templates. During deployment or verification, the engine deeply renders placeholders into concrete runtime values for team `k`:

| Placeholder | Rendered Example (`team_k=5`) |
|---|---|
| `{{team_k}}` | `5` |
| `{{subnet}}` | `10.5.1` |
| `{{verifier_ip}}` | `10.5.1.250` |
| `{{ip:<container_name>}}` | `10.5.1.10` (resolves IP suffix of target container) |
