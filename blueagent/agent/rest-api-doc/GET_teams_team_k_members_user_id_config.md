# GET /teams/{team_k}/members/{user_id}/config

## ENDPOINT & METHOD
`GET /teams/{team_k}/members/{user_id}/config`

## PURPOSE & CTFD CONTEXT
RETRIEVE RAW PLAINTEXT WIREGUARD `.conf` CONFIGURATION TEXT FOR DOWNLOAD:
READS ENCRYPTED PRIVATE KEY AND PEER SETTINGS FROM DATABASE, RENDERS CLIENT CONFIGURATION FILE.
USE CASE: STREAM FILE DOWNLOAD DIRECTLY TO CTFD PLAYER BROWSER: `team-k-<username>.conf`.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETERS:
- `team_k` (INT, REQUIRED, 0..254): TARGET TEAM INDEX.
- `user_id` (STR, REQUIRED): CTFD USER ID OR MEMBER IDENTIFIER.
HEADERS: `Authorization: ...`
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `text/plain; charset=utf-8`
BODY: RAW WIREGUARD CONFIGURATION STRING:
```ini
[Interface]
PrivateKey = aBcDeF123...
Address = 10.1.0.2/24
DNS = 10.1.0.1

[Peer]
PublicKey = gHiJkL456...
Endpoint = 198.51.100.1:51821
AllowedIPs = 10.1.0.0/16
PersistentKeepalive = 25
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: MEMBER OR TEAM NOT FOUND -> RE-RUN `PUT /teams/{team_k}/members/{user_id}` TO PROVISION.
- `401 Unauthorized`: TOKEN INVALID.
