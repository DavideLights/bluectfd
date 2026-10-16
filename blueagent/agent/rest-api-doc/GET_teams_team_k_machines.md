# GET /teams/{team_k}/machines

## ENDPOINT & METHOD
`GET /teams/{team_k}/machines`

## PURPOSE & CTFD CONTEXT
LIST ACTIVE RUNNING CONTAINERS DEPLOYED FOR TEAM:
RETRIEVES INSTANCE NAMES, ASSIGNED STATIC IPS, CONTAINER SPEC NAMES, IMAGE ALIASES.
USE CASE: POPULATE CTFD CHALLENGE MODAL TARGET TABLE (SHOW PLAYERS WHERE APPLICATION SERVICES RUNNING: `10.k.1.x`). POPULATE ADMIN CONTAINER MANAGEMENT VIEW.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETER:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
HEADERS: `Authorization: ...`
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA: `List[RunningContainerRecord]`
```json
[
  {
    "instance_name": "c-1-web",
    "team_k": 1,
    "spec_name": "web",
    "ip_address": "10.1.1.10",
    "image_alias": "alpine-3.20-nginx",
    "challenge_id": "web-vuln-01",
    "created_at": "2026-10-12T15:15:00Z"
  }
]
```

## ERROR CODES
- `404 Not Found`: TEAM NOT FOUND.
- `401 Unauthorized`: TOKEN INVALID.
- `500 Internal Server Error`: ENGINE/DB QUERY FAILURE.
