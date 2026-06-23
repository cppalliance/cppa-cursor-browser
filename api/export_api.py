"""
API route for export — produces per-chat Markdown in a zip download.
POST /api/export  { since: "all"|"last", zip: true }
GET  /api/export/state — returns last export time
"""

from __future__ import annotations

import io
import json
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from flask import Blueprint, Response, request

from api.flask_config import exclusion_rules, json_response
from services.export_engine import collect_export_entries
from services.workspace_db import global_storage_db_path
from utils.path_helpers import to_epoch_ms
from utils.workspace_path import resolve_workspace_path

bp = Blueprint("export_api", __name__)
_logger = logging.getLogger(__name__)


def _get_state_dir() -> str:
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def _get_export_state() -> dict[str, Any]:
    """Read the export state file."""
    state_path = os.path.join(_get_state_dir(), "export_state.json")
    if os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if isinstance(parsed, dict):
                return parsed
            _logger.warning(
                "Export state in %s is not a JSON object (got %s); ignoring",
                state_path,
                type(parsed).__name__,
            )
        except (json.JSONDecodeError, ValueError, OSError) as e:
            _logger.warning(
                "Could not read export state from %s: %s",
                state_path,
                e,
            )
    return {}


def _save_export_state(count: int) -> None:
    """Save export state after an export."""
    state_dir = _get_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    state = {
        "lastExportTime": datetime.now().isoformat(),
        "exportedCount": count,
    }
    state_path = os.path.join(state_dir, "export_state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _read_last_export_ms(since: Literal["all", "last"]) -> int:
    if since != "last":
        return 0
    ts = _get_export_state().get("lastExportTime")
    if ts:
        return to_epoch_ms(ts)
    return 0


@bp.route("/api/export/state")
def get_export_state() -> Response:
    """Return the last export timestamp."""
    state = _get_export_state()
    return json_response(state)


@bp.route("/api/export", methods=["POST"])
def export_chats() -> tuple[Response, int] | Response:
    """Export chats as a zip archive.

    Exclusion rules (``EXCLUSION_RULES`` app config key) are evaluated against
    each chat's project name, title, and model.  Rules are loaded once at
    application startup; an app restart is required to pick up changes to the
    exclusion rules file.
    """
    try:
        body = request.get_json(silent=True) or {}
        since: Literal["all", "last"] = (
            "last" if body.get("since") == "last" else "all"
        )

        workspace_path = resolve_workspace_path()
        gdb = global_storage_db_path(workspace_path)
        if not os.path.isfile(gdb):
            return json_response({"error": "Cursor global storage not found"}, 404)

        exported = collect_export_entries(
            workspace_path=workspace_path,
            exclusion_rules=exclusion_rules(),
            since=since,
            last_export_ms=_read_last_export_ms(since),
            out_dir="",
            include_composer=True,
            include_cli=False,
        )
        count = len(exported)
        if count == 0:
            return json_response(
                {"error": "No conversations to export" + (
                    " since last export" if since == "last" else ""
                )},
                404,
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["rel_path"], entry["content"])

        buf.seek(0)
        _save_export_state(count)

        filename = "cursor-export.zip"
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Count": str(count),
            },
        )

    except Exception as e:
        _logger.error(
            "Export failed: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return json_response({"error": "Export failed"}, 500)
