# POST /teams/{team_k}/verificator

## ENDPOINT & METHOD
`POST /teams/{team_k}/verificator`

## PURPOSE & CTFD CONTEXT
EXECUTE AUTOMATED SERVICE & PATCH VERIFICATION VIA DARK OVN SANDBOX:
1. ACQUIRES EXCLUSIVE TEAM VERIFICATION LOCK (REJECTS CONCURRENCY).
2. CREATES ZFS COPY-ON-WRITE (COW) INSTANT SNAPSHOT CLONES OF RUNNING CONTAINERS (~100MS).
3. BINDS CLONE `eth0` INTERFACES TO EPHEMERAL OVN LOGICAL SWITCH (`verify-{team_k}`, DARK NETWORK ISOLATED FROM PLAYERS, OPTIONAL EGRESS NAT).
4. ATTACHES EPHEMERAL VERIFIER RUNNER CONTAINER (`10.k.1.250` OR `verifier.ip_suffix`).
5. RUNS `readiness_commands` WITH INTERVAL & RETRY TIMEOUT (ENSURES SERVICES RUNNING).
6. RUNS `verify_commands` (FUNCTIONALITY CHECKS + EXPLOIT VULNERABILITY AUDITS).
7. PERFORMS STRICT LIFO ATOMIC TEARDOWN OF SANDBOX CLONES & OVN SWITCH. PRODUCTION CONTAINERS UNTOUCHED.
USE CASE: PLAYER CLICKS "VERIFY PATCH", OR SLA CRON CHECKER. ON SUCCESS (`passed==True`), CTFD AWARDS SOLVE/FLAG!

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETER:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
HEADERS: `Content-Type: application/json`
BODY SCHEMA (`ChallengeConfig`):
```json
{
  "challenge_id": "patch-service-01",
  "verify_internet_access": true,
  "verifier": {
    "image": "alpine-verifier",
    "ip_suffix": 250,
    "limits_cpu": "1",
    "limits_memory": "256MB"
  },
  "containers": [
    {
      "name": "vuln-app",
      "image": "alpine-patch-service",
      "ip_suffix": 10,
      "limits_cpu": "1",
      "limits_memory": "256MB"
    }
  ],
  "readiness_commands": [
    {
      "target": "container",
      "container_name": "vuln-app",
      "cmd": ["nc", "-z", "127.0.0.1", "8080"],
      "timeout": 5,
      "interval": 2,
      "retry_timeout": 30
    }
  ],
  "verify_commands": [
    {
      "target": "verifier",
      "cmd": ["curl", "-sf", "http://10.1.1.10:8080/health"],
      "timeout": 10
    },
    {
      "target": "verifier",
      "cmd": ["python3", "/exploits/test_vuln.py", "10.1.1.10"],
      "timeout": 15
    }
  ]
}
```

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA (`VerifyResultResp`):
```json
{
  "success": true,
  "passed": true,
  "message": "All verification commands passed.",
  "details": {
    "step_timings": {
      "sandbox_setup": {
        "ovn_network_create_sec": 0.08,
        "total_clone_time_sec": 0.15,
        "total_connect_time_sec": 0.04,
        "total_start_time_sec": 0.32,
        "verifier_runner_sec": 0.22,
        "total_setup_sec": 0.85
      },
      "readiness_sec": 0.42,
      "verify_commands_sec": 0.35,
      "cleanup": {
        "clones_teardown_sec": 0.18,
        "ovn_network_delete_sec": 0.09,
        "total_cleanup_sec": 0.28
      },
      "total_verify_lifecycle_sec": 1.90
    }
  }
}
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: TEAM DOES NOT EXIST -> VERIFY TEAM IS PROVISIONED.
- `409 Conflict`: `VerificationLockedError` -> VERIFICATION ALREADY IN PROGRESS OR TEAM RESOURCE LOCKED. CTFD MUST RETRY SHORTLY.
- `401 Unauthorized`: TOKEN INVALID.
- `500 Internal Server Error`: VERIFICATION FAILED UNEXPECTEDLY.
