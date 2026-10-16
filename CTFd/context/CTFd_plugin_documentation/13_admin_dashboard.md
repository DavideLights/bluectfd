# BlueAgent CTFd Plugin: Admin Dashboard & Control Center

## Template: `CTFd/CTFd/plugins/blueagent/templates/blueagent/admin_dashboard.html`
## Route: `/admin/plugins/blueagent`

Central management console extending `admin/base.html`.

---

## 1. Dashboard Sections

```
+-------------------------------------------------------------------------+
| [BlueAgent Control Center]                Health: [● ONLINE (Ready)]    |
+-------------------------------------------------------------------------+
| Section 1: Teams & WireGuard Topology                                   |
| [Team Name]  [team_k]  [VPN Subnet]   [Bridge]     [Status]   [Actions] |
| AlphaSec     1         10.1.0.0/24    br-team-1    Active     [Peers]   |
|                                                               [Purge]   |
+-------------------------------------------------------------------------+
| Section 2: Distrobuilder Image Catalog                                  |
| [Alias]             [Status]    [Created]       [Actions]               |
| alpine-nginx-sec    Ready       2026-10-12      [View Logs] [Delete]    |
| [ + Build New Image (YAML / Context Archive) ]                          |
+-------------------------------------------------------------------------+
| Section 3: Bulk Challenge Deployer                                      |
| Select Challenge: [Demo Web Patch ▼]    [ Deploy to All Active Teams ]  |
+-------------------------------------------------------------------------+
| Section 4: Live Verification Audit Log                                  |
| [Timestamp]  [Team]     [Challenge]     [Duration]  [Verdict] [Details] |
| 05:14:02     AlphaSec   Web Patch       1820ms      PASSED    [View]    |
+-------------------------------------------------------------------------+
```

---

## 2. Core Administrative Operations

### WireGuard Peer Telemetry
Clicking **"Peers"** fetches `GET /admin/plugins/blueagent/teams/<team_k>/peers` and opens a modal displaying:
- Public key fingerprint
- Client assigned tunnel IP (`10.k.0.x`)
- Time elapsed since last handshake (green if `< 3m`, gray if offline)
- Total Rx / Tx transfer volume
- Reset Peer button (deletes peer from kernel interface)

### Image Catalog & Build Log Streaming
- Admins can upload `definition.yaml` or a `.tar.gz` build context directly through the dashboard.
- Clicking **"View Logs"** queries `GET /admin/plugins/blueagent/images/<alias>/logs` to view raw Distrobuilder compiler logs in a modal terminal window, assisting in troubleshooting compilation failures.

### Atomic Team Environment Purge
The **"Purge Environment"** action:
1. Stops and deletes all active Incus containers for that team.
2. Destroys Linux bridge `br-team-k` and kernel interface `wg-k`.
3. Resets `BlueAgentTeamMapping.provisioned = False` and resets deployment states to `idle`.
