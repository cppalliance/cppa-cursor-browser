"""
API route for search — mirrors src/app/api/search/route.ts
GET /api/search?q=...&type=all|chat|composer&all_history=1
"""

import logging
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
from utils.workspace_path import get_cli_chats_path, resolve_workspace_path

bp = Blueprint("search", __name__)
_logger = logging.getLogger(__name__)


def _parse_since_days_param(raw: str | None) -> int | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@bp.route("/api/search")
def search() -> tuple[Response, int] | Response:
    try:
        query = request.args.get("q", "").strip()
        search_type = request.args.get("type", "all")
        rules = current_app.config.get("EXCLUSION_RULES") or []
        all_history = request.args.get("all_history") in ("1", "true")
        since_ms = resolve_search_since_ms(
            all_history=all_history,
            since_days=_parse_since_days_param(request.args.get("since_days")),
        )

        if not query:
            return json_response({"error": "No search query provided"}, 400)
        workspace_path = resolve_workspace_path()
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

        payload: dict[str, Any] = {
            "results": rank_results(results),
            "allHistory": since_ms is None,
            "searchWindowDays": (
                None if since_ms is None else (
                    _parse_since_days_param(request.args.get("since_days"))
                    or DEFAULT_SEARCH_WINDOW_DAYS
                )
            ),
        }
        return json_response(parse_warnings.attach_to(payload))

    except Exception:
        _logger.exception("Search failed")
        return json_response({"error": "Search failed", "results": []}, 500)