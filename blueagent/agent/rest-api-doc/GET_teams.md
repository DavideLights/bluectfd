# GET /teams

## ENDPOINT & METHOD
`GET /teams`

## PURPOSE & CTFD CONTEXT
LIST ALL REGISTERED/PROVISIONED TEAMS IN BLUEAGENT.
USE CASE: POPULATE CTFD ADMIN DASHBOARD TEAMS TABLE, RECONCILE CTFD DB WITH BLUEAGENT REALITY.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
HEADERS: `Authorization: ...`
PARAMS: NONE.
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA: `List[TeamRecord]`
```json
[
  {
    "team_k": 1,
    "name": "Team Alpha",
    "bridge_name": "br-team-1",
    "wg_interface": "wg-1",
    "created_at": "2026-10-12T15:00:00Z"
  },
  {
    "team_k": 2,
    "name": "Team Bravo",
    "bridge_name": "br-team-2",
    "wg_interface": "wg-2",
    "created_at": "2026-10-12T15:05:00Z"
  }
]
```

## ERROR HANDLING
- `401 Unauthorized`: TOKEN INVALID.
- `500 Internal Server Error`: DATABASE QUERY ERROR.
