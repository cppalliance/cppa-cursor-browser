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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "DEFAULT_SEARCH_WINDOW_DAYS",
    "rank_results",
    "resolve_search_since_ms",
    "search_cli_sessions",
    "search_global_storage",
    "search_legacy_workspaces",
]
from models import Composer, ParseWarningCollector, SchemaError, SearchResult
from services.workspace_composer_scan import assign_composer_workspace
from services.workspace_context import (
    WorkspaceContext,
    enrich_workspace_context_from_global_db,
    resolve_workspace_context_cached,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    build_composer_id_to_workspace_id_cached,
    collect_workspace_entries,
    open_global_db,
    safe_fetchall,
)
from services.workspace_resolver import infer_invalid_workspace_aliases
from utils.cli_chat_reader import list_cli_projects, messages_to_bubbles, traverse_blobs
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.text_extract import extract_text_from_bubble
from utils.path_helpers import (
    get_workspace_display_name,
    to_epoch_ms,
    warn_workspace_json_read,
)

_logger = logging.getLogger(__name__)

# Missing/unparseable timestamps sort last in rank_results() (treated as 0.0 s).
_UNKNOWN_SEARCH_TIMESTAMP: int = 0

DEFAULT_SEARCH_WINDOW_DAYS = 30

# When a date window is active, chats with no parseable timestamp stay searchable.
_INCLUDE_UNKNOWN_TIMESTAMPS_IN_WINDOW = True


def resolve_search_since_ms(
    *,
    all_history: bool = False,
    since_days: int | None = None,
    now: datetime | None = None,
) -> int | None:
    """Return epoch-ms cutoff for search, or ``None`` to search all history.

    Composers with no parseable timestamp (``updated_ms <= 0``) remain
    searchable when a window is active; see ``_INCLUDE_UNKNOWN_TIMESTAMPS_IN_WINDOW``.
    """
    if all_history:
        return None
    days = since_days if since_days is not None else DEFAULT_SEARCH_WINDOW_DAYS
    if days > 36_500:
        days = 36_500
    if days <= 0:
        return None
    ref = now or datetime.now(timezone.utc)
    cutoff = ref - timedelta(days=days)
    return int(cutoff.timestamp() * 1000)


def _timestamp_in_search_window(timestamp_ms: int, since_ms: int | None) -> bool:
    if since_ms is None:
        return True
    if timestamp_ms <= 0:
        return _INCLUDE_UNKNOWN_TIMESTAMPS_IN_WINDOW
    return timestamp_ms >= since_ms


def _composer_dict_timestamp_ms(cd: dict[str, Any]) -> int:
    return (
        to_epoch_ms(cd.get("lastUpdatedAt"))
        or to_epoch_ms(cd.get("createdAt"))
        or 0
    )


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


def _sql_like_substring(query_lower: str) -> str:
    """Escape *query_lower* for use in a case-insensitive SQL ``LIKE``."""
    escaped = (
        query_lower
        .replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _quick_bubble_text(raw_value: object) -> str:
    """Extract searchable text from a bubble KV value without full model validation."""
    try:
        if isinstance(raw_value, (bytes, bytearray)):
            text_value = raw_value.decode("utf-8", errors="replace")
        elif isinstance(raw_value, str):
            text_value = raw_value
        else:
            return ""
        obj = json.loads(text_value)
        if isinstance(obj, dict):
            text = extract_text_from_bubble(obj)
            if text:
                return text
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def _model_config_from_composer_dict(cd: dict[str, Any]) -> dict[str, Any]:
    raw = cd.get("modelConfig")
    return raw if isinstance(raw, dict) else {}


def _all_bubble_texts_for_composer(
    conn: sqlite3.Connection,
    composer_id: str,
) -> list[str]:
    """Load all bubble texts for one composer (exclusion-rule checks)."""
    texts: list[str] = []
    try:
        rows = conn.execute(
            "SELECT value FROM cursorDiskKV"
            " WHERE key LIKE ? AND value IS NOT NULL",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return texts
    for row in rows:
        text = _quick_bubble_text(row["value"])
        if text:
            texts.append(text)
    return texts


def _composer_exclusion_text(
    *,
    project_name: str,
    title: str,
    model_names: list[str] | None,
    model_config: dict[str, Any],
    cd: dict[str, Any],
    all_bubble_texts: list[str],
) -> str:
    return _build_exclusion_searchable(
        project_name=project_name,
        chat_title=title,
        model_names=model_names,
        content_parts=all_bubble_texts or None,
        metadata_parts=[
            _json_dump_safe(model_config),
            _json_dump_safe(cd.get("conversationSummary")),
            _json_dump_safe(cd.get("usage")),
            _json_dump_safe(cd.get("requestMetadata")),
        ],
    )


def _index_bubble_texts_matching_query(
    conn: sqlite3.Connection,
    query_lower: str,
    *,
    composer_ids: set[str] | None = None,
) -> dict[str, list[str]]:
    """Index composer ID -> bubble texts containing *query_lower*.

    Always uses one SQL pass over ``bubbleId:%`` rows (LIKE prefilter), then
    optionally filters to *composer_ids* in Python. Per-composer SQL queries
    are slower than a single scan even when the date window is narrow.
    """
    if not query_lower:
        return {}
    if composer_ids is not None and not composer_ids:
        return {}

    pattern = _sql_like_substring(query_lower)
    by_composer: dict[str, list[str]] = {}

    def _ingest_row(row_key: str, row_value: object) -> None:
        parts = row_key.split(":")
        if len(parts) < 2 or not parts[1]:
            return
        composer_id = parts[1]
        if composer_ids is not None and composer_id not in composer_ids:
            return
        text = _quick_bubble_text(row_value)
        if text and query_lower in text.lower():
            by_composer.setdefault(composer_id, []).append(text)

    try:
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV"
            " WHERE key LIKE 'bubbleId:%'"
            " AND value IS NOT NULL"
            " AND LOWER(value) LIKE ? ESCAPE '\\'",
            (pattern,),
        ).fetchall()
        for row in rows:
            _ingest_row(row["key"], row["value"])
    except sqlite3.Error:
        return by_composer
    return by_composer


def _composer_row_raw_text(row: sqlite3.Row) -> str:
    raw = row["value"]
    if raw is None:
        return ""
    if isinstance(raw, (bytes, bytearray)):
        return raw.decode("utf-8", errors="replace")
    return str(raw)


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


@dataclass(frozen=True)
class _SearchWorkspaceAssigner:
    """Workspace assignment aligned with tab summaries (not roster map alone)."""

    ws_id_to_name: dict[str, str]
    ctx: WorkspaceContext
    invalid_workspace_aliases: dict[str, str]

    def workspace_for_composer(self, composer: Composer) -> str:
        # Deliberately omit global bubble index (same as list/summary paths).
        return assign_composer_workspace(
            composer,
            project_layouts_map=self.ctx.project_layouts_map,
            project_name_map=self.ctx.project_name_to_workspace_id,
            workspace_path_map=self.ctx.workspace_path_to_id,
            workspace_entries=self.ctx.workspace_entries,
            bubble_map={},
            composer_id_to_ws=self.ctx.composer_id_to_workspace_id,
            invalid_workspace_ids=self.ctx.invalid_workspace_ids,
            invalid_workspace_aliases=self.invalid_workspace_aliases,
        )


def _load_search_workspace_assigner(
    workspace_path: str,
    rules: list[Any],
    workspace_entries: list[dict[str, Any]],
) -> _SearchWorkspaceAssigner | None:
    ctx = resolve_workspace_context_cached(
        workspace_path, rules, workspace_entries=workspace_entries,
    )
    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return None
        ctx = enrich_workspace_context_from_global_db(
            ctx, global_db, populate_project_layouts=True,
        )
        invalid_workspace_aliases: dict[str, str] = {}
        if ctx.invalid_workspace_ids:
            # Issue #116 follow-up: search assigner still cold-scans composerData:*
            # rows here; sharing resolve_invalid_workspace_aliases_cached is
            # intentionally deferred (operator scope — see issue Out of scope).
            composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)
            invalid_workspace_aliases = infer_invalid_workspace_aliases(
                composer_rows=composer_rows,
                project_layouts_map=ctx.project_layouts_map,
                project_name_map=ctx.project_name_to_workspace_id,
                workspace_path_map=ctx.workspace_path_to_id,
                workspace_entries=ctx.workspace_entries,
                bubble_map={},
                composer_id_to_ws=ctx.composer_id_to_workspace_id,
                invalid_workspace_ids=ctx.invalid_workspace_ids,
            )
    return _SearchWorkspaceAssigner(
        ws_id_to_name=_build_ws_id_to_name(workspace_entries),
        ctx=ctx,
        invalid_workspace_aliases=invalid_workspace_aliases,
    )


def _workspace_id_for_search_hit(
    *,
    composer_id: str,
    cd: dict[str, Any],
    assigner: _SearchWorkspaceAssigner | None,
    composer_id_to_ws: dict[str, str],
) -> str:
    if assigner is None:
        return composer_id_to_ws.get(composer_id, "global")
    try:
        composer = Composer.from_dict(cd, composer_id=composer_id)
    except SchemaError:
        return composer_id_to_ws.get(composer_id, "global")
    return assigner.workspace_for_composer(composer)


# ---------------------------------------------------------------------------
# Public: per-source search functions
# ---------------------------------------------------------------------------


def search_global_storage(
    workspace_path: str,
    query: str,
    query_lower: str,
    rules: list[Any],
    parse_warnings: ParseWarningCollector,
    *,
    since_ms: int | None = None,
) -> list[SearchResult]:
    """Search composer conversations stored in the global ``cursorDiskKV`` table.

    This is the primary data source for current Cursor versions.

    Args:
        workspace_path: Cursor workspaceStorage root directory.
        query: Raw search string (used for snippet extraction).
        query_lower: ``query.lower()`` (pre-computed by caller).
        rules: Parsed exclusion rules from app config.
        parse_warnings: Collector that accumulates parse/schema failures.
        since_ms: When set, only composers updated/created on or after this
            epoch-ms cutoff are searched. ``None`` searches all history.

    Returns:
        List of search result dicts with keys ``workspaceId``, ``workspaceFolder``,
        ``chatId``, ``chatTitle``, ``timestamp``, ``matchingText``, ``type``.
    """
    from services.search_index import index_is_usable

    if index_is_usable(workspace_path, rules):
        indexed = _search_global_storage_via_index(
            workspace_path,
            query,
            query_lower,
            rules,
            parse_warnings,
            since_ms=since_ms,
        )
        if indexed is not None:
            return indexed

    return _search_global_storage_live_scan(
        workspace_path,
        query,
        query_lower,
        rules,
        parse_warnings,
        since_ms=since_ms,
    )


def _search_global_storage_via_index(
    workspace_path: str,
    query: str,
    query_lower: str,
    rules: list[Any],
    parse_warnings: ParseWarningCollector,
    *,
    since_ms: int | None = None,
) -> list[SearchResult] | None:
    """Search using local FTS index. Returns ``None`` to fall back to live scan."""
    from services.search_index import (
        query_all_bubble_texts_for_composer_ids,
        query_composer_bubble_hits,
        query_composer_rows_in_window,
        query_composer_title_hits,
    )

    results: list[SearchResult] = []
    try:
        workspace_entries = collect_workspace_entries(workspace_path)
        assigner = _load_search_workspace_assigner(
            workspace_path, rules, workspace_entries,
        )
        if assigner is not None:
            ws_id_to_name = assigner.ws_id_to_name
            composer_id_to_ws = assigner.ctx.composer_id_to_workspace_id
        else:
            ws_id_to_name = _build_ws_id_to_name(workspace_entries)
            composer_id_to_ws = build_composer_id_to_workspace_id_cached(
                workspace_path, workspace_entries, rules,
            )

        composers_in_window = query_composer_rows_in_window(since_ms)
        if since_ms is not None and not composers_in_window:
            return results

        search_pool = composers_in_window

        window_ids = set(composers_in_window.keys()) if since_ms is not None else None
        bubble_texts_by_composer = query_composer_bubble_hits(
            query_lower,
            since_ms=since_ms,
            composer_ids_filter=window_ids,
        )

        candidate_ids: set[str] = set(bubble_texts_by_composer.keys())
        for row in query_composer_title_hits(query_lower, since_ms=since_ms):
            candidate_ids.add(row["composer_id"])

        for composer_id, row in search_pool.items():
            raw_lower = (row["raw_json"] or "").lower()
            if query_lower in raw_lower:
                candidate_ids.add(composer_id)

        all_bubbles_by_composer = query_all_bubble_texts_for_composer_ids(candidate_ids)

        for composer_id in candidate_ids:
            composer_row = search_pool.get(composer_id)
            if composer_row is None:
                continue

            raw_text = composer_row["raw_json"] or ""
            try:
                cd = json.loads(raw_text)
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                _logger.warning(
                    "Failed to decode Composer from index composerData:%s: %s",
                    composer_id,
                    exc,
                )
                parse_warnings.record_composer_skipped()
                continue
            if not isinstance(cd, dict):
                parse_warnings.record_composer_skipped()
                continue

            try:
                headers = cd.get("fullConversationHeadersOnly") or []
                if not headers:
                    continue

                title = composer_row["title"] or cd.get("name") or ""
                if not isinstance(title, str):
                    title = str(title) if title else ""

                ws_id = _workspace_id_for_search_hit(
                    composer_id=composer_id,
                    cd=cd,
                    assigner=assigner,
                    composer_id_to_ws=composer_id_to_ws,
                )
                ws_name = ws_id_to_name.get(ws_id)
                project_name = ws_name or ("Other chats" if ws_id == "global" else ws_id)

                model_config = _model_config_from_composer_dict(cd)
                model_name = model_config.get("modelName")
                model_names = (
                    [str(model_name)] if model_name and model_name != "default" else None
                )

                in_raw = query_lower in raw_text.lower()
                title_match = bool(title and query_lower in title.lower())
                bubble_texts: list[str] = []

                if title_match:
                    has_match, matching_text = True, title
                else:
                    has_match, matching_text = _find_match(title, [], query_lower, query)
                    if not has_match and in_raw:
                        has_match, matching_text = _find_match(
                            title, [raw_text], query_lower, query
                        )
                    if not has_match:
                        bubble_texts = bubble_texts_by_composer.get(composer_id, [])
                        has_match, matching_text = _find_match(
                            title, bubble_texts, query_lower, query
                        )
                    if not has_match:
                        continue

                all_bubble_texts = all_bubbles_by_composer.get(composer_id, [])
                exclusion_text = _composer_exclusion_text(
                    project_name=project_name,
                    title=title,
                    model_names=model_names,
                    model_config=model_config,
                    cd=cd,
                    all_bubble_texts=all_bubble_texts,
                )
                if is_excluded_by_rules(rules, exclusion_text):
                    continue

                if not title_match:
                    bubble_texts = bubble_texts or bubble_texts_by_composer.get(composer_id, [])

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
                        to_epoch_ms(cd.get("lastUpdatedAt"))
                        or to_epoch_ms(cd.get("createdAt"))
                        or _UNKNOWN_SEARCH_TIMESTAMP
                    ),
                    "matchingText": matching_text,
                    "type": "composer",
                })
            except Exception as exc:
                _logger.warning(
                    "Failed to process indexed Composer %s during search: %s",
                    composer_id,
                    exc,
                )
                parse_warnings.record_composer_processing_failure()

    except Exception as exc:
        _logger.warning("Indexed search failed, falling back to live scan: %s", exc)
        return None

    return results


def _search_global_storage_live_scan(
    workspace_path: str,
    query: str,
    query_lower: str,
    rules: list[Any],
    parse_warnings: ParseWarningCollector,
    *,
    since_ms: int | None = None,
) -> list[SearchResult]:
    """Search composer conversations by scanning Cursor's global ``cursorDiskKV``."""
    results: list[SearchResult] = []
    try:
        workspace_entries = collect_workspace_entries(workspace_path)
        assigner = _load_search_workspace_assigner(
            workspace_path, rules, workspace_entries,
        )
        if assigner is not None:
            ws_id_to_name = assigner.ws_id_to_name
            composer_id_to_ws = assigner.ctx.composer_id_to_workspace_id
        else:
            ws_id_to_name = _build_ws_id_to_name(workspace_entries)
            composer_id_to_ws = build_composer_id_to_workspace_id_cached(
                workspace_path, workspace_entries, rules,
            )

        with open_global_db(workspace_path) as (conn, _db_path):
            if conn is None:
                return results

            composer_rows = conn.execute(COMPOSER_ROWS_WITH_HEADERS_SQL).fetchall()

            window_composer_ids: set[str] | None = None
            if since_ms is not None:
                window_composer_ids = set()
                in_window_rows: list[sqlite3.Row] = []
                for row in composer_rows:
                    composer_id = row["key"].split(":")[1]
                    raw_text = _composer_row_raw_text(row)
                    try:
                        cd_probe = json.loads(raw_text)
                    except (json.JSONDecodeError, TypeError, ValueError):
                        continue
                    if not isinstance(cd_probe, dict):
                        continue
                    if not _timestamp_in_search_window(
                        _composer_dict_timestamp_ms(cd_probe), since_ms,
                    ):
                        continue
                    window_composer_ids.add(composer_id)
                    in_window_rows.append(row)
                composer_rows = in_window_rows

            bubble_texts_by_composer = _index_bubble_texts_matching_query(
                conn, query_lower, composer_ids=window_composer_ids,
            )

            for row in composer_rows:
                composer_id = row["key"].split(":")[1]
                raw_text = _composer_row_raw_text(row)
                raw_lower = raw_text.lower()
                in_raw = query_lower in raw_lower
                if not in_raw and composer_id not in bubble_texts_by_composer:
                    continue

                try:
                    cd = json.loads(raw_text)
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    _logger.warning(
                        "Failed to decode Composer from composerData:%s: %s",
                        composer_id,
                        exc,
                    )
                    parse_warnings.record_composer_skipped()
                    continue
                if not isinstance(cd, dict):
                    parse_warnings.record_composer_skipped()
                    continue

                try:
                    headers = cd.get("fullConversationHeadersOnly") or []
                    if not headers:
                        continue

                    title = cd.get("name") or ""
                    if not isinstance(title, str):
                        title = str(title) if title else ""

                    ws_id = _workspace_id_for_search_hit(
                        composer_id=composer_id,
                        cd=cd,
                        assigner=assigner,
                        composer_id_to_ws=composer_id_to_ws,
                    )
                    ws_name = ws_id_to_name.get(ws_id)
                    project_name = ws_name or ("Other chats" if ws_id == "global" else ws_id)

                    model_config = _model_config_from_composer_dict(cd)
                    model_name = model_config.get("modelName")
                    model_names = (
                        [str(model_name)] if model_name and model_name != "default" else None
                    )

                    title_match = bool(title and query_lower in title.lower())
                    bubble_texts: list[str] = []

                    if title_match:
                        has_match, matching_text = True, title
                    else:
                        has_match, matching_text = _find_match(
                            title, [], query_lower, query
                        )
                        if not has_match and in_raw:
                            has_match, matching_text = _find_match(
                                title, [raw_text], query_lower, query
                            )
                        if not has_match:
                            bubble_texts = bubble_texts_by_composer.get(composer_id, [])
                            has_match, matching_text = _find_match(
                                title, bubble_texts, query_lower, query
                            )
                        if not has_match:
                            continue

                    all_bubble_texts = _all_bubble_texts_for_composer(conn, composer_id)
                    exclusion_text = _composer_exclusion_text(
                        project_name=project_name,
                        title=title,
                        model_names=model_names,
                        model_config=model_config,
                        cd=cd,
                        all_bubble_texts=all_bubble_texts,
                    )
                    if is_excluded_by_rules(rules, exclusion_text):
                        continue

                    if not title_match:
                        bubble_texts = bubble_texts or bubble_texts_by_composer.get(composer_id, [])

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
                            to_epoch_ms(cd.get("lastUpdatedAt"))
                            or to_epoch_ms(cd.get("createdAt"))
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
    *,
    since_ms: int | None = None,
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

                raw_chat = chat_row[0]
                if isinstance(raw_chat, (bytes, bytearray)):
                    raw_chat = raw_chat.decode("utf-8", errors="replace")
                if query_lower not in str(raw_chat).lower():
                    continue

                data = json.loads(raw_chat)
                for tab in (data.get("tabs") or []):
                    ct = tab.get("chatTitle") or ""
                    tab_id = str(tab.get("tabId") or "")

                    tab_ts = to_epoch_ms(tab.get("lastSendTime")) or _UNKNOWN_SEARCH_TIMESTAMP
                    if not _timestamp_in_search_window(tab_ts, since_ms):
                        continue

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
                        "timestamp": tab_ts,
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
    *,
    since_ms: int | None = None,
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
                try:
                    session_ts = int(os.path.getmtime(session["db_path"]) * 1000)
                except OSError:
                    session_ts = 0
                effective_ms = max(created_ms or 0, session_ts)
                if not _timestamp_in_search_window(effective_ms, since_ms):
                    continue
                session_name: str = meta.get("name") or f"Session {session_id[:8]}"
                title_match = bool(session_name and query_lower in session_name.lower())

                if title_match:
                    exclusion_text = _build_exclusion_searchable(
                        project_name=ws_name,
                        chat_title=session_name,
                    )
                    if is_excluded_by_rules(rules, exclusion_text):
                        continue
                    results.append({
                        "workspaceId": f"cli:{cp['project_id']}",
                        "workspaceFolder": ws_name,
                        "chatId": session_id,
                        "chatTitle": session_name,
                        "timestamp": effective_ms,
                        "matchingText": session_name,
                        "type": "cli_agent",
                        "source": "cli",
                    })
                    continue

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

                bubbles = messages_to_bubbles(messages, effective_ms)
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
                    "timestamp": effective_ms,
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
