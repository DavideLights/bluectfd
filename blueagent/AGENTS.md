# BlueAgent Project Guidelines & Context Management

1. **Cross-Chat Project Context (`context.md`):**
   - Before planning or executing tasks, read `./context.md` for architecture details, discovered infrastructure (e.g. test VM IPs, SSH logins, tokens, ports), and recent progress from other chats.
   - When concluding a task, deploying services, making architectural choices, or modifying configurations, use the `update-context` skill or run `python3 .agents/skills/update-context/scripts/sync_context.py` to persist updates into `context.md` so that companion chats stay in sync.
