"""Search helpers: three independent data-source readers for /api/search.

Each public function targets exactly one data source, accepts explicit inputs
with no Flask request-context dependency, and returns a plain list of result
dicts.  The route handler in ``api/search.py`` calls all three and merges.

Data sources
------------
* :func:`search_global_storage` — composerData rows in global ``cursorDiskKV``
* :func:`search_legacy_workspaces` — per-workspace ItemTable (legacy chat format)
* :func:`search_cli_sessions` — JSONL files from Cursor CLI agent sessions

Aggregation
-----------
* :func:`rank_results` — sort merged results by timestamp descending
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = [
    "rank_results",
    "search_cli_sessions",
    "search_global_storage",
    "search_legacy_workspaces",
]
from models import Bubble, Composer, ParseWarningCollector, SchemaError, SearchResult
from services.workspace_db import (
    build_composer_id_to_workspace_id_cached,
    collect_workspace_entries,
    open_global_db,
)
from utils.cli_chat_reader import list_cli_projects, messages_to_bubbles, traverse_blobs
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.path_helpers import (
    get_workspace_display_name,
    to_epoch_ms,
    warn_workspace_json_read,
)
from utils.text_extract import extract_text_from_bubble

_logger = logging.getLogger(__name__)

# Missing/unparseable timestamps sort last in rank_results() (treated as 0.0 s).
_UNKNOWN_SEARCH_TIMESTAMP: int = 0


# ---------------------------------------------------------------------------
# Private helpers — pure functions / small utilities
# ---------------------------------------------------------------------------


def _json_dump_safe(value: object) -> str:
    """Best-effort JSON serialisation for exclusion-rule matching."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value) if value is not None else ""


def _build_exclusion_searchable(
    *,
    project_name: str | None,
    chat_title: str | None,
    model_names: list[str] | None = None,
    content_parts: list[str] | None = None,
    metadata_parts: list[str] | None = None,
) -> str:
    """Compose broad searchable text so exclusion rules cover all visible fields."""
    combined: list[str] = []
    if content_parts:
        combined.extend(p for p in content_parts if p)
    if metadata_parts:
        combined.extend(p for p in metadata_parts if p)
    return build_searchable_text(
        project_name=project_name,
        chat_title=chat_title,
        model_names=model_names,
        chat_content_snippet="\n\n".join(combined) if combined else None,
    )


def _extract_snippet(text: str, query: str, query_lower: str) -> str:
    """Return a context window around the first match of *query* in *text*.

    Returns an empty string if there is no match.
    """
    if not query_lower:
        return ""
    idx = text.lower().find(query_lower)
    if idx == -1:
        return ""
    start = max(0, idx - 80)
    end = min(len(text), idx + len(query) + 120)
    return (
        ("..." if start > 0 else "")
        + text[start:end]
        + ("..." if end < len(text) else "")
    )


def _find_match(
    title: str,
    bubble_texts: list[str],
    query_lower: str,
    query: str,
) -> tuple[bool, str]:
    """Check whether a conversation matches the search query.

    Returns ``(has_match, matching_text)`` where *matching_text* is either the
    full title (on a title hit) or a snippet around the first bubble match.
    """
    if title and query_lower in title.lower():
        return True, title
    for text in bubble_texts:
        if text and query_lower in text.lower():
            return True, _extract_snippet(text, query, query_lower)
    return False, ""


# ---------------------------------------------------------------------------
# Private data builders
# ---------------------------------------------------------------------------


def _build_ws_id_to_name(
    workspace_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Map workspace folder IDs to human-readable display names.

    Reads each workspace's ``workspace.json`` via
    :func:`~utils.path_helpers.get_workspace_display_name`.  Entries whose
    JSON cannot be read are silently skipped (warning logged).
    """
    mapping: dict[str, str] = {}
    for entry in workspace_entries:
        try:
            with open(entry["workspaceJsonPath"], "r", encoding="utf-8") as fh:
                wd = json.load(fh)
            name = get_workspace_display_name(wd)
            if name:
                mapping[entry["name"]] = name
        except Exception as exc:
            warn_workspace_json_read(_logger, entry["name"], exc)
    return mapping


def _build_search_bubble_map(
    global_db: sqlite3.Connection,
    parse_warnings: ParseWarningCollector,
) -> dict[str, dict[str, Any]]:
    """Load ``bubbleId:*`` rows from an open global DB connection.

    Returns ``{bubble_id: {"text": str, "raw": dict}}``.  Rows that fail
    schema validation or JSON decoding are skipped; the skip is recorded in
    *parse_warnings*.
    """
    bubble_map: dict[str, dict[str, Any]] = {}
    for row in global_db.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
    ):
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        bid = parts[2]
        try:
            bubble = Bubble.from_dict(json.loads(row["value"]), bubble_id=bid)
            bubble_map[bid] = {"text": extract_text_from_bubble(bubble), "raw": bubble.raw}
        except SchemaError as exc:
            _logger.warning(
                "Schema drift in bubble %s: %s (%s)", bid, exc, type(exc).__name__
            )
            parse_warnings.record_bubble_skipped()
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            _logger.warning("Failed to decode Bubble from bubbleId:%s: %s", bid, exc)
            parse_warnings.record_bubble_skipped()
    return bubble_map


# ---------------------------------------------------------------------------
# Public: per-source search functions
# ---------------------------------------------------------------------------


def search_global_storage(
    workspace_path: str,
    query: str,
    query_lower: str,
    rules: list[Any],
    parse_warnings: ParseWarningCollector,
) -> list[SearchResult]:
    """Search composer conversations stored in the global ``cursorDiskKV`` table.

    This is the primary data source for current Cursor versions.

    Args:
        workspace_path: Cursor workspaceStorage root directory.
        query: Raw search string (used for snippet extraction).
        query_lower: ``query.lower()`` (pre-computed by caller).
        rules: Parsed exclusion rules from app config.
        parse_warnings: Collector that accumulates parse/schema failures.

    Returns:
        List of search result dicts with keys ``workspaceId``, ``workspaceFolder``,
        ``chatId``, ``chatTitle``, ``timestamp``, ``matchingText``, ``type``.
    """
    results: list[SearchResult] = []
    try:
        workspace_entries = collect_workspace_entries(workspace_path)
        ws_id_to_name = _build_ws_id_to_name(workspace_entries)
        composer_id_to_ws = build_composer_id_to_workspace_id_cached(
            workspace_path, workspace_entries, rules
        )

        with open_global_db(workspace_path) as (conn, _db_path):
            if conn is None:
                return results
            bubble_map = _build_search_bubble_map(conn, parse_warnings)
            composer_rows = conn.execute(
                "SELECT key, value FROM cursorDiskKV"
                " WHERE key LIKE 'composerData:%' AND LENGTH(value) > 10"
            ).fetchall()

        for row in composer_rows:
            composer_id = row["key"].split(":")[1]
            try:
                composer = Composer.from_dict(
                    json.loads(row["value"]), composer_id=composer_id
                )
            except SchemaError as exc:
                _logger.warning(
                    "Schema drift in composer %s: %s (%s)",
                    composer_id,
                    exc,
                    type(exc).__name__,
                )
                parse_warnings.record_composer_skipped()
                continue
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                _logger.warning(
                    "Failed to decode Composer from composerData:%s: %s",
                    composer_id,
                    exc,
                )
                parse_warnings.record_composer_skipped()
                continue

            try:
                headers = composer.full_conversation_headers_only
                if not headers:
                    continue

                title = composer.name or ""
                ws_id = composer_id_to_ws.get(composer_id, "global")
                ws_name = ws_id_to_name.get(ws_id)
                project_name = ws_name or ("Other chats" if ws_id == "global" else ws_id)

                cd = composer.raw
                model_config = composer.model_config
                model_name = model_config.get("modelName")
                model_names = (
                    [model_name] if model_name and model_name != "default" else None
                )

                bubble_texts: list[str] = []
                bubble_meta: list[str] = []
                for header in headers:
                    bid = header.get("bubbleId")
                    if not bid:
                        continue
                    entry = bubble_map.get(bid)
                    if not entry:
                        continue
                    text = entry.get("text") or ""
                    if text:
                        bubble_texts.append(text)
                    raw_bubble = entry.get("raw")
                    if raw_bubble:
                        bubble_meta.append(_json_dump_safe(raw_bubble))

                exclusion_text = _build_exclusion_searchable(
                    project_name=project_name,
                    chat_title=title,
                    model_names=model_names,
                    content_parts=bubble_texts,
                    metadata_parts=[
                        _json_dump_safe(model_config),
                        _json_dump_safe(cd.get("conversationSummary")),
                        _json_dump_safe(cd.get("usage")),
                        _json_dump_safe(cd.get("requestMetadata")),
                        "\n".join(bubble_meta),
                    ],
                )
                if is_excluded_by_rules(rules, exclusion_text):
                    continue

                has_match, matching_text = _find_match(
                    title, bubble_texts, query_lower, query
                )
                if not has_match:
                    continue

                if not title:
                    for text in bubble_texts:
                        if text:
                            first_lines = [ln for ln in text.split("\n") if ln.strip()]
                            if first_lines:
                                title = first_lines[0][:100]
                            break
                    if not title:
                        title = f"Conversation {composer_id[:8]}"

                results.append({
                    "workspaceId": ws_id,
                    "workspaceFolder": ws_name,
                    "chatId": composer_id,
                    "chatTitle": title,
                    "timestamp": (
                        to_epoch_ms(composer.last_updated_at)
                        or to_epoch_ms(composer.created_at)
                        or _UNKNOWN_SEARCH_TIMESTAMP
                    ),
                    "matchingText": matching_text,
                    "type": "composer",
                })
            except Exception as exc:
                _logger.warning(
                    "Failed to process Composer from composerData:%s during search: %s",
                    composer_id,
                    exc,
                )
                parse_warnings.record_composer_processing_failure()

    except Exception as exc:
        _logger.exception("Error searching global storage")
        parse_warnings.record_source_failure(exc, source="global_storage")

    return results


def search_legacy_workspaces(
    workspace_path: str,
    query: str,
    query_lower: str,
    search_type: str,
    rules: list[Any],
) -> list[SearchResult]:
    """Search legacy per-workspace ItemTable chat data.

    Iterates per-workspace ``state.vscdb`` files looking for the
    ``workbench.panel.aichat.view.aichat.chatdata`` key (present in older
    Cursor versions before global storage migration).

    Args:
        workspace_path: Cursor workspaceStorage root directory.
        query: Raw search string (used for snippet extraction).
        query_lower: ``query.lower()`` (pre-computed by caller).
        search_type: ``"all"`` or ``"chat"`` — other values return immediately.
        rules: Parsed exclusion rules from app config.

    Returns:
        List of search result dicts with ``type`` set to ``"chat"``.
    """
    results: list[SearchResult] = []
    if search_type not in ("all", "chat"):
        return results

    try:
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if not os.path.isdir(full):
                continue
            db_path = os.path.join(full, "state.vscdb")
            wj_path = os.path.join(full, "workspace.json")
            if not os.path.isfile(db_path):
                continue

            workspace_folder: str | None = None
            workspace_name = name
            try:
                with open(wj_path, "r", encoding="utf-8") as fh:
                    wd = json.load(fh)
                workspace_folder = wd.get("folder")
                workspace_name = get_workspace_display_name(wd, fallback=name)
            except Exception as exc:
                warn_workspace_json_read(_logger, name, exc)

            db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            try:
                with closing(sqlite3.connect(db_uri, uri=True)) as conn:
                    chat_row = conn.execute(
                        "SELECT value FROM ItemTable"
                        " WHERE [key] = 'workbench.panel.aichat.view.aichat.chatdata'"
                    ).fetchone()

                if not (chat_row and chat_row[0]):
                    continue

                data = json.loads(chat_row[0])
                for tab in (data.get("tabs") or []):
                    ct = tab.get("chatTitle") or ""
                    tab_id = str(tab.get("tabId") or "")

                    tab_model_names: list[str] | None = None
                    tab_meta = tab.get("metadata")
                    if isinstance(tab_meta, dict):
                        models_used = tab_meta.get("modelsUsed")
                        if isinstance(models_used, list):
                            tab_model_names = [str(m) for m in models_used if m]
                        elif tab_meta.get("model"):
                            tab_model_names = [str(tab_meta.get("model"))]

                    tab_bubble_texts = [
                        bubble.get("text") or ""
                        for bubble in (tab.get("bubbles") or [])
                        if bubble.get("text")
                    ]
                    exclusion_text = _build_exclusion_searchable(
                        project_name=workspace_name,
                        chat_title=ct,
                        model_names=tab_model_names,
                        content_parts=tab_bubble_texts,
                        metadata_parts=[
                            _json_dump_safe(tab),
                            _json_dump_safe(workspace_folder),
                        ],
                    )
                    if is_excluded_by_rules(rules, exclusion_text):
                        continue

                    has_match, matching_text = _find_match(
                        ct, tab_bubble_texts, query_lower, query
                    )
                    if not has_match:
                        continue

                    results.append({
                        "workspaceId": name,
                        "workspaceFolder": workspace_name,
                        "chatId": tab_id,
                        "chatTitle": ct or f"Chat {tab_id[:8]}",
                        "timestamp": to_epoch_ms(tab.get("lastSendTime")) or _UNKNOWN_SEARCH_TIMESTAMP,
                        "matchingText": matching_text,
                        "type": "chat",
                    })

            except Exception as exc:
                _logger.warning(
                    "Failed to search legacy workspace %s: %s",
                    name,
                    exc,
                    exc_info=True,
                )

    except Exception as exc:
        _logger.warning(
            "Failed to iterate legacy workspaces under %s: %s", workspace_path, exc
        )

    return results


def search_cli_sessions(
    cli_chats_path: str,
    query: str,
    query_lower: str,
    rules: list[Any],
    parse_warnings: ParseWarningCollector | None = None,
) -> list[SearchResult]:
    """Search Cursor CLI agent sessions stored as JSONL + blob files.

    Reads from ``~/.cursor/chats/`` (or the path returned by
    :func:`~utils.workspace_path.get_cli_chats_path`).

    Args:
        cli_chats_path: Path to the Cursor CLI chats directory.
        query: Raw search string (used for snippet extraction).
        query_lower: ``query.lower()`` (pre-computed by caller).
        rules: Parsed exclusion rules from app config.

    Returns:
        List of search result dicts with ``type`` set to ``"cli_agent"`` and
        ``source`` set to ``"cli"``.
    """
    results: list[SearchResult] = []
    try:
        cli_projects = list_cli_projects(cli_chats_path)
        for cp in cli_projects:
            ws_name = cp["workspace_name"] or cp["project_id"][:12]
            for session in cp["sessions"]:
                meta = session.get("meta", {})
                session_id = session["session_id"]
                created_ms: int = to_epoch_ms(meta.get("createdAt"))
                session_name: str = meta.get("name") or f"Session {session_id[:8]}"

                try:
                    messages = traverse_blobs(session["db_path"])
                except Exception as exc:
                    _logger.warning(
                        "Failed to traverse CLI session blobs for %s: %s",
                        session_id,
                        exc,
                    )
                    continue

                if not messages and meta:
                    _logger.warning(
                        "CLI session %s has meta but traverse_blobs returned no "
                        "messages from %s",
                        session_id,
                        session["db_path"],
                    )

                bubbles = messages_to_bubbles(messages, created_ms)
                if not bubbles:
                    continue

                title = session_name
                if not title or title.startswith("New Agent"):
                    for b in bubbles:
                        if b["type"] == "user" and b.get("text"):
                            first_lines = [
                                ln for ln in b["text"].split("\n") if ln.strip()
                            ]
                            if first_lines:
                                title = first_lines[0][:100]
                            break

                bubble_texts = [b["text"] for b in bubbles if b.get("text")]
                tool_payloads = [
                    tc.get("input") or tc.get("summary") or ""
                    for b in bubbles
                    for tc in (b.get("metadata") or {}).get("toolCalls") or []
                ]
                exclusion_text = _build_exclusion_searchable(
                    project_name=ws_name,
                    chat_title=title,
                    content_parts=bubble_texts + tool_payloads,
                )
                if is_excluded_by_rules(rules, exclusion_text):
                    continue

                has_match, matching_text = _find_match(
                    title, bubble_texts, query_lower, query
                )
                if not has_match:
                    continue

                results.append({
                    "workspaceId": f"cli:{cp['project_id']}",
                    "workspaceFolder": ws_name,
                    "chatId": session_id,
                    "chatTitle": title,
                    "timestamp": created_ms,
                    "matchingText": matching_text,
                    "type": "cli_agent",
                    "source": "cli",
                })
    except Exception as exc:
        _logger.exception("Error searching CLI sessions")
        if parse_warnings is not None:
            parse_warnings.record_source_failure(exc, source="cli_sessions")

    return results


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def rank_results(results: list[SearchResult]) -> list[SearchResult]:
    """Sort *results* by timestamp descending.

    All three source types use epoch-millisecond integers, except
    ``search_legacy_workspaces`` which may emit ISO 8601 strings for the
    ``lastSendTime`` field.  ISO strings are converted to epoch-ms so
    cross-source comparisons are made in the same unit.
    """
    def _ts(r: SearchResult) -> float:
        t = r.get("timestamp", 0)
        if t is None:
            return 0.0
        if isinstance(t, str):
            try:
                # .timestamp() -> epoch-seconds; x1000 -> epoch-ms to match ints
                return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp() * 1000
            except Exception:
                return 0.0
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return 0.0
        return float(t) if t else 0.0

    return sorted(results, key=_ts, reverse=True)
