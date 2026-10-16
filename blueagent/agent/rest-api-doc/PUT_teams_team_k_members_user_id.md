# PUT /teams/{team_k}/members/{user_id}

## ENDPOINT & METHOD
`PUT /teams/{team_k}/members/{user_id}`

## PURPOSE & CTFD CONTEXT
REGISTER OR UPDATE INDIVIDUAL WIREGUARD VPN PROFILE FOR TEAM MEMBER:
1. ALLOCATES NEXT AVAILABLE CLIENT IP ON `10.k.0.0/24` (RANGE `10.k.0.2`..`10.k.0.254`).
2. GENERATES CURVE25519 KEYPAIR (OR ACCEPTS CLIENT `public_key`).
3. REGISTERS PEER IN WIREGUARD KERNEL MODULE VIA `wg set`.
4. GENERATES FULL WIREGUARD `.conf` CLIENT TEXT.
USE CASE: PLAYER CLICKS "DOWNLOAD VPN" BUTTON IN CTFD UI. IDEMPOTENT: IF MEMBER ALREADY REGISTERED, RETURNS EXISTING RECORD WITH SAVED CONFIG.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETERS:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
- `user_id` (STR, REQUIRED): CTFD USER ID OR IDENTIFIER FOR PLAYER.
HEADERS: `Content-Type: application/json`
BODY SCHEMA (`TeamMemberPutReq`):
```json
{
  "name": "alice",
  "public_key": null
}
```
FIELDS:
- `name` (STR, REQUIRED, 1..128 CHARS): PLAYER USERNAME.
- `public_key` (STR, OPTIONAL): OPTIONAL CLIENT-GENERATED KEY; IF NULL, BLUEAGENT GENERATES PRIVATE/PUBLIC PAIR.

## RESPONSE
STATUS:
- `201 Created`: NEW MEMBER CREATED & REGISTERED.
- `200 OK`: MEMBER ALREADY REGISTERED (RETURNS PERSISTED CONFIG).
CONTENT-TYPE: `application/json`
BODY SCHEMA (`TeamMemberWG`):
```json
{
  "member_id": "ctfd-1-42",
  "team_k": 1,
  "name": "alice",
  "client_ip": "10.1.0.2/24",
  "public_key": "xYz123...",
  "ctfd_user_id": 42,
  "config_text": "[Interface]\nPrivateKey = ...\nAddress = 10.1.0.2/24\nDNS = 10.1.0.1\n\n[Peer]\nPublicKey = ...\nEndpoint = 203.0.113.1:51821\nAllowedIPs = 10.1.0.0/16\nPersistentKeepalive = 25\n",
  "created_at": "2026-10-12T15:10:00Z"
}
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: TEAM NOT FOUND -> PROVISION TEAM FIRST VIA `PUT /teams/{team_k}`.
- `409 Conflict`:
  * `MemberAlreadyAssignedError`: USER ASSIGNED TO DIFFERENT TEAM -> ADMIN RESOLUTION.
  * `TeamAddressSpaceExhaustedError`: SUBNET EXHAUSTED (>253 MEMBERS ON SINGLE TEAM).
  * `MemberConflictError`: ADDRESS OR KEY CONFLICT.
- `422 Unprocessable Entity`: MISSING FIELDS OR INVALID PARAMETERS.
- `503 Service Unavailable`: LOCK TIMEOUT ACQUIRING TEAM WG LOCK -> RETRY SHORTLY.
- `500 Internal Server Error`: DATABASE OR WIREGUARD PEER REGISTRATION FAILURE.
