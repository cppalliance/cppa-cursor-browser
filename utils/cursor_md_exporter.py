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
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from models import Bubble, DisplayBubble
from models.bubble_display import BubbleRole
from utils.cli_chat_reader import traverse_blobs, messages_to_bubbles
from utils.display_bubble import (
    annotate_response_times,
    build_display_bubble_from_storage,
    display_bubble_metadata,
    display_bubble_tool_calls,
)
from utils.path_helpers import to_epoch_ms
from utils.text_extract import slug


_CLI_META_READ_ERRORS = (
    sqlite3.Error,
    json.JSONDecodeError,
    IndexError,
    TypeError,
    ValueError,
    UnicodeDecodeError,
)


def _render_tool_call_md(tc: dict[str, Any], *, include_status: bool = False) -> str:
    """Render one tool-call block as markdown blockquote lines."""
    summary = tc.get("summary") or tc.get("name") or "unknown"
    status_str = ""
    if include_status:
        tool_status = tc.get("status") or ""
        if tool_status:
            status_str = f" ({tool_status})"
    lines = [f"> **Tool: {summary}**{status_str}"]
    if tc.get("input"):
        lines.append("> **INPUT:**\n> ```")
        for iline in str(tc["input"]).split("\n"):
            lines.append(f"> {iline}")
        lines.append("> ```")
    if tc.get("output"):
        lines.append("> **OUTPUT:**\n> ```")
        for oline in str(tc["output"]).split("\n"):
            lines.append(f"> {oline}")
        lines.append("> ```")
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_cli_export_body(bubbles: list[DisplayBubble]) -> str:
    """Render conversation body for CLI session export."""
    body_parts: list[str] = []
    for b in bubbles:
        role_label = "User" if b["type"] == "user" else "Assistant"
        body_parts.append(f"### {role_label}\n\n")
        body_parts.append(b.get("text", "") + "\n\n")
        tool_calls = (b.get("metadata") or {}).get("toolCalls") or []
        for tc in tool_calls:
            if isinstance(tc, dict):
                body_parts.append(_render_tool_call_md(tc))
        body_parts.append("---\n\n")
    return "".join(body_parts)


# ── CLI session exporter ─────────────────────────────────────────────────────


def cursor_cli_session_to_markdown(
    db_path: str | Path,
    session_meta: dict[str, Any] | None = None,
    workspace_info: dict[str, Any] | None = None,
    bubbles: list[DisplayBubble] | None = None,
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
        from contextlib import closing
        try:
            # `closing(...)` guarantees .close() on scope exit (including on
            # exception); sqlite3.Connection's own context manager only handles
            # commit/rollback, not close. See issue #17.
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            decoded = json.loads(bytes.fromhex(row[0]).decode()) if row else {}
            session_meta = decoded if isinstance(decoded, dict) else {}
        except _CLI_META_READ_ERRORS:
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

    return fm_str + header + _render_cli_export_body(bubbles)


# ── IDE chat exporter ────────────────────────────────────────────────────────


def _build_ide_export_bubbles(
    composer_data: dict[str, Any],
    bubble_map: dict[str, Bubble],
    diffs: list[Any],
) -> list[DisplayBubble]:
    """Build sorted display bubbles for IDE markdown export."""
    headers = composer_data.get("fullConversationHeadersOnly") or []
    bubbles: list[DisplayBubble] = []
    for h in headers:
        storage = bubble_map.get(h.get("bubbleId"))
        if storage is None:
            continue
        role: BubbleRole = "user" if h.get("type") == 1 else "ai"
        entry = build_display_bubble_from_storage(storage, role)
        if entry is not None:
            bubbles.append(entry)

    diff_ts = (
        to_epoch_ms(composer_data.get("lastUpdatedAt"))
        or to_epoch_ms(composer_data.get("createdAt"))
        or int(datetime.now().timestamp() * 1000)
    )
    for d in diffs:
        bubbles.append({
            "type": "ai",
            "text": f"**Code edit:** {json.dumps(d)}",
            "timestamp": diff_ts,
        })

    bubbles.sort(key=lambda bub: bub.get("timestamp") or 0)
    annotate_response_times(bubbles)
    return bubbles


def _compute_ide_session_aggregates(
    bubbles: list[DisplayBubble],
    composer_data: dict[str, Any],
) -> dict[str, Any]:
    """Session-level stats for IDE export frontmatter and header."""
    total_response_ms = sum(
        display_bubble_metadata(bub).get("responseTimeMs") or 0 for bub in bubbles
    )
    total_thinking_ms = sum(
        display_bubble_metadata(bub).get("thinkingDurationMs") or 0 for bub in bubbles
    )
    total_tool_calls = sum(len(display_bubble_tool_calls(bub)) for bub in bubbles)
    max_ctx_used = max(
        (display_bubble_metadata(bub).get("contextTokensUsed") or 0) for bub in bubbles
    ) if bubbles else 0
    ctx_limit = max(
        (display_bubble_metadata(bub).get("contextTokenLimit") or 0) for bub in bubbles
    ) if bubbles else 0

    tool_breakdown: dict[str, int] = {}
    for bub in bubbles:
        for tool in display_bubble_tool_calls(bub):
            tn = tool.get("name", "unknown")
            tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

    ts_vals = [bub["timestamp"] for bub in bubbles if bub.get("timestamp")]
    wall_clock_sec = int((max(ts_vals) - min(ts_vals)) / 1000) if len(ts_vals) >= 2 else None

    return {
        "total_response_ms": total_response_ms,
        "total_thinking_ms": total_thinking_ms,
        "total_tool_calls": total_tool_calls,
        "max_ctx_used": max_ctx_used,
        "ctx_limit": ctx_limit,
        "lines_added": composer_data.get("totalLinesAdded", 0),
        "lines_removed": composer_data.get("totalLinesRemoved", 0),
        "tool_breakdown": tool_breakdown,
        "wall_clock_sec": wall_clock_sec,
        "thinking_count": sum(
            1 for bub in bubbles if display_bubble_metadata(bub).get("thinking")
        ),
    }


def _scan_ide_tool_activity(
    bubbles: list[DisplayBubble],
) -> tuple[list[str], list[str], list[str], dict[str, int]]:
    """Collect file/command activity lists and tool-result counters."""
    files_read_list: list[str] = []
    files_written_list: list[str] = []
    commands_run_list: list[str] = []
    tool_result_stats = {
        "terminal_success": 0, "terminal_error": 0,
        "file_reads": 0, "file_edits": 0,
        "searches": 0, "web": 0,
    }
    for bub in bubbles:
        for t in display_bubble_tool_calls(bub):
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
    return files_read_list, files_written_list, commands_run_list, tool_result_stats


def _build_ide_frontmatter(
    *,
    composer_id: str,
    title: str,
    created_ms: int,
    updated_at: int,
    ws_slug: str,
    ws_display_name: str,
    model_name: str | None,
    bubbles: list[DisplayBubble],
    aggregates: dict[str, Any],
    files_read_list: list[str],
    files_written_list: list[str],
    commands_run_list: list[str],
) -> str:
    """YAML frontmatter block for IDE export."""
    fm_lines = ["---"]
    fm_lines.append(f"log_id: {json.dumps(composer_id, ensure_ascii=False)}")
    fm_lines.append("log_type: chat")
    fm_lines.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    fm_lines.append(f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}")
    fm_lines.append(
        f"updated_at: {datetime.fromtimestamp(updated_at / 1000).isoformat() if updated_at else datetime.now().isoformat()}"
    )
    fm_lines.append(f"workspace: {ws_slug}")
    fm_lines.append(f"workspace_name: {json.dumps(ws_display_name, ensure_ascii=False)}")
    if model_name and model_name != "default":
        fm_lines.append(f"model: {json.dumps(model_name, ensure_ascii=False)}")
    fm_lines.append(f"message_count: {len(bubbles)}")
    total_tool_calls = aggregates["total_tool_calls"]
    if total_tool_calls:
        fm_lines.append(f"total_tool_calls: {total_tool_calls}")
    tool_breakdown = aggregates["tool_breakdown"]
    if tool_breakdown:
        fm_lines.append("tool_call_breakdown:")
        for tn, cnt in sorted(tool_breakdown.items(), key=lambda x: -x[1]):
            fm_lines.append(f"  {json.dumps(tn, ensure_ascii=False)}: {cnt}")
    if aggregates["thinking_count"]:
        fm_lines.append(f"thinking_count: {aggregates['thinking_count']}")
    wall_clock_sec = aggregates["wall_clock_sec"]
    if wall_clock_sec is not None:
        fm_lines.append(f"wall_clock_seconds: {wall_clock_sec}")
    if aggregates["total_response_ms"]:
        fm_lines.append(f"total_response_time_sec: {aggregates['total_response_ms'] / 1000:.1f}")
    if aggregates["total_thinking_ms"]:
        fm_lines.append(f"total_thinking_time_sec: {aggregates['total_thinking_ms'] / 1000:.1f}")
    max_ctx_used = aggregates["max_ctx_used"]
    ctx_limit = aggregates["ctx_limit"]
    if max_ctx_used and ctx_limit:
        fm_lines.append(f"max_context_tokens_used: {max_ctx_used}")
        fm_lines.append(f"context_token_limit: {ctx_limit}")
    lines_added = aggregates["lines_added"]
    lines_removed = aggregates["lines_removed"]
    if lines_added or lines_removed:
        fm_lines.append(f"lines_added: {lines_added}")
        fm_lines.append(f"lines_removed: {lines_removed}")
    if files_read_list or files_written_list:
        fm_lines.append(f"files_read: {len(files_read_list)}")
        fm_lines.append(f"files_written: {len(files_written_list)}")
    if commands_run_list:
        fm_lines.append(f"commands_run: {len(commands_run_list)}")
    fm_lines.append("---")
    return "\n".join(fm_lines) + "\n\n"


def _build_ide_document_header(
    title: str,
    *,
    created_ms: int,
    model_name: str | None,
    aggregates: dict[str, Any],
) -> str:
    """Title and metadata line for IDE export."""
    header = f"# {title}\n\n"
    meta_parts: list[str] = []
    if created_ms:
        meta_parts.append(
            f"Created: {datetime.fromtimestamp(created_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}"
        )
    if model_name and model_name != "default":
        meta_parts.append(f"Model: {model_name}")
    total_tool_calls = aggregates["total_tool_calls"]
    if total_tool_calls:
        meta_parts.append(f"Tool calls: {total_tool_calls}")
    wall_clock_sec = aggregates["wall_clock_sec"]
    if wall_clock_sec is not None:
        hrs, rem = divmod(wall_clock_sec, 3600)
        mins, secs = divmod(rem, 60)
        dur = f"{hrs}h {mins}m" if hrs else (f"{mins}m {secs}s" if mins else f"{secs}s")
        meta_parts.append(f"Duration: {dur}")
    header += f"_{' | '.join(meta_parts)}_\n\n---\n\n" if meta_parts else "---\n\n"
    return header


def _build_ide_session_summary(
    files_read_list: list[str],
    files_written_list: list[str],
    commands_run_list: list[str],
    tool_result_stats: dict[str, int],
) -> str:
    """Optional session summary section for IDE export."""
    has_tool_stats = any(v > 0 for v in tool_result_stats.values())
    if not (files_read_list or files_written_list or commands_run_list or has_tool_stats):
        return ""
    parts: list[str] = ["## Session Summary\n\n"]
    if files_written_list or files_read_list:
        parts.append("### Files Touched\n\n")
        parts.append("| Action | File |\n|--------|------|\n")
        for fp in files_written_list:
            parts.append(f"| Edit | `{fp}` |\n")
        for fp in files_read_list:
            parts.append(f"| Read | `{fp}` |\n")
        parts.append("\n")
    if commands_run_list:
        parts.append("### Commands Run\n\n")
        for i, cmd in enumerate(commands_run_list, 1):
            parts.append(f"{i}. `{cmd}`\n")
        parts.append("\n")
    non_zero = {k: v for k, v in tool_result_stats.items() if v > 0}
    if non_zero:
        parts.append("### Tool Results\n\n")
        labels = {
            "terminal_success": "Terminal Success",
            "terminal_error": "Terminal Error",
            "file_reads": "File Reads",
            "file_edits": "File Edits",
            "searches": "Searches",
            "web": "Web Fetches",
        }
        for k, v in non_zero.items():
            parts.append(f"- {labels.get(k, k)}: {v}\n")
        parts.append("\n")
    parts.append("---\n\n")
    return "".join(parts)


def _render_ide_export_body(bubbles: list[DisplayBubble]) -> str:
    """Render conversation body for IDE chat export."""
    body_parts: list[str] = []
    for bub in bubbles:
        role_label = "User" if bub["type"] == "user" else "Assistant"
        body_parts.append(f"### {role_label}\n\n")
        meta = display_bubble_metadata(bub)
        bub_meta: list[str] = []
        if meta.get("modelName"):
            bub_meta.append(f"Model: {meta['modelName']}")
        response_ms = meta.get("responseTimeMs")
        if response_ms:
            bub_meta.append(f"Response: {response_ms / 1000:.1f}s")
        thinking_ms = meta.get("thinkingDurationMs")
        if thinking_ms:
            bub_meta.append(f"Thinking: {thinking_ms / 1000:.1f}s")
        ctx_used = meta.get("contextTokensUsed")
        ctx_limit_bub = meta.get("contextTokenLimit")
        if ctx_used and ctx_limit_bub:
            pct = ctx_used / ctx_limit_bub * 100
            bub_meta.append(
                f"Context: {ctx_used:,} / {ctx_limit_bub:,}"
                f" tokens ({pct:.0f}% used)"
            )
        elif meta.get("contextWindowPercent") is not None:
            remaining = meta["contextWindowPercent"]
            bub_meta.append(f"Context: {remaining}% remaining")
        if bub_meta:
            body_parts.append(f"_{' | '.join(bub_meta)}_\n\n")
        if bub.get("timestamp"):
            body_parts.append(
                f"_{datetime.fromtimestamp(bub['timestamp'] / 1000).isoformat()}_\n\n"
            )
        thinking_text = meta.get("thinking")
        if thinking_text:
            dur_str = f" ({thinking_ms / 1000:.1f}s)" if thinking_ms else ""
            body_parts.append(
                f"<details><summary>Thinking{dur_str}</summary>\n\n"
                f"{thinking_text}\n\n</details>\n\n"
            )
        body_parts.append(bub["text"] + "\n\n")
        for t in display_bubble_tool_calls(bub):
            body_parts.append(_render_tool_call_md(t, include_status=True))
        body_parts.append("---\n\n")
    return "".join(body_parts)


def cursor_ide_chat_to_markdown(
    composer_data: dict[str, Any],
    composer_id: str,
    bubble_map: dict[str, Bubble],
    code_block_diff_map: dict[str, Any] | None = None,
    workspace_info: dict[str, Any] | None = None,
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
        Global ``{bubble_id: Bubble}`` map from
        :func:`services.workspace_db.load_bubble_map`.
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

    bubbles = _build_ide_export_bubbles(cd, bubble_map, diffs)
    aggregates = _compute_ide_session_aggregates(bubbles, cd)
    files_read_list, files_written_list, commands_run_list, tool_result_stats = (
        _scan_ide_tool_activity(bubbles)
    )

    fm_str = _build_ide_frontmatter(
        composer_id=composer_id,
        title=title,
        created_ms=created_ms,
        updated_at=updated_at,
        ws_slug=ws_slug,
        ws_display_name=ws_display_name,
        model_name=model_name,
        bubbles=bubbles,
        aggregates=aggregates,
        files_read_list=files_read_list,
        files_written_list=files_written_list,
        commands_run_list=commands_run_list,
    )
    header = _build_ide_document_header(
        title,
        created_ms=created_ms,
        model_name=model_name,
        aggregates=aggregates,
    )
    summary = _build_ide_session_summary(
        files_read_list, files_written_list, commands_run_list, tool_result_stats,
    )
    body = _render_ide_export_body(bubbles)
    return fm_str + header + summary + body
