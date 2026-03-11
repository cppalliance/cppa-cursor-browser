"""Markdown export for Cursor CLI agent sessions.

Exposes ``cursor_cli_session_to_markdown`` — a reusable function that
generates a complete Markdown document (YAML frontmatter + body) from a
Cursor CLI ``store.db`` session.  The logic is extracted from
``scripts/export.py`` so it can be called programmatically without running
the full export CLI.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from utils.cli_chat_reader import traverse_blobs, messages_to_bubbles


def _slug(s: str) -> str:
    """Simple slug: collapse whitespace and special chars to dashes."""
    import re
    s = re.sub(r'[<>:"/\\|?*]', "_", s or "")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")[:80] or "untitled"


def cursor_cli_session_to_markdown(
    db_path: str | Path,
    session_meta: dict | None = None,
) -> str:
    """Generate a complete Markdown document from a Cursor CLI store.db session.

    Parameters
    ----------
    db_path:
        Path to the ``store.db`` SQLite file for the session.
    session_meta:
        Optional dict with pre-read session metadata (keys: ``agentId``,
        ``createdAt``, ``name``, ``mode``).  If omitted, metadata is read
        from ``db_path`` automatically.

    Returns
    -------
    str
        Full Markdown text including YAML frontmatter and conversation body.
    """
    db_path = Path(db_path)

    # Read metadata from the database if not provided.
    if session_meta is None:
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            conn.close()
            session_meta = json.loads(bytes.fromhex(row[0]).decode()) if row else {}
        except Exception:
            session_meta = {}

    session_id: str = session_meta.get("agentId", db_path.parent.name)
    created_ms: int = session_meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
    session_name: str = session_meta.get("name") or f"Session {session_id[:8]}"
    mode: str = session_meta.get("mode", "")

    # Reconstruct conversation.
    try:
        messages = traverse_blobs(str(db_path))
        bubbles = messages_to_bubbles(messages, created_ms)
    except Exception:
        bubbles = []

    # Derive title.
    title = session_name
    if not title or title.startswith("New Agent"):
        for b in bubbles:
            if b["type"] == "user" and b.get("text"):
                first_lines = [ln for ln in b["text"].split("\n") if ln.strip()]
                if first_lines:
                    title = first_lines[0][:100]
                    if len(title) == 100:
                        title += "..."
                break

    # Aggregate statistics.
    total_tool_calls = 0
    tool_breakdown: dict[str, int] = {}
    for b in bubbles:
        tcs = (b.get("metadata") or {}).get("toolCalls") or []
        total_tool_calls += len(tcs)
        for tc in tcs:
            tn = tc.get("name", "unknown")
            tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

    # Frontmatter.
    fm_lines = ["---"]
    fm_lines.append(f"log_id: {session_id}")
    fm_lines.append(f"log_type: cli_agent")
    fm_lines.append(f'title: "{title.replace(chr(34), chr(92) + chr(34))}"')
    fm_lines.append(
        f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}"
    )
    fm_lines.append(f"session_id: {session_id}")
    if mode:
        fm_lines.append(f"mode: {mode}")
    fm_lines.append(f"message_count: {len(bubbles)}")
    if total_tool_calls:
        fm_lines.append(f"total_tool_calls: {total_tool_calls}")
    if tool_breakdown:
        fm_lines.append("tool_call_breakdown:")
        for tn, cnt in sorted(tool_breakdown.items(), key=lambda x: -x[1]):
            fm_lines.append(f"  {tn}: {cnt}")
    fm_lines.append("---")
    fm_str = "\n".join(fm_lines) + "\n\n"

    # Header.
    header_meta_parts = [
        f"Created: {datetime.fromtimestamp(created_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}"
    ]
    if mode:
        header_meta_parts.append(f"Mode: {mode}")
    if total_tool_calls:
        header_meta_parts.append(f"Tool calls: {total_tool_calls}")
    header = f"# {title}\n\n_{' | '.join(header_meta_parts)}_\n\n---\n\n"

    # Body.
    body = ""
    for b in bubbles:
        role_label = "User" if b["type"] == "user" else "Assistant"
        body += f"### {role_label}\n\n"
        body += b.get("text", "") + "\n\n"
        tool_calls = (b.get("metadata") or {}).get("toolCalls") or []
        for tc in tool_calls:
            summary = tc.get("summary") or tc.get("name") or "unknown"
            body += f"> **Tool: {summary}**\n"
            if tc.get("input"):
                body += "> **INPUT:**\n> ```\n"
                for iline in str(tc["input"]).split("\n"):
                    body += f"> {iline}\n"
                body += "> ```\n"
            body += "\n"
        body += "---\n\n"

    return fm_str + header + body
