# POST /teams/{team_k}/machines

## ENDPOINT & METHOD
`POST /teams/{team_k}/machines`

## PURPOSE & CTFD CONTEXT
DEPLOY MULTI-CONTAINER CHALLENGE TOPOLOGY TO TARGET TEAM PRODUCTION BRIDGE `br-team-k`:
1. VERIFIES BRIDGE `br-team-k` EXISTS & ACTIVE.
2. VERIFIES REQUESTED DISTROBUILDER IMAGES (BOTH APPLICATION CONTAINERS AND OPTIONAL VERIFIER) ARE BUILT & READY.
3. CREATES INCUS CONTAINERS (NAMED `c-{team_k}-{spec.name}`).
4. BINDS `eth0` TO `br-team-k` WITH STATIC IP `10.k.1.<ip_suffix>`.
5. APPLIES CPU/MEMORY LIMIT QUOTAS.
6. STARTS CONTAINERS WITH ATOMIC ROLLBACK (IF ONE FAILS, DESTROYS PREVIOUS ONES).
USE CASE: ADMIN CLICKS "DEPLOY CHALLENGE TO TEAM", OR ON-DEMAND INSTANCE PROVISIONING ON TEAM CHALLENGE START.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETER:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
HEADERS: `Content-Type: application/json`
BODY SCHEMA (`ChallengeConfig`):
```json
{
  "challenge_id": "web-vuln-01",
  "verify_internet_access": true,
  "verifier": {
    "image": "alpine-verifier",
    "ip_suffix": 250,
    "limits_cpu": "1",
    "limits_memory": "256MB"
  },
  "containers": [
    {
      "name": "web",
      "image": "alpine-3.20-nginx",
      "ip_suffix": 10,
      "limits_cpu": "1",
      "limits_memory": "256MB"
    },
    {
      "name": "db",
      "image": "alpine-3.20-mariadb",
      "ip_suffix": 11,
      "limits_cpu": "1",
      "limits_memory": "512MB"
    }
  ],
  "readiness_commands": [
    {
      "target": "container",
      "container_name": "web",
      "cmd": ["pgrep", "nginx"],
      "timeout": 5,
      "interval": 2,
      "retry_timeout": 15
    }
  ],
  "verify_commands": [
    {
      "target": "verifier",
      "cmd": ["curl", "-sf", "http://10.1.1.10/login"],
      "timeout": 10
    }
  ]
}
```
CONSTRAINTS:
- `team_k` IS TAKEN FROM URL PATH (NOT REQUIRED IN BODY).
- `container.name`: REGEX `^[a-z0-9][a-z0-9-]{0,55}$` (MAX 56 CHARS).
- `container.ip_suffix`: INT `2..254` (UNIQUE PER CHALLENGE TOPOLOGY).
- `verifier.image`: MUST EXIST AND BE READY IN DISTROBUILDER CATALOG.
- `limits_memory`: FORMAT `"256MB"`, `"1GB"`.

## RESPONSE
STATUS: `201 Created`
CONTENT-TYPE: `application/json`
BODY SCHEMA (`DeployResponse` / `ActiveChallengeRecord`):
```json
{
  "team_k": 1,
  "challenge_id": "web-vuln-01",
  "status": "running",
  "deployed_at": "2026-10-12T15:15:00Z",
  "containers": [
    {
      "instance_name": "c-1-web",
      "team_k": 1,
      "spec_name": "web",
      "ip_address": "10.1.1.10",
      "image_alias": "alpine-3.20-nginx",
      "challenge_id": "web-vuln-01",
      "created_at": "2026-10-12T15:15:00Z"
    },
    {
      "instance_name": "c-1-db",
      "team_k": 1,
      "spec_name": "db",
      "ip_address": "10.1.1.11",
      "image_alias": "alpine-3.20-mariadb",
      "challenge_id": "web-vuln-01",
      "created_at": "2026-10-12T15:15:02Z"
    }
  ],
  "step_timings": {
    "validation_sec": 0.005,
    "total_create_sec": 0.45,
    "total_push_sec": 0.12,
    "total_start_sec": 0.38,
    "total_runtime_net_sec": 0.05,
    "total_db_record_sec": 0.01,
    "total_deploy_sec": 1.02
  }
}
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: TEAM NOT FOUND -> PROVISION TEAM FIRST VIA `PUT /teams/{team_k}`.
- `409 Conflict`: `BridgeNotActiveError` -> TEAM BRIDGE MISSING / UNPLUGGED.
- `422 Unprocessable Entity`: `ImageNotReadyError` -> SPECIFIED CONTAINER OR VERIFIER IMAGE NOT BUILT IN IMAGES CATALOG.
- `502 Bad Gateway`: `DeploymentRollbackError` -> CONTAINER FAILED TO START, SYSTEM ATOMICALLY ROLLED BACK CLEANLY.
- `503 Service Unavailable`: TEAM RESOURCE LOCK TIMEOUT -> CONCURRENT OPERATION ACTIVE, RETRY.
- `500 Internal Server Error`: DEPLOYMENT FAILURE.
