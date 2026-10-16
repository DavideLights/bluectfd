# GET /teams/{team_k}

## ENDPOINT & METHOD
`GET /teams/{team_k}`

## PURPOSE & CTFD CONTEXT
RETRIEVE SPECIFIC TEAM NETWORK PARAMETERS & METADATA.
USE CASE: VERIFY TEAM PROVISIONED BEFORE DEPLOYING CHALLENGE OR PROVISIONING WIREGUARD PEERS.

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
BODY SCHEMA (`TeamRecord`):
```json
{
  "team_k": 1,
  "name": "Team Alpha",
  "bridge_name": "br-team-1",
  "wg_interface": "wg-1",
  "created_at": "2026-10-12T15:00:00Z"
}
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: TEAM NOT PROVISIONED IN BLUEAGENT -> CTFD PLUGIN CAN TRIGGER AUTO LAZY `PUT /teams/{team_k}`.
- `401 Unauthorized`: TOKEN INVALID.
