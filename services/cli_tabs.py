from __future__ import annotations

from datetime import datetime

from flask import current_app, jsonify

from utils.cli_chat_reader import list_cli_projects, messages_to_bubbles, traverse_blobs
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.workspace_path import get_cli_chats_path


def _get_cli_workspace_tabs(workspace_id: str):
    """Return tabs for a Cursor CLI project (workspace_id starts with "cli:")."""
    try:
        project_id = workspace_id[4:]
        cli_projects = list_cli_projects(get_cli_chats_path())
        project = next((cp for cp in cli_projects if cp["project_id"] == project_id), None)
        if project is None:
            return jsonify({"error": "CLI project not found"}), 404

        rules = current_app.config.get("EXCLUSION_RULES") or []
        ws_name = project["workspace_name"] or project_id[:12]
        tabs = []

        for session in project["sessions"]:
            meta = session.get("meta", {})
            session_id = session["session_id"]
            created_ms: int = meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
            session_name = meta.get("name") or f"Session {session_id[:8]}"

            try:
                messages = traverse_blobs(session["db_path"])
            except Exception as e:
                print(f"CLI: could not read session {session_id}: {e}")
                continue

            try:
                bubbles = messages_to_bubbles(messages, created_ms)
            except Exception as e:
                print(f"CLI: could not convert session {session_id} to bubbles: {e}")
                continue
            if not bubbles:
                continue

            # Derive title from first user bubble when name is generic
            title = session_name
            if not title or title.startswith("New Agent"):
                for b in bubbles:
                    if b["type"] == "user" and b.get("text"):
                        first_lines = [l for l in b["text"].split("\n") if l.strip()]
                        if first_lines:
                            title = first_lines[0][:100]
                            if len(title) == 100:
                                title += "..."
                        break

            searchable = build_searchable_text(project_name=ws_name, chat_title=title)
            if is_excluded_by_rules(rules, searchable):
                continue

            # Aggregate metadata
            total_tool_calls = 0
            tool_breakdown: dict = {}
            for b in bubbles:
                tcs = (b.get("metadata") or {}).get("toolCalls") or []
                total_tool_calls += len(tcs)
                for tc in tcs:
                    tn = tc.get("name", "unknown")
                    tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

            tab_meta: dict | None = None
            if total_tool_calls or tool_breakdown:
                tab_meta = {"totalToolCalls": total_tool_calls or None}
                if tool_breakdown:
                    tab_meta["toolBreakdown"] = tool_breakdown

            tab = {
                "id": session_id,
                "title": title,
                "timestamp": created_ms,
                "bubbles": [
                    {
                        "type": b["type"],
                        "text": b.get("text", ""),
                        "timestamp": b.get("timestamp", created_ms),
                        **({"metadata": b["metadata"]} if b.get("metadata") else {}),
                    }
                    for b in bubbles
                ],
                "source": "cli",
            }
            if tab_meta:
                tab_meta_clean = {k: v for k, v in tab_meta.items() if v is not None}
                if tab_meta_clean:
                    tab["metadata"] = tab_meta_clean

            tabs.append(tab)

        tabs.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
        return jsonify({"tabs": tabs})

    except Exception as e:
        print(f"Failed to get CLI workspace tabs: {e}")
        return jsonify({"error": "Failed to get CLI workspace tabs"}), 500
