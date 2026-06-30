"""
API route for search — mirrors src/app/api/search/route.ts
GET /api/search?q=...&type=all|chat|composer&all_history=1&workspace=<id>
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
from typing import Any

from flask import Blueprint, Response, current_app, request

from api.flask_config import json_response

from models import ParseWarningCollector, SearchResult
from services.search import (
    DEFAULT_SEARCH_WINDOW_DAYS,
    rank_results,
    resolve_search_since_ms,
    search_cli_sessions,
    search_global_storage,
    search_legacy_workspaces,
)
from utils.cli_chat_reader import list_cli_projects
from utils.workspace_path import get_cli_chats_path, resolve_workspace_path

bp = Blueprint("search", __name__)
_logger = logging.getLogger(__name__)

_MAX_SEARCH_SINCE_DAYS = 36_500  # ~100 years; avoids timedelta overflow on bad input
_MAX_SEARCH_QUERY_LEN = 500
_VALID_SEARCH_TYPES = frozenset({"all", "chat", "composer"})
_SAFE_WORKSPACE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _parse_since_days_param(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        days = int(raw)
    except ValueError:
        return None
    if days <= 0 or days > _MAX_SEARCH_SINCE_DAYS:
        return None
    return days


def _search_error(
    message: str,
    code: str,
    status: int,
) -> tuple[Response, int]:
    return json_response({"error": message, "code": code}, status)


def _is_safe_workspace_folder_id(workspace_id: str) -> bool:
    """Return whether *workspace_id* is a safe Cursor workspace folder name."""
    if not workspace_id or workspace_id in {".", ".."}:
        return False
    if (
        os.path.isabs(workspace_id)
        or ".." in workspace_id
        or "/" in workspace_id
        or "\\" in workspace_id
    ):
        return False
    return _SAFE_WORKSPACE_ID_RE.fullmatch(workspace_id) is not None


def _workspace_exists(workspace_id: str, workspace_path: str) -> bool:
    if workspace_id == "global":
        return True
    if workspace_id.startswith("cli:"):
        project_id = workspace_id[4:]
        if not _is_safe_workspace_folder_id(project_id):
            return False
        return any(
            cp.get("project_id") == project_id
            for cp in list_cli_projects(get_cli_chats_path())
        )
    if not _is_safe_workspace_folder_id(workspace_id):
        return False
    candidate = os.path.join(workspace_path, workspace_id)
    root = os.path.normpath(workspace_path)
    joined = os.path.normpath(candidate)
    if os.path.commonpath([root, joined]) != root:
        return False
    return os.path.isdir(joined)


def _filter_results_by_workspace(
    results: list[SearchResult],
    workspace_id: str,
) -> list[SearchResult]:
    return [r for r in results if r.get("workspaceId") == workspace_id]


@bp.route("/api/search")
def search() -> tuple[Response, int] | Response:
    """Search chats, composers, and CLI sessions across Cursor storage.

    Args:
        q: Search query string (required; 400 when empty).
        type: Filter scope — ``all`` (default), ``chat``, or ``composer``.
        workspace: Optional workspace folder hash; 404 when unknown (bonus API
            filter — not exposed in the search UI).

    Returns:
        JSON ``{"results": [...]}`` with optional ``warnings``. Structured
        ``{"error", "code"}`` bodies for 400/404/503/500 failures. Error
        responses omit ``results`` (breaking change vs. legacy 500 bodies).
    """
    query = request.args.get("q", "").strip()
    if not query:
        return _search_error("No search query provided", "empty_query", 400)
    if len(query) > _MAX_SEARCH_QUERY_LEN:
        return _search_error("Search query is too long", "query_too_long", 400)

    search_type = request.args.get("type", "all")
    if search_type not in _VALID_SEARCH_TYPES:
        return _search_error("Invalid search type", "invalid_type", 400)

    since_days_raw = request.args.get("since_days")
    if (
        since_days_raw is not None
        and str(since_days_raw).strip()
        and _parse_since_days_param(since_days_raw) is None
    ):
        return _search_error("Invalid since_days parameter", "invalid_since_days", 400)

    workspace_filter = request.args.get("workspace", "").strip() or None

    try:
        workspace_path = resolve_workspace_path()
        if workspace_filter and not _workspace_exists(workspace_filter, workspace_path):
            return _search_error("Workspace not found", "workspace_not_found", 404)

        rules = current_app.config.get("EXCLUSION_RULES") or []
        all_history = request.args.get("all_history") in ("1", "true")
        since_ms = resolve_search_since_ms(
            all_history=all_history,
            since_days=_parse_since_days_param(since_days_raw),
        )

        parse_warnings = ParseWarningCollector()
        query_lower = query.lower()

        results: list[SearchResult] = []
        if search_type != "chat":
            results.extend(
                search_global_storage(
                    workspace_path,
                    query,
                    query_lower,
                    rules,
                    parse_warnings,
                    since_ms=since_ms,
                )
            )
        results.extend(
            search_legacy_workspaces(
                workspace_path,
                query,
                query_lower,
                search_type,
                rules,
                since_ms=since_ms,
            )
        )
        if search_type == "all":
            results.extend(
                search_cli_sessions(
                    get_cli_chats_path(),
                    query,
                    query_lower,
                    rules,
                    parse_warnings,
                    since_ms=since_ms,
                )
            )

        ranked = rank_results(results)
        if workspace_filter:
            ranked = _filter_results_by_workspace(ranked, workspace_filter)

        payload: dict[str, Any] = {
            "results": ranked,
            "allHistory": since_ms is None,
            "searchWindowDays": (
                None if since_ms is None else (
                    _parse_since_days_param(since_days_raw)
                    or DEFAULT_SEARCH_WINDOW_DAYS
                )
            ),
        }
        return json_response(parse_warnings.attach_to(payload))

    except sqlite3.OperationalError:
        _logger.exception("Search index unavailable")
        return _search_error(
            "Search index is temporarily unavailable",
            "search_index_unavailable",
            503,
        )
    except OSError:
        _logger.exception("Workspace storage unavailable")
        return _search_error(
            "Workspace storage is temporarily unavailable",
            "storage_unavailable",
            503,
        )
    except Exception:
        _logger.exception("Search failed")
        return _search_error("Search failed", "internal_error", 500)
