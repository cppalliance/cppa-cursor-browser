"""
API route for search — mirrors src/app/api/search/route.ts
GET /api/search?q=...&type=all|chat|composer
"""

import logging
from typing import Any

from flask import Blueprint, Response, current_app, jsonify, request

from models import ParseWarningCollector, SearchResult
from services.search import (
    rank_results,
    search_cli_sessions,
    search_global_storage,
    search_legacy_workspaces,
)
from utils.workspace_path import get_cli_chats_path, resolve_workspace_path

bp = Blueprint("search", __name__)
_logger = logging.getLogger(__name__)


@bp.route("/api/search")
def search() -> tuple[Response, int] | Response:
    try:
        query = request.args.get("q", "").strip()
        search_type = request.args.get("type", "all")
        rules = current_app.config.get("EXCLUSION_RULES") or []

        if not query:
            return jsonify({"error": "No search query provided"}), 400

        workspace_path = resolve_workspace_path()
        parse_warnings = ParseWarningCollector()
        query_lower = query.lower()

        results: list[SearchResult] = []
        results.extend(
            search_global_storage(workspace_path, query, query_lower, rules, parse_warnings)
        )
        results.extend(
            search_legacy_workspaces(workspace_path, query, query_lower, search_type, rules)
        )
        if search_type == "all":
            results.extend(
                search_cli_sessions(get_cli_chats_path(), query, query_lower, rules)
            )

        payload: dict[str, Any] = {"results": rank_results(results)}
        return jsonify(parse_warnings.attach_to(payload))

    except Exception:
        _logger.exception("Search failed")
        return jsonify({"error": "Search failed", "results": []}), 500
