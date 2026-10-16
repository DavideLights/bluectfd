#!/usr/bin/env python3
"""
sync_context.py - Synchronize and update context.md across all project chats.

This script queries the Antigravity conversation database and transcript logs,
extracts user intents, actions, environmental variables, and progress across
all conversations belonging to the project workspace, and updates context.md.
"""

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def get_default_paths():
    home = os.path.expanduser("~")
    gemini_dir = os.path.join(home, ".gemini", "antigravity")
    db_path = os.path.join(gemini_dir, "conversation_summaries.db")
    brain_dir = os.path.join(gemini_dir, "brain")
    return db_path, brain_dir


def find_conversations(db_path, workspace_path, limit=10):
    if not os.path.exists(db_path):
        return []

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    norm_ws = os.path.abspath(workspace_path).rstrip("/")
    query = """
        SELECT conversation_id, title, preview, last_modified_time, last_user_input_time, step_count, workspace_uris
        FROM conversation_summaries
        WHERE workspace_uris LIKE ? OR workspace_uris LIKE ?
        ORDER BY last_modified_time DESC
        LIMIT ?
    """
    pattern1 = f"%{norm_ws}%"
    pattern2 = f"%{norm_ws.replace('/', '%2F')}%"

    cur.execute(query, (pattern1, pattern2, limit))
    rows = cur.fetchall()

    # If no exact match, fallback to checking all recent conversations
    if not rows:
        cur.execute(
            """
            SELECT conversation_id, title, preview, last_modified_time, last_user_input_time, step_count, workspace_uris
            FROM conversation_summaries
            ORDER BY last_modified_time DESC
            LIMIT ?
        """,
            (limit,),
        )
        rows = cur.fetchall()

    conn.close()
    return rows


def parse_transcript(transcript_path):
    if not os.path.exists(transcript_path):
        return None

    user_requests = []
    executed_commands = []
    ssh_details = []
    ips = set()
    ports = set()
    files_created = set()
    files_modified = set()
    files_read = set()
    tokens_discovered = set()
    scripts_created = set()
    final_messages = []

    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                step = json.loads(line)
            except Exception:
                continue

            stype = step.get("type")
            content = step.get("content") or ""

            if stype == "USER_INPUT":
                if "<USER_REQUEST>" in content:
                    req = (
                        content.split("<USER_REQUEST>")[1]
                        .split("</USER_REQUEST>")[0]
                        .strip()
                    )
                else:
                    req = content.strip()
                if req:
                    user_requests.append(req)

            elif stype == "PLANNER_RESPONSE":
                if content and len(content.strip()) > 30:
                    # Clean out thinking or meta tags
                    cleaned_content = re.sub(
                        r"<thinking>.*?</thinking>", "", content, flags=re.DOTALL
                    ).strip()
                    if cleaned_content:
                        final_messages.append(cleaned_content)

            for tc in step.get("tool_calls", []):
                tname = tc.get("name")
                args = tc.get("args", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                if not isinstance(args, dict):
                    continue

                if tname == "run_command":
                    cmd = args.get("CommandLine", "").strip('\"\'')
                    if cmd:
                        executed_commands.append(cmd)

                        # SSH connection detection
                        if "ssh" in cmd:
                            ssh_details.append(cmd)
                            # Extract user@ip
                            match_user_ip = re.findall(
                                r"([a-zA-Z0-9_-]+)@((?:\d{1,3}\.){3}\d{1,3})", cmd
                            )
                            for u, ip in match_user_ip:
                                ssh_details.append(f"{u}@{ip}")

                        # IP detection (filter out common masks/loopbacks)
                        raw_ips = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", cmd)
                        for ip in raw_ips:
                            if not ip.startswith("127.") and ip not in (
                                "0.0.0.0",
                                "255.255.255.0",
                                "255.255.0.0",
                            ):
                                ips.add(ip)

                        # Port detection
                        port_matches = re.findall(
                            r":([4-9]\d{3}|[1-3]\d{4}|5182\d)", cmd
                        )
                        for p in port_matches:
                            ports.add(p)

                        # Token detection
                        tok_matches = re.findall(
                            r"([A-Z0-9_]*TOKEN[A-Z0-9_]*)=([^\s\"\'\\]+)", cmd
                        )
                        for k, v in tok_matches:
                            tokens_discovered.add(f"{k}={v}")

                        # Script creation in bash
                        script_matches = re.findall(
                            r">+\s*([/\w\.\-]+\.(?:sh|py|bash))", cmd
                        )
                        for s in script_matches:
                            scripts_created.add(s)

                elif tname == "write_to_file":
                    tf = (
                        args.get("TargetFile") or args.get("AbsolutePath") or ""
                    ).strip('\"\'')
                    if tf:
                        files_created.add(tf)

                elif tname == "replace_file_content":
                    tf = (
                        args.get("TargetFile") or args.get("AbsolutePath") or ""
                    ).strip('\"\'')
                    if tf:
                        files_modified.add(tf)

                elif tname == "view_file":
                    tf = (
                        args.get("TargetFile") or args.get("AbsolutePath") or ""
                    ).strip('\"\'')
                    if tf:
                        files_read.add(tf)

    return {
        "user_requests": user_requests,
        "executed_commands": executed_commands,
        "ssh_details": list(dict.fromkeys(ssh_details)),
        "ips": sorted(list(ips)),
        "ports": sorted(list(ports)),
        "tokens": sorted(list(tokens_discovered)),
        "scripts_created": sorted(list(scripts_created)),
        "files_created": sorted(list(files_created)),
        "files_modified": sorted(list(files_modified)),
        "files_read": sorted(list(files_read)),
        "final_summary": final_messages[-1] if final_messages else None,
    }


def generate_sync_markdown(chats_data, workspace_path):
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Aggregate discovered infrastructure
    all_ips = set()
    all_ssh = set()
    all_ports = set()
    all_tokens = set()
    all_scripts = set()

    for item in chats_data:
        p = item.get("parsed") or {}
        all_ips.update(p.get("ips", []))
        all_ssh.update(p.get("ssh_details", []))
        all_ports.update(p.get("ports", []))
        all_tokens.update(p.get("tokens", []))
        all_scripts.update(p.get("scripts_created", []))

    md = []
    md.append("<!-- BEGIN CROSS-CHAT SYNC -->")
    md.append(f"## Cross-Chat Multi-Session Synchronization")
    md.append(
        f"*Last Synchronized: {now_str} across {len(chats_data)} active project chats*\n"
    )

    # 1. Discovered Infrastructure & Runtime Parameters
    md.append("### 1. Discovered Infrastructure & Runtime Parameters")
    md.append(
        "Key environments, endpoints, and credentials identified across all project chats:\n"
    )
    md.append("| Category | Value | Notes / Origin |")
    md.append("| :--- | :--- | :--- |")

    if all_ips:
        for ip in sorted(all_ips):
            md.append(f"| **Target Host / VM IP** | `{ip}` | Discovered in SSH commands / network probes |")
    else:
        md.append("| **Target Host / VM IP** | `None detected` | N/A |")

    # Filter clean ssh users
    clean_ssh_targets = set()
    for s in all_ssh:
        m = re.search(r"([a-zA-Z0-9_-]+@(?:\d{1,3}\.){3}\d{1,3})", s)
        if m:
            clean_ssh_targets.add(m.group(1))
    if clean_ssh_targets:
        for target in sorted(clean_ssh_targets):
            md.append(f"| **SSH Target** | `{target}` | SSH access (e.g., `ssh {target}`) |")

    if all_ports:
        ports_desc = []
        for port in sorted(all_ports):
            desc = f"`{port}`"
            if port == "4000":
                desc += " (CTFd Web UI)"
            elif port == "8000":
                desc += " (BlueAgent FastAPI REST API)"
            elif port.startswith("5182"):
                desc += " (WireGuard VPN port)"
            ports_desc.append(desc)
        md.append(f"| **Service Ports** | {', '.join(ports_desc)} | Active network service listeners |")

    if all_tokens:
        for t in sorted(all_tokens):
            md.append(f"| **Discovered Auth / Token** | `{t}` | Environment token configured |")

    if all_scripts:
        for sc in sorted(all_scripts):
            md.append(f"| **Automation Script** | `{sc}` | Multi-service management script |")

    md.append("\n---\n")

    # 2. Multi-Chat Session Directory
    md.append("### 2. Multi-Chat Session Directory")
    md.append(
        "Summary of all conversation sessions associated with this workspace:\n"
    )
    md.append(
        "| Conversation ID | Chat Title | Last Activity | Primary Objective / Request | Status |"
    )
    md.append(
        "| :--- | :--- | :--- | :--- | :--- |"
    )

    for item in chats_data:
        cid = item["id"]
        short_id = cid[:8]
        title = item["title"]
        lmt = item["last_modified"] or "Unknown"
        p = item.get("parsed") or {}
        reqs = p.get("user_requests", [])
        primary_req = reqs[0] if reqs else (item.get("preview") or "No prompt")
        # Sanitize single line
        primary_req = " ".join(primary_req.split())
        if len(primary_req) > 75:
            primary_req = primary_req[:72] + "..."

        # Determine status
        status = "Completed / Stable"
        if "test" in title.lower() or "plan" in title.lower():
            status = "Testing / Validation"
        elif "backup" in title.lower():
            status = "Backup & Repo Tracking"
        elif "context" in title.lower() or "skill" in title.lower():
            status = "Skill Active"

        md.append(
            f"| `{short_id}` | **{title}** | {lmt[:19]} | {primary_req} | `{status}` |"
        )

    md.append("\n---\n")

    # 3. Detailed Per-Chat Findings and State Changes
    md.append("### 3. Detailed Cross-Chat Activity & Progress Log\n")

    for idx, item in enumerate(chats_data, 1):
        cid = item["id"]
        title = item["title"]
        lmt = item["last_modified"] or ""
        p = item.get("parsed") or {}

        md.append(f"#### Session {idx}: {title} (`{cid[:8]}`)")
        md.append(f"- **Last Updated:** {lmt}")

        reqs = p.get("user_requests", [])
        if reqs:
            md.append("- **User Goals:**")
            for r in reqs[:2]:
                r_clean = " ".join(r.split())
                if len(r_clean) > 160:
                    r_clean = r_clean[:157] + "..."
                md.append(f"  - \"{r_clean}\"")

        # Key actions / commands
        cmds = p.get("executed_commands", [])
        interesting_cmds = [
            c
            for c in cmds
            if any(
                k in c.lower()
                for k in [
                    "ssh",
                    "pytest",
                    "git",
                    "systemctl",
                    "incus",
                    "docker",
                    "python",
                    "start_",
                ]
            )
        ]
        if interesting_cmds:
            md.append("- **Key Executions & Probes:**")
            for ic in interesting_cmds[:4]:
                ic_clean = " ".join(ic.split())
                if len(ic_clean) > 120:
                    ic_clean = ic_clean[:117] + "..."
                md.append(f"  - `{ic_clean}`")

        # Files touched
        files_mod = (p.get("files_created", []) or []) + (
            p.get("files_modified", []) or []
        )
        if files_mod:
            md.append(
                f"- **Files Created/Modified:** {', '.join([f'`{os.path.basename(f)}`' for f in files_mod[:5]])}"
            )

        # Summary
        if p.get("final_summary"):
            summary_snippet = " ".join(p["final_summary"].split())
            if len(summary_snippet) > 200:
                summary_snippet = summary_snippet[:197] + "..."
            md.append(f"- **Outcome/Takeaway:** {summary_snippet}")

        md.append("")

    # 4. Open Tasks & Next Steps Across Chats
    md.append("### 4. Consolidated Open Tasks & Multi-Chat Next Steps")
    md.append(
        "- [x] Extract and synchronize context across existing project chats."
    )
    md.append(
        "- [ ] Test end-to-end integration between CTFd and BlueAgent on Debian VM (`10.226.253.215`)."
    )
    md.append(
        "- [ ] Verify service automation script (`/home/debian/bluectfd/start_services.sh`) under background operation."
    )
    md.append(
        "- [ ] Ensure git version control and backup state across both local workspace and remote VM."
    )
    md.append(
        "- [ ] Keep `context.md` regularly updated whenever key changes or decisions occur."
    )
    md.append("<!-- END CROSS-CHAT SYNC -->\n")

    return "\n".join(md)


def update_context_file(context_path, sync_markdown, workspace_path):
    existing_content = ""
    if os.path.exists(context_path):
        with open(context_path, "r", encoding="utf-8") as f:
            existing_content = f.read()

    # If the file does not exist, look for upstream/reference context to seed it
    if not existing_content.strip():
        # Candidate references
        candidates = [
            os.path.expanduser("~/agent/context.md"),
            "/home/debian/bluectfd/context.md",
        ]
        seeded_header = ""
        for cand in candidates:
            if os.path.exists(cand):
                try:
                    with open(cand, "r", encoding="utf-8") as f:
                        cand_content = f.read()
                    if cand_content.strip():
                        seeded_header = cand_content.strip() + "\n\n---\n\n"
                        break
                except Exception:
                    pass

        if not seeded_header:
            seeded_header = f"""# Project Context: {os.path.basename(workspace_path)}

## System Overview & Architecture
Central shared project context for all active agents and chats.
"""
        new_content = seeded_header + sync_markdown
    else:
        # Check for marker replace
        pattern = r"<!-- BEGIN CROSS-CHAT SYNC -->.*?<!-- END CROSS-CHAT SYNC -->"
        if re.search(pattern, existing_content, flags=re.DOTALL):
            new_content = re.sub(
                pattern, sync_markdown, existing_content, flags=re.DOTALL
            )
        elif "## Cross-Chat Multi-Session Synchronization" in existing_content:
            idx = existing_content.find("## Cross-Chat Multi-Session Synchronization")
            new_content = existing_content[:idx].rstrip() + "\n\n" + sync_markdown
        else:
            new_content = existing_content.rstrip() + "\n\n---\n\n" + sync_markdown

    with open(context_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    return True


def main():
    parser = argparse.ArgumentParser(
        description="Synchronize context.md across all project chats."
    )
    parser.add_argument(
        "--workspace",
        "-w",
        default=os.getcwd(),
        help="Workspace directory to synchronize context for.",
    )
    parser.add_argument(
        "--context-file",
        "-o",
        default=None,
        help="Target context markdown file (default: <workspace>/context.md).",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=10,
        help="Maximum number of recent chats to analyze.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the synchronized markdown to stdout without modifying files.",
    )
    parser.add_argument(
        "--dump-json",
        action="store_true",
        help="Dump the extracted multi-chat intelligence as JSON.",
    )

    args = parser.parse_args()

    workspace_dir = os.path.abspath(args.workspace)
    context_file = args.context_file or os.path.join(workspace_dir, "context.md")

    db_path, brain_dir = get_default_paths()

    conv_rows = find_conversations(db_path, workspace_dir, limit=args.limit)
    if not conv_rows:
        print(f"No conversation history found for workspace: {workspace_dir}")
        sys.exit(0)

    chats_data = []
    for row in conv_rows:
        cid, title, preview, lmt, luit, sc, ws_uris = row
        tpath = os.path.join(
            brain_dir, cid, ".system_generated", "logs", "transcript.jsonl"
        )
        parsed = parse_transcript(tpath)
        chats_data.append(
            {
                "id": cid,
                "title": title or "Untitled Conversation",
                "preview": preview,
                "last_modified": lmt,
                "step_count": sc,
                "parsed": parsed or {},
            }
        )

    if args.dump_json:
        print(json.dumps(chats_data, indent=2))
        return

    sync_md = generate_sync_markdown(chats_data, workspace_dir)

    if args.dry_run:
        print(sync_md)
        return

    update_context_file(context_file, sync_md, workspace_dir)
    print(f"Successfully synchronized {len(chats_data)} chats into: {context_file}")


if __name__ == "__main__":
    main()
