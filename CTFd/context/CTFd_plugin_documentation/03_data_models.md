# BlueAgent CTFd Plugin: Data Models & Schema

## Overview: `CTFd/CTFd/plugins/blueagent/models.py`

Defines 4 SQLAlchemy models extending CTFd schema with polymorphic inheritance and cascade constraints.

```
+--------------------+ 1      1 +----------------------------+
| Challenges (Core)  |<-------->| BlueAgentPatchChallenge    |
| id, name, value... | (CASCADE)| spec_json, cooldown, flags |
+--------------------+          +----------------------------+
         ^ 1
         │
         │ * (CASCADE)
+----------------------------+
| BlueAgentDeployment        |
| team_k, challenge_id       |
| status (running/failed...) |
+----------------------------+

+----------------------------+  +----------------------------+
| BlueAgentTeamMapping       |  | BlueAgentVerifyLog         |
| account_id, team_k (1..254)|  | team_k, challenge_id, user |
| provisioned (bool)         |  | passed, details_json, time |
+----------------------------+  +----------------------------+
```

---

## 1. `BlueAgentPatchChallenge`

Extends core `Challenges` using SQLAlchemy polymorphic inheritance.

```python
class BlueAgentPatchChallenge(Challenges):
    __tablename__ = "blueagent_challenges"
    __mapper_args__ = {"polymorphic_identity": "blueagent_patch"}

    id = db.Column(
        db.Integer,
        db.ForeignKey("challenges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    spec_raw = db.Column(db.Text, default="")
    spec_json = db.Column(db.Text, default="{}")
    spec_format = db.Column(db.String(16), default="yaml")
    distro_alias = db.Column(db.String(64), nullable=True)
    auto_deploy = db.Column(db.Boolean, default=True)
    show_verify_details = db.Column(db.Boolean, default=False)
    verify_cooldown = db.Column(db.Integer, default=30)
    allow_flag_fallback = db.Column(db.Boolean, default=False)
    container_names = db.Column(db.Text, default="[]")
    ip_suffixes = db.Column(db.Text, default="[]")
```

### Critical Rules
- `ondelete="CASCADE"` on `challenges.id` foreign key ensures deleting challenge from CTFd admin automatically purges plugin table row without orphan integrity violations.
- `parsed_spec`, `parsed_container_names`, and `parsed_ip_suffixes` JSON helper properties deserialize stored text safely with empty fallbacks.

---

## 2. `BlueAgentTeamMapping`

Maps CTFd team ID (or user ID in user mode) to integer `team_k` in range `1..254`.

```python
class BlueAgentTeamMapping(db.Model):
    __tablename__ = "blueagent_team_mappings"

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    account_type = db.Column(db.String(16), nullable=False, default="team")
    team_k = db.Column(db.Integer, unique=True, nullable=False, index=True)
    provisioned = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
```

### Allocation & Concurrency Logic (`get_or_create`)
- Checks existing record first.
- If missing, queries all currently allocated `team_k` values.
- Picks lowest unused integer in `range(1, 255)`. (Value `0` is reserved for host/infrastructure).
- Wraps write in `db.session.begin_nested()` savepoint. If concurrent request claims candidate `team_k`, catches `IntegrityError`, rolls back savepoint, and retries up to 5 times.
- Raises `CapacityError` if all 254 subnets are consumed.

---

## 3. `BlueAgentDeployment`

Tracks container deployment state per team and challenge.

```python
class BlueAgentDeployment(db.Model):
    __tablename__ = "blueagent_deployments"
    __table_args__ = (
        db.UniqueConstraint("team_k", "challenge_id", name="uq_team_challenge_deployment"),
    )

    id = db.Column(db.Integer, primary_key=True)
    team_k = db.Column(db.Integer, nullable=False, index=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    status = db.Column(db.String(32), default="idle")  # idle, deploying, running, failed, torn_down
    last_error = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
```

---

## 4. `BlueAgentVerifyLog`

Audit trail for all OVN sandbox verification executions.

```python
class BlueAgentVerifyLog(db.Model):
    __tablename__ = "blueagent_verify_logs"

    id = db.Column(db.Integer, primary_key=True)
    team_k = db.Column(db.Integer, nullable=False, index=True)
    challenge_id = db.Column(db.Integer, db.ForeignKey("challenges.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    passed = db.Column(db.Boolean, nullable=False)
    success = db.Column(db.Boolean, nullable=False)
    message = db.Column(db.Text, default="")
    details_json = db.Column(db.Text, default="{}")
    duration_ms = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
```
