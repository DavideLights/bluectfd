# BlueAgent CTFd Plugin: Frontend Assets & UI Architecture

## Directory: `CTFd/CTFd/plugins/blueagent/assets/`

---

## 1. File Catalog

| File | Type | Target User | Function |
|---|---|---|---|
| `blueagent.css` | Stylesheet | All | Styling for badges, target IP cards, terminal panels, and countdown animations. |
| `create.html` | HTML Fragment | Admin | Challenge creation modal dialog with tabbed editor (spec file upload / paste). |
| `create.js` | JavaScript | Admin | Parses and pre-validates specification YAML before form submission. |
| `update.html` | HTML Fragment | Admin | Challenge update modal with config toggles and editor. |
| `update.js` | JavaScript | Admin | Handles update submissions and spec changes. |
| `view.html` | HTML Fragment | Player | Challenge modal displaying VPN instructions, target container IPs, and verify actions. |
| `view.js` | JavaScript | Player | Polling, VPN download trigger, deploy trigger, verify runner, and countdown timers. |
| `admin.js` | JavaScript | Admin | Orchestrates AJAX requests on `/admin/plugins/blueagent`. |

---

## 2. Critical UI Gotchas & Implementation Rules

### Modal Fragment Isolation
CTFd injects `create.html`, `update.html`, and `view.html` into modal containers via AJAX.
- **Do not include** `<!DOCTYPE html>`, `<html>`, `<head>`, or `<body>`.
- Templates must begin directly with `<div class="modal-dialog" role="document">`.

### CSRF Protection
CTFd rejects any state-changing HTTP request lacking a valid CSRF nonce.
- **Header**: All `fetch` and `$.ajax` calls must include:
```javascript
headers: {
  "CSRF-Token": window.init.csrfNonce,
  "Content-Type": "application/json"
}
```

### Safe Script Loading & jQuery Scoping
- In CTFd v3+, jQuery may not be bound to global `$` before deferred bundles finish loading.
- `admin_dashboard.html` loads `admin.js` with `defer`.
- Scripts wrap execution in safe initialization closures:
```javascript
(function () {
  function init() {
    if (typeof jQuery === "undefined") {
      setTimeout(init, 50);
      return;
    }
    jQuery(function ($) {
      // DOM event delegation safely bound here
      $(document).on("click", "#btn-verify-patch", function (e) { ... });
    });
  }
  init();
})();
```

---

## 3. Player View Lifecycle (`view.js`)

```
Player clicks challenge card on CTFd challenges page
  │
  ├── CTFd loads view.html fragment and evaluates view.js
  │
  ├── view.js queries GET /api/v1/plugins/blueagent/status?challenge_id=<id>
  │     ├── Renders "Connected" / "VPN Config" button
  │     ├── Renders Target Containers table:
  │     │     e.g. [web-app] -> 10.5.1.10:80
  │     └── Renders deployment state: running | idle | deploying
  │
  └── Player clicks "Verify Patch"
        ├── Disables button and displays animated spinner
        ├── Sends POST to /api/v1/plugins/blueagent/challenges/<id>/verify
        ├── On 200 OK & solved == True:
        │     Updates button to "SOLVED", shows success banner, triggers CTFd solve celebration
        ├── On 200 OK & solved == False:
        │     Displays failure alert and optional exploit error output
        └── On 429 Too Many Requests (Cooldown):
              Reads retry_after from response
              Initiates countdown timer on button (e.g. "Wait 28s...")
              Re-enables button automatically when timer reaches zero
```
