# GET /images

## ENDPOINT & METHOD
`GET /images`

## PURPOSE & CTFD CONTEXT
LIST ALL REGISTERED DISTROBUILDER IMAGES AND THEIR CURRENT BUILD STATES (`building`, `ready`, `failed`).
USE CASE: POPULATE IMAGE CATALOG TAB IN CTFD ADMIN DASHBOARD, PREVENT DEPLOYING CHALLENGES WITH UNREADY IMAGES.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
HEADERS: `Authorization: ...`
PARAMS: NONE.
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `application/json`
BODY SCHEMA: `List[ImageRecord]`
```json
[
  {
    "alias": "alpine-3.20-nginx",
    "yaml_sha256": "e3b0c442...",
    "status": "ready",
    "fingerprint": "a1b2c3d4e5f6...",
    "error": null,
    "created_at": "2026-10-12T15:20:00Z",
    "updated_at": "2026-10-12T15:22:00Z"
  },
  {
    "alias": "debian-12-custom",
    "yaml_sha256": "f4c8996f...",
    "status": "building",
    "fingerprint": null,
    "error": null,
    "created_at": "2026-10-12T15:25:00Z",
    "updated_at": "2026-10-12T15:25:00Z"
  }
]
```

## ERROR CODES
- `401 Unauthorized`: TOKEN INVALID.
