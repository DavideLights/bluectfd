# Project Context Specification & Template

This reference specifies the layout and standards for `context.md` files synchronized across Antigravity project conversations.

## Purpose of `context.md`
`context.md` serves as the single source of truth across all chat sessions in a project workspace. While individual chat contexts are isolated, `context.md` bridges the gap, allowing each agent session to:
1. Instantly inherit decisions and technical insights from other sessions.
2. Avoid rediscovering environment details (VM IPs, ports, tokens, running daemons).
3. Record progress, completed tasks, and blockers so companion sessions stay in sync.

---

## Standard Structure

```markdown
# [Project Name] Context & System Architecture

## 1. System Philosophy & Architecture
- Core design patterns and invariants
- Component breakdown and communication interfaces

## 2. Workspace & Monorepo Map
- Directory layout and module responsibilities
- Key files, configs, entrypoints, and documentation references

## 3. Interfaces & Contracts
- REST API endpoints and data models
- Environment variables, settings, and authorization tokens

<!-- BEGIN CROSS-CHAT SYNC -->
## Cross-Chat Multi-Session Synchronization
*(Managed automatically by the `update-context` skill)*

### 1. Discovered Infrastructure & Runtime Parameters
- Table of Target VM IPs, SSH logins, and ports
- Discovered authentication tokens and credentials
- Discovered service scripts (e.g. startup/test scripts)

### 2. Multi-Chat Session Directory
- Table of all chats: Conversation ID, Title, Last Modified, Objective, Status

### 3. Detailed Cross-Chat Activity & Progress Log
- Step-by-step summary of actions taken in each session

### 4. Consolidated Open Tasks & Multi-Chat Next Steps
- Checklist of active, completed, and pending items
<!-- END CROSS-CHAT SYNC -->
```

---

## Best Practices for Agent Updates
1. **Preserve Static Architecture:** Keep manual architectural definitions intact outside the sync markers.
2. **Atomic Markers:** Always preserve `<!-- BEGIN CROSS-CHAT SYNC -->` and `<!-- END CROSS-CHAT SYNC -->` so automated synchronization never clobbers human documentation.
3. **High Signal over Noise:** Focus on actionable commands, discovered environment parameters, and blockers.
