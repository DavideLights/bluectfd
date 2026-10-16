# GET /teams/{team_k}/members/{user_id}

## ENDPOINT & METHOD
`GET /teams/{team_k}/members/{user_id}`

## PURPOSE & CTFD CONTEXT
RETRIEVE SPECIFIC TEAM MEMBER RECORD AND REAL-TIME TELEMETRY BY USER ID OR MEMBER ID:
- RETURNS MEMBER IDENTIFIERS, ASSIGNED CLIENT IP, PUBLIC KEY.
- INCLUDES REAL-TIME `telemetry` (ONLINE STATUS, LAST HANDSHAKE, RX/TX BYTES).
- EXCLUDES PRIVATE KEYS.
USE CASE: CHECK IF SPECIFIC USER PROFILE ACTIVE AND CURRENTLY CONNECTED TO VPN.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETERS:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
- `user_id` (STR, REQUIRED): CTFD USER ID OR MEMBER IDENTIFIER (E.G. `"42"` OR `"ctfd-1-42"`).
HEADERS: `Authorization: ...`
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA (`TeamMemberResp`):
```json
{
  "user_id": 42,
  "member_id": "ctfd-1-42",
  "team_k": 1,
  "name": "alice",
  "client_ip": "10.1.0.2/24",
  "public_key": "xYz123...",
  "created_at": "2026-10-12T15:10:00Z",
  "telemetry": {
    "online": true,
    "endpoint": "198.51.100.55:41234",
    "latest_handshake": 1728745200,
    "transfer_rx": 4194304,
    "transfer_tx": 8388608,
    "persistent_keepalive": "25"
  }
}
```

## ERROR CODES
- `404 Not Found`: MEMBER OR TEAM NOT FOUND.
- `401 Unauthorized`: TOKEN INVALID.
