# BlueAgent CTFd Plugin: Services & Orchestration Layer

## Module: `CTFd/CTFd/plugins/blueagent/services.py`

Coordinates user session context, lazy infrastructure provisioning, container deployments, and teardown workflows.

---

## 1. Account Context Resolution (`get_current_account_context`)

CTFd operates in two distinct modes: **Teams Mode** (`user_mode == "teams"`) and **Users Mode** (`user_mode == "users"`).

```python
def get_current_account_context() -> Tuple[int, str, str, Optional[int]]:
    user = get_current_user()
    if not user:
        raise PermissionError("Authentication required.")

    mode = get_config("user_mode") or "teams"
    if mode == "teams":
        team = get_current_team()
        if not team:
            raise PermissionError("Team membership required in Teams mode.")
        return team.id, "team", team.name, user.id
    else:
        return user.id, "user", user.name, user.id
```

All downstream allocations tie to `account_id` and `account_type`.

---

## 2. Lazy Provisioning Workflows

Instead of pre-allocating bridges and WireGuard tunnels for all teams at CTFd launch, BlueAgent provisions resources on demand:

```
Player clicks "Start Environment" or "Download VPN"
  │
  ├── 1. Resolve team_k via BlueAgentTeamMapping.get_or_create()
  │
  ├── 2. ensure_team_provisioned(team_k, team_name)
  │     Calls POST /teams on BlueAgent
  │     Creates Linux bridge br-team-k and WireGuard wg-k
  │     Updates BlueAgentTeamMapping.provisioned = True
  │
  └── 3. ensure_member_vpn(team_k, user_id, username, team_name)
        Calls POST /teams/{k}/members
        Allocates WireGuard IP and returns client configuration
```

If a member endpoint throws `TeamNotFound` (e.g., orchestrator was rebooted or purged), the service catches the exception, recreates the team bridge, and automatically retries the member creation.

---

## 3. Container Deployment Lifecycle (`deploy_challenge_for_team`)

```python
def deploy_challenge_for_team(team_k, challenge, client=None):
    with team_deploy_lock(team_k) as acquired:
        if not acquired:
            return {"status": "locked", "message": "Deployment in progress"}

        rendered = render_spec_for_team(challenge.parsed_spec, team_k)
        deployment.status = "deploying"
        db.session.commit()

        try:
            res = client.deploy_challenge(team_k=team_k, config=rendered)
            deployment.status = "running"
            deployment.last_error = None
            db.session.commit()
            return {"status": "running", "containers": res.get("containers", [])}
        except Exception as exc:
            deployment.status = "failed"
            deployment.last_error = str(exc)
            db.session.commit()
            raise
```

## 4. Teardown Lifecycle (`teardown_challenge_for_team`)

- Acquires `team_deploy_lock(team_k)`.
- Sends `DELETE /teams/{team_k}/machines` with rendered challenge config.
- Updates `BlueAgentDeployment.status = "torn_down"`.
