# GET /teams/{team_k}/members

## ENDPOINT & METHOD
`GET /teams/{team_k}/members`

## PURPOSE & CTFD CONTEXT
LIST ALL REGISTERED WIREGUARD PEERS/MEMBERS FOR TEAM WITH REAL-TIME TELEMETRY:
RETRIEVES MEMBER METADATA AND QUERIES WIREGUARD KERNEL STATUS TO RETURN:
- MEMBER IDENTIFIER, USER ID, NAME, CLIENT IP, PUBLIC KEY.
- REAL-TIME TELEMETRY: `online` STATUS, `endpoint`, `latest_handshake`, `transfer_rx`, `transfer_tx`, `persistent_keepalive`.
(SENSITIVE CREDENTIALS MASKED: `private_key` & `preshared_key` OMITTED).
USE CASE: CTFD ADMIN DASHBOARD AUDIT OF REGISTERED TEAM VPN CLIENTS, MONITORING REAL-TIME VPN CONNECTION STATUS AND BANDWIDTH.

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
BODY SCHEMA: `List[TeamMemberResp]`
```json
[
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
]
```

## ERROR CODES
- `404 Not Found`: TEAM DOES NOT EXIST.
- `401 Unauthorized`: TOKEN INVALID.
