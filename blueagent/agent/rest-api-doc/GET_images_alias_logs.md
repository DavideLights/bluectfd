# GET /images/{alias}/logs

## ENDPOINT & METHOD
`GET /images/{alias}/logs`

## PURPOSE & CTFD CONTEXT
RETRIEVE REAL-TIME OR COMPLETED COMPILATION LOGS FOR DISTROBUILDER IMAGE BUILD:
STREAMS STDOUT AND STDERR FROM `distrobuilder build-incus` PROCESS AS RECORDED IN REAL-TIME TO `{image_build_dir}/{alias}.log`.
USE CASE: CTFD ADMIN DASHBOARD SHOWS REAL-TIME TERMINAL LOG VIEWER DURING IMAGE BUILDS AND ALLOWS DEBUGGING FAILED PACKAGE INSTALLS OR ROOTFS ERRORS.

## AUTHENTICATION
REQUIRED: `Authorization: Bearer <token>` OR `X-CTFd-Token: <token>`.

## REQUEST
PATH PARAMETER:
- `alias` (STR, REQUIRED): TARGET IMAGE ALIAS.
HEADERS: `Authorization: ...`
BODY: NONE.

## RESPONSE
STATUS: `200 OK`
CONTENT-TYPE: `text/plain; charset=utf-8`
BODY: RAW COMPILATION LOG STREAM:
```text
=== Build started for image 'alpine-3.20-nginx' ===
Extracting build context archive into /var/lib/blueagent/builds/build-alpine-3.20-nginx-xyz...
Executing: distrobuilder build-incus definition.yaml --type=unified
Downloading rootfs...
Validating image...
Importing image tarball incus.tar.xz into Incus...
Imported fingerprint: a1b2c3d4e5f6...
Creating Incus alias 'alpine-3.20-nginx' -> a1b2c3d4e5f6...
=== Image 'alpine-3.20-nginx' successfully built and marked ready ===
```

## ERROR CODES & CTFD ACTION
- `404 Not Found`: IMAGE ALIAS NOT REGISTERED IN DATABASE.
- `401 Unauthorized`: TOKEN INVALID.
