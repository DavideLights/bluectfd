# BlueAgent CTFd Plugin: Concurrency Locks & Cooldowns

## Module: `CTFd/CTFd/plugins/blueagent/locks.py`

Guarantees thread-safe and cluster-safe operations across multi-worker CTFd environments (Gunicorn / uWSGI).

---

## 1. Dual-Tier Locking Architecture

The lock manager checks `CTFd.cache.cache` (Redis or Memcached) first. If Redis is unavailable or CTFd runs in single-process mode, it falls back to thread-safe in-memory data structures guarded by `threading.Lock()`.

```
                    ┌─────────────────────────┐
                    │ check_and_set_cooldown  │
                    │   or team_verify_lock   │
                    └───────────┬─────────────┘
                                │
                 Is CTFd.cache available?
                                │
                 ┌──────────────┴──────────────┐
                 ▼ YES                         ▼ NO / In-Memory
         ┌───────────────┐             ┌───────────────┐
         │ Redis Atomic  │             │ Threading     │
         │ cache.add()   │             │ Mutex Map     │
         └───────────────┘             └───────────────┘
```

---

## 2. Cooldown Engine

Enforces waiting intervals between verification attempts to prevent DoS attacks on the Incus/OVN hypervisor.

```python
is_allowed, seconds_remaining = check_and_set_cooldown(team_k, challenge_id, cooldown_seconds=30)
```

- **Atomic Key**: `blueagent:cooldown:<team_k>:<challenge_id>`.
- **`cache.add(key, expiry_ts, timeout)`**: Returns `True` only if key did not exist.
- **Refund Policy (`refund_cooldown`)**: Invoked whenever an infrastructure fault (HTTP 409, 503, connection dropped) prevents the test from completing.

---

## 3. Team-Level Mutexes

Prevents concurrent modifications on team bridges and dark OVN switches.

### `team_verify_lock(team_k, timeout=330)`
- **Key**: `blueagent:lock:verify:<team_k>`
- Ensures only ONE verification test runs at a time for team `k`.
- Context manager automatically clears lock on block exit or unhandled exception.

### `team_deploy_lock(team_k, timeout=120)`
- **Key**: `blueagent:lock:deploy:<team_k>`
- Serializes `POST /teams/{k}/machines` and `DELETE /teams/{k}/machines`.
- Prevents split-brain container provisioning when players spam deploy buttons.
