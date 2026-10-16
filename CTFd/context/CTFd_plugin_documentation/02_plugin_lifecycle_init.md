# BlueAgent CTFd Plugin: Lifecycle & Initialization

## Entrypoint: `CTFd/CTFd/plugins/blueagent/__init__.py`

CTFd plugin loader discovers `blueagent` directory on startup and invokes `load(app)`.

```python
def load(app):
    with app.app_context():
        # 1. Initialize plugin database tables
        app.db.create_all()

        # 2. Register custom challenge type
        CHALLENGE_CLASSES["blueagent_patch"] = BlueAgentPatchChallengeType

        # 3. Register Flask Blueprint
        app.register_blueprint(blueagent_bp)

        # 4. Register static assets
        register_plugin_assets_directory(app, base_path="/plugins/blueagent/assets/")

        # 5. Register admin menu item
        register_admin_plugin_menu_bar(title="BlueAgent", route="/admin/plugins/blueagent")
```

## Initialization Sequence

```
Flask App Creation (CTFd/wsgi.py or CTFd/__init__.py)
  │
  ├── CTFd.plugins.init_plugins(app)
  │     │
  │     └── import CTFd.plugins.blueagent -> invoke load(app)
  │           │
  │           ├── app.db.create_all()
  │           │     Creates 4 tables if not existing:
  │           │     - blueagent_challenges (polymorphic extension of challenges)
  │           │     - blueagent_team_mappings (team_k allocations)
  │           │     - blueagent_deployments (container status per team)
  │           │     - blueagent_verify_logs (audit logs)
  │           │
  │           ├── CHALLENGE_CLASSES["blueagent_patch"] = BlueAgentPatchChallengeType
  │           │     Enables "blueagent_patch" in /admin/challenges/new dropdown
  │           │     and /api/v1/challenges/types API
  │           │
  │           ├── app.register_blueprint(blueagent_bp)
  │           │     Hooks player APIs, admin APIs, and asset routing
  │           │
  │           ├── register_plugin_assets_directory(app, base_path="/plugins/blueagent/assets/")
  │           │     Maps static file handler to /plugins/blueagent/assets/<filename>
  │           │
  │           └── register_admin_plugin_menu_bar(title="BlueAgent", route="/admin/plugins/blueagent")
  │                 Injects link into admin dashboard top navigation
```

## Teardown & Hot Reloading

- Database tables persist across reboots (`create_all()` is idempotent).
- Static assets reload automatically without container rebuild when volume-mounted into `/opt/CTFd/CTFd/plugins/blueagent`.
- In-memory lock fallbacks reset on worker restart; Redis cache preserves active cooldown keys.
