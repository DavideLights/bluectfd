# BlueAgent CTFd Plugin: Dark OVN Verification & Solve Pipeline

## Flow: `verify_and_record_solve()` in `services.py`

Verifies defensive cyber patches using an ephemeral, copy-on-write dark sandbox and records scores directly into the CTFd database.

```
Player clicks "Verify Patch" (/api/v1/plugins/blueagent/challenges/<id>/verify)
  │
  ├── 1. Check existing solve in CTFd Solves table
  │     If already solved -> Return 200 OK ("already_solved")
  │
  ├── 2. Check & acquire verification cooldown (e.g. 30s)
  │     If active -> Return 429 ("cooldown", retry_after=N)
  │
  ├── 3. Acquire team-level mutex lock (team_verify_lock)
  │     If busy -> Refund cooldown -> Return 409 ("busy")
  │
  ├── 4. Call BlueAgent: POST /teams/{k}/verificator
  │     ┌────────────────────────────────────────────────────────┐
  │     │ BlueAgent Orchestrator:                                │
  │     │ 1. Snapshot running containers via ZFS CoW clone (~100ms)│
  │     │ 2. Create isolated dark OVN logical switch             │
  │     │ 3. Attach cloned containers to dark switch             │
  │     │ 4. Launch ephemeral verifier container (10.k.1.250)    │
  │     │ 5. Execute readiness_commands                          │
  │     │ 6. Execute verify_commands (functional & exploit probes)│
  │     │ 7. Destroy verifier, switch, and clones (LIFO)         │
  │     │ 8. Return {"passed": bool, "details": dict}            │
  │     └────────────────────────────────────────────────────────┘
  │
  ├── 5. Handle Exceptions & Conflicts:
  │     - BlueAgentConflict (409): Refund cooldown -> Return 409
  │     - TeamNotFound (404): Refund cooldown -> Return 409 ("not_deployed")
  │     - BlueAgentUnavailable (503): Refund cooldown -> Return 503
  │
  ├── 6. Record Audit Log:
  │     Insert into blueagent_verify_logs (team_k, user_id, passed, duration_ms)
  │
  └── 7. Atomic Scoreboard Commit:
        ├── IF passed == True:
        │     - Insert Solves(user_id, team_id, challenge_id, provided="VERIFIED_PATCH")
        │     - clear_standings() & clear_challenges() (Bust CTFd cache)
        │     - Return 200 OK {"solved": True}
        └── IF passed == False:
              - Insert Fails(user_id, team_id, challenge_id, provided="FAILED_PATCH")
              - If challenge.show_verify_details == True -> return exploit output
              - Return 200 OK {"solved": False}
```

---

## Key Reliability Safeguards

1. **Cooldown Refund**: If the verification request fails due to transient infrastructure conditions (409 conflict, network drop, orchestrator busy), `refund_cooldown()` is immediately invoked so players are not unfairly penalized with a waiting penalty.
2. **Concurrent Solve Protection**: If two teammates click "Verify Patch" at the same instant, the first creates the `Solves` record. The second catches SQLAlchemy `IntegrityError`, rolls back the transaction, and returns a benign `"already_solved"` status without raising a 500 error.
3. **Exploit Redaction**: Player responses contain verification logs only when `show_verify_details` is enabled on the challenge. Otherwise, secret exploit output is redacted from the payload.
