# GET /images/{alias}

## ENDPOINT & METHOD
`GET /images/{alias}`

## PURPOSE & CTFD CONTEXT
RETRIEVE STATUS, BUILD FINGERPRINT, AND DETAILS FOR SPECIFIC IMAGE ALIAS.
USE CASE: CTFD FRONTEND AJAX POLLS STATUS AFTER ADMIN SUBMITS DISTROBUILDER YAML/ARCHIVE UNTIL `status == 'ready'`.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETER:
- `alias` (STR, REQUIRED): TARGET IMAGE ALIAS.
HEADERS: `Authorization: ...`
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA (`ImageRecord`):
```json
{
  "alias": "alpine-3.20-nginx",
  "yaml_sha256": "e3b0c442...",
  "yaml_text": "image:\n  description: ...",
  "status": "ready",
  "fingerprint": "a1b2c3d4e5f6...",
  "error": null,
  "created_at": "2026-10-12T15:20:00Z",
  "updated_at": "2026-10-12T15:22:00Z"
}
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: IMAGE ALIAS NOT REGISTERED -> TRIGGER BUILD VIA `PUT /images/{alias}`.
- `401 Unauthorized`: TOKEN INVALID.
