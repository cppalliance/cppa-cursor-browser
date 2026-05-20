"""Markdown export for Cursor chat sessions.

Two public functions:

* ``cursor_cli_session_to_markdown`` — generates a Markdown document from a
  Cursor CLI ``store.db`` session (agent/CLI chat).

* ``cursor_ide_chat_to_markdown`` — generates a Markdown document from a
  Cursor IDE composer session (global-storage ``composerData:`` entry).  The
  caller supplies the pre-loaded ``bubble_map`` and optional
  ``code_block_diff_map`` so this function never touches the database.

Both are shared between ``scripts/export.py``, ``api/export_api.py``, and any
programmatic caller.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from utils.cli_chat_reader import traverse_blobs, messages_to_bubbles
from utils.path_helpers import to_epoch_ms
from utils.text_extract import extract_text_from_bubble, slug
from utils.tool_parser import parse_tool_call


# ── CLI session exporter ─────────────────────────────────────────────────────


def cursor_cli_session_to_markdown(
    db_path: str | Path,
    session_meta: dict | None = None,
    workspace_info: dict | None = None,
    bubbles: list[dict] | None = None,
    title_override: str | None = None,
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
    workspace_info:
        Optional dict with workspace-level fields to include in frontmatter.
        Recognised keys: ``workspace`` (slug), ``workspace_name``,
        ``workspace_path``, ``project_id``.
    bubbles:
        Pre-computed bubble list from ``messages_to_bubbles()``.  When
        provided the database is not re-read, avoiding a redundant SQL query.
    title_override:
        Caller-supplied title (e.g. already derived for a filename).  When
        set, skips the first-user-message derivation heuristic.

    Returns
    -------
    str
        Full Markdown text including YAML frontmatter and conversation body.

    Raises
    ------
    Exception
        Re-raises any exception from ``traverse_blobs`` / ``messages_to_bubbles``
        so callers can detect unreadable databases rather than silently receiving
        an empty document.
    """
    db_path = Path(db_path)

    # Read metadata from the database if not provided.
    if session_meta is None:
        import sqlite3
        from contextlib import closing
        try:
            # `closing(...)` guarantees .close() on scope exit (including on
            # exception); sqlite3.Connection's own context manager only handles
            # commit/rollback, not close. See issue #17.
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            session_meta = json.loads(bytes.fromhex(row[0]).decode()) if row else {}
        except Exception:
            session_meta = {}

    session_id: str = session_meta.get("agentId", db_path.parent.name)
    created_ms: int = session_meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
    session_name: str = session_meta.get("name") or f"Session {session_id[:8]}"
    mode: str = session_meta.get("mode", "")

    # Reconstruct conversation — callers may pass pre-computed bubbles to
    # avoid a redundant DB read.  Errors propagate; caller decides how to handle.
    if bubbles is None:
        messages = traverse_blobs(str(db_path))
        bubbles = messages_to_bubbles(messages, created_ms)

    # Derive title.
    title = title_override or session_name
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

    # Frontmatter.  Free-form string scalars are serialized with json.dumps()
    # so that backslashes, newlines, and embedded quotes are all escaped safely
    # (JSON strings are a valid YAML double-quoted scalar subset).
    fm_lines = ["---"]
    fm_lines.append(f"log_id: {json.dumps(session_id, ensure_ascii=False)}")
    fm_lines.append("log_type: cli_agent")
    fm_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    fm_lines.append(
        f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}"
    )
    # Workspace-level fields (only when caller provides them).
    ws_info = workspace_info or {}
    if ws_info.get("workspace"):
        fm_lines.append(f"workspace: {ws_info['workspace']}")
    if ws_info.get("workspace_name"):
        fm_lines.append(f"workspace_name: {json.dumps(ws_info['workspace_name'], ensure_ascii=False)}")
    if ws_info.get("workspace_path"):
        fm_lines.append(f"workspace_path: {json.dumps(ws_info['workspace_path'], ensure_ascii=False)}")
    if ws_info.get("project_id"):
        fm_lines.append(f"project_id: {json.dumps(ws_info['project_id'], ensure_ascii=False)}")
    fm_lines.append(f"session_id: {json.dumps(session_id, ensure_ascii=False)}")
    if mode:
        fm_lines.append(f"mode: {json.dumps(mode, ensure_ascii=False)}")
    fm_lines.append(f"message_count: {len(bubbles)}")
    if total_tool_calls:
        fm_lines.append(f"total_tool_calls: {total_tool_calls}")
    if tool_breakdown:
        fm_lines.append("tool_call_breakdown:")
        for tn, cnt in sorted(tool_breakdown.items(), key=lambda x: -x[1]):
            fm_lines.append(f"  {json.dumps(tn, ensure_ascii=False)}: {cnt}")
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
            if tc.get("output"):
                body += "> **OUTPUT:**\n> ```\n"
                for oline in str(tc["output"]).split("\n"):
                    body += f"> {oline}\n"
                body += "> ```\n"
            body += "\n"
        body += "---\n\n"

    return fm_str + header + body


# ── IDE chat exporter ────────────────────────────────────────────────────────


def cursor_ide_chat_to_markdown(
    composer_data: dict,
    composer_id: str,
    bubble_map: dict,
    code_block_diff_map: dict | None = None,
    workspace_info: dict | None = None,
) -> str:
    """Generate a complete Markdown document from a Cursor IDE composer session.

    Parameters
    ----------
    composer_data:
        Parsed value of a ``composerData:<id>`` KV entry from global storage.
    composer_id:
        The composer UUID — used as ``log_id`` in frontmatter and as the key
        into ``code_block_diff_map``.
    bubble_map:
        Global ``{bubble_id: bubble_dict}`` map loaded from
        ``cursorDiskKV`` (see ``services.workspace_db._load_bubble_map``).
    code_block_diff_map:
        Optional ``{composer_id: [diff_dict]}`` map.  When ``None`` no code
        edit bubbles are appended.
    workspace_info:
        Optional dict with workspace display fields.  Recognised keys:
        ``ws_slug`` (str), ``ws_display_name`` (str).

    Returns
    -------
    str
        Full Markdown text including YAML frontmatter and conversation body.
    """
    cd = composer_data
    ws_info = workspace_info or {}
    ws_slug = ws_info.get("ws_slug", "other-chats")
    ws_display_name = ws_info.get("ws_display_name", "Other chats")
    diffs = (code_block_diff_map or {}).get(composer_id, [])

    title = cd.get("name") or f"Chat {composer_id[:8]}"
    model_config = cd.get("modelConfig") or {}
    model_name = model_config.get("modelName")
    updated_at = to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0
    created_ms = to_epoch_ms(cd.get("createdAt")) or updated_at or int(datetime.now().timestamp() * 1000)
    headers = cd.get("fullConversationHeadersOnly") or []

    # ── Build bubble list ─────────────────────────────────────────────────────
    bubbles: list[dict] = []
    for h in headers:
        b = bubble_map.get(h.get("bubbleId"))
        if not b:
            continue
        text = extract_text_from_bubble(b)
        has_tool = isinstance(b.get("toolFormerData"), dict)
        has_thinking = bool(b.get("thinking"))
        if not text.strip() and not has_tool and not has_thinking:
            continue
        if not text.strip() and has_tool:
            text = f"**Tool: {b['toolFormerData'].get('name', 'unknown')}**"

        btype = "user" if h.get("type") == 1 else "ai"

        thinking = None
        thinking_duration_ms = None
        if b.get("thinking"):
            thinking = (
                b["thinking"] if isinstance(b["thinking"], str)
                else (b["thinking"].get("text") if isinstance(b["thinking"], dict) else None)
            )
            thinking_duration_ms = b.get("thinkingDurationMs")

        tool_info = parse_tool_call(b["toolFormerData"]) if has_tool else None

        model_info = (b.get("modelInfo") or {}).get("modelName")
        if model_info == "default":
            model_info = None

        ctx_window = b.get("contextWindowStatusAtCreation") or {}
        ctx_tokens_used = ctx_window.get("tokensUsed", 0)
        ctx_token_limit = ctx_window.get("tokenLimit", 0)
        ctx_pct_remaining = (
            ctx_window.get("percentageRemainingFloat") or ctx_window.get("percentageRemaining")
        )

        bubbles.append({
            "type": btype,
            "text": text,
            "timestamp": (
                to_epoch_ms(b.get("createdAt"))
                or to_epoch_ms(b.get("timestamp"))
                or int(datetime.now().timestamp() * 1000)
            ),
            "tool": tool_info,
            "thinking": thinking,
            "thinkingDurationMs": thinking_duration_ms,
            "model": model_info,
            "contextTokensUsed": ctx_tokens_used if ctx_tokens_used > 0 else None,
            "contextTokenLimit": ctx_token_limit if ctx_token_limit > 0 else None,
            "contextPctRemaining": round(ctx_pct_remaining, 1) if ctx_pct_remaining else None,
        })

    # Append code-block diffs as synthetic AI bubbles.
    diff_ts = to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000)
    for d in diffs:
        bubbles.append({
            "type": "ai",
            "text": f"**Code edit:** {json.dumps(d)}",
            "timestamp": diff_ts,
        })

    bubbles.sort(key=lambda bub: bub.get("timestamp") or 0)

    # ── Compute response times ────────────────────────────────────────────────
    last_user_ts = None
    for bub in bubbles:
        if bub["type"] == "user":
            last_user_ts = bub.get("timestamp")
        elif bub["type"] == "ai" and last_user_ts:
            bts = bub.get("timestamp")
            if bts and bts > last_user_ts:
                bub["responseTimeMs"] = bts - last_user_ts

    # ── Session-level aggregates ──────────────────────────────────────────────
    total_response_ms = sum(bub.get("responseTimeMs", 0) for bub in bubbles)
    total_thinking_ms = sum(bub.get("thinkingDurationMs", 0) or 0 for bub in bubbles)
    total_tool_calls = sum(1 for bub in bubbles if bub.get("tool"))
    max_ctx_used = max((bub.get("contextTokensUsed") or 0) for bub in bubbles) if bubbles else 0
    ctx_limit = max((bub.get("contextTokenLimit") or 0) for bub in bubbles) if bubbles else 0
    lines_added = cd.get("totalLinesAdded", 0)
    lines_removed = cd.get("totalLinesRemoved", 0)

    tool_breakdown: dict[str, int] = {}
    for bub in bubbles:
        if bub.get("tool"):
            tn = bub["tool"].get("name", "unknown")
            tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

    ts_vals = [bub["timestamp"] for bub in bubbles if bub.get("timestamp")]
    wall_clock_sec = int((max(ts_vals) - min(ts_vals)) / 1000) if len(ts_vals) >= 2 else None

    # ── File / command activity ───────────────────────────────────────────────
    files_read_list: list[str] = []
    files_written_list: list[str] = []
    commands_run_list: list[str] = []
    tool_result_stats = {
        "terminal_success": 0, "terminal_error": 0,
        "file_reads": 0, "file_edits": 0,
        "searches": 0, "web": 0,
    }
    for bub in bubbles:
        if not bub.get("tool"):
            continue
        t = bub["tool"]
        tn = t.get("name", "")
        status = t.get("status") or ""
        raw_input = str(t.get("input") or "").strip()
        first_line = raw_input.split("\n")[0] if raw_input else ""
        if tn == "read_file_v2" and first_line:
            files_read_list.append(first_line)
            tool_result_stats["file_reads"] += 1
        elif tn == "edit_file_v2" and first_line:
            files_written_list.append(first_line)
            tool_result_stats["file_edits"] += 1
        elif tn == "run_terminal_command_v2" and raw_input:
            commands_run_list.append(raw_input)
            if status in ("error", "failed"):
                tool_result_stats["terminal_error"] += 1
            else:
                tool_result_stats["terminal_success"] += 1
        elif tn in ("ripgrep_raw_search", "glob_file_search", "semantic_search_full"):
            tool_result_stats["searches"] += 1
        elif tn in ("web_search", "web_fetch"):
            tool_result_stats["web"] += 1

    # ── Frontmatter ───────────────────────────────────────────────────────────
    fm_lines = ["---"]
    fm_lines.append(f"log_id: {composer_id}")
    fm_lines.append("log_type: chat")
    fm_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    fm_lines.append(f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}")
    fm_lines.append(
        f"updated_at: {datetime.fromtimestamp(updated_at / 1000).isoformat() if updated_at else datetime.now().isoformat()}"
    )
    fm_lines.append(f"workspace: {ws_slug}")
    fm_lines.append(f"workspace_name: {json.dumps(ws_display_name, ensure_ascii=False)}")
    if model_name and model_name != "default":
        fm_lines.append(f"model: {model_name}")
    fm_lines.append(f"message_count: {len(bubbles)}")
    if total_tool_calls:
        fm_lines.append(f"total_tool_calls: {total_tool_calls}")
    if tool_breakdown:
        fm_lines.append("tool_call_breakdown:")
        for tn, cnt in sorted(tool_breakdown.items(), key=lambda x: -x[1]):
            fm_lines.append(f"  {tn}: {cnt}")
    total_think = sum(1 for bub in bubbles if bub.get("thinking"))
    if total_think:
        fm_lines.append(f"thinking_count: {total_think}")
    if wall_clock_sec is not None:
        fm_lines.append(f"wall_clock_seconds: {wall_clock_sec}")
    if total_response_ms:
        fm_lines.append(f"total_response_time_sec: {total_response_ms / 1000:.1f}")
    if total_thinking_ms:
        fm_lines.append(f"total_thinking_time_sec: {total_thinking_ms / 1000:.1f}")
    if max_ctx_used and ctx_limit:
        fm_lines.append(f"max_context_tokens_used: {max_ctx_used}")
        fm_lines.append(f"context_token_limit: {ctx_limit}")
    if lines_added or lines_removed:
        fm_lines.append(f"lines_added: {lines_added}")
        fm_lines.append(f"lines_removed: {lines_removed}")
    if files_read_list or files_written_list:
        fm_lines.append(f"files_read: {len(files_read_list)}")
        fm_lines.append(f"files_written: {len(files_written_list)}")
    if commands_run_list:
        fm_lines.append(f"commands_run: {len(commands_run_list)}")
    fm_lines.append("---")
    fm_str = "\n".join(fm_lines) + "\n\n"

    # ── Document header ───────────────────────────────────────────────────────
    header = f"# {title}\n\n"
    meta_parts: list[str] = []
    if created_ms:
        meta_parts.append(f"Created: {datetime.fromtimestamp(created_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}")
    if model_name and model_name != "default":
        meta_parts.append(f"Model: {model_name}")
    if total_tool_calls:
        meta_parts.append(f"Tool calls: {total_tool_calls}")
    if wall_clock_sec is not None:
        hrs, rem = divmod(wall_clock_sec, 3600)
        mins, secs = divmod(rem, 60)
        dur = f"{hrs}h {mins}m" if hrs else (f"{mins}m {secs}s" if mins else f"{secs}s")
        meta_parts.append(f"Duration: {dur}")
    header += f"_{' | '.join(meta_parts)}_\n\n---\n\n" if meta_parts else "---\n\n"

    # ── Session summary block ─────────────────────────────────────────────────
    summary = ""
    if files_read_list or files_written_list or commands_run_list:
        summary += "## Session Summary\n\n"
        if files_written_list or files_read_list:
            summary += "### Files Touched\n\n"
            summary += "| Action | File |\n|--------|------|\n"
            for fp in files_written_list:
                summary += f"| Edit | `{fp}` |\n"
            for fp in files_read_list:
                summary += f"| Read | `{fp}` |\n"
            summary += "\n"
        if commands_run_list:
            summary += "### Commands Run\n\n"
            for i, cmd in enumerate(commands_run_list, 1):
                summary += f"{i}. `{cmd}`\n"
            summary += "\n"
        non_zero = {k: v for k, v in tool_result_stats.items() if v > 0}
        if non_zero:
            summary += "### Tool Results\n\n"
            labels = {
                "terminal_success": "Terminal Success",
                "terminal_error": "Terminal Error",
                "file_reads": "File Reads",
                "file_edits": "File Edits",
                "searches": "Searches",
                "web": "Web Fetches",
            }
            for k, v in non_zero.items():
                summary += f"- {labels.get(k, k)}: {v}\n"
            summary += "\n"
        summary += "---\n\n"

    # ── Body ──────────────────────────────────────────────────────────────────
    body = ""
    for bub in bubbles:
        role = "User" if bub["type"] == "user" else "Assistant"
        body += f"### {role}\n\n"
        bub_meta: list[str] = []
        if bub.get("model"):
            bub_meta.append(f"Model: {bub['model']}")
        if bub.get("responseTimeMs"):
            bub_meta.append(f"Response: {bub['responseTimeMs'] / 1000:.1f}s")
        if bub.get("thinkingDurationMs"):
            bub_meta.append(f"Thinking: {bub['thinkingDurationMs'] / 1000:.1f}s")
        if bub.get("contextTokensUsed") and bub.get("contextTokenLimit"):
            pct = bub["contextTokensUsed"] / bub["contextTokenLimit"] * 100
            bub_meta.append(
                f"Context: {bub['contextTokensUsed']:,} / {bub['contextTokenLimit']:,}"
                f" tokens ({pct:.0f}% used)"
            )
        elif bub.get("contextPctRemaining") is not None:
            bub_meta.append(f"Context: {bub['contextPctRemaining']}% remaining")
        if bub_meta:
            body += f"_{' | '.join(bub_meta)}_\n\n"
        if bub.get("timestamp"):
            body += f"_{datetime.fromtimestamp(bub['timestamp'] / 1000).isoformat()}_\n\n"
        if bub.get("thinking"):
            dur_str = (
                f" ({bub['thinkingDurationMs'] / 1000:.1f}s)"
                if bub.get("thinkingDurationMs") else ""
            )
            body += f"<details><summary>Thinking{dur_str}</summary>\n\n{bub['thinking']}\n\n</details>\n\n"
        body += bub["text"] + "\n\n"
        if bub.get("tool"):
            t = bub["tool"]
            tool_summary = t.get("summary") or t.get("name") or "unknown"
            tool_status = t.get("status") or ""
            status_str = f" ({tool_status})" if tool_status else ""
            body += f"> **Tool: {tool_summary}**{status_str}\n"
            if t.get("input"):
                body += "> **INPUT:**\n> ```\n"
                for iline in str(t["input"]).split("\n"):
                    body += f"> {iline}\n"
                body += "> ```\n"
            if t.get("output"):
                body += "> **OUTPUT:**\n> ```\n"
                for oline in str(t["output"]).split("\n"):
                    body += f"> {oline}\n"
                body += "> ```\n"
            body += "\n"
        body += "---\n\n"

    return fm_str + header + summary + body
