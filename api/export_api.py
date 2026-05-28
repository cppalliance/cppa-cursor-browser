"""
API route for export — produces per-chat Markdown in a zip download.
POST /api/export  { since: "all"|"last", zip: true }
GET  /api/export/state — returns last export time
"""

import io
import json
import logging
import os
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request

from utils.workspace_path import resolve_workspace_path
from utils.path_helpers import to_epoch_ms
from utils.text_extract import extract_text_from_bubble, slug
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.cursor_md_exporter import cursor_ide_chat_to_markdown
from services.workspace_db import (
    build_composer_id_to_workspace_id,
    collect_workspace_entries,
    load_bubble_map,
    load_code_block_diff_map,
    open_global_db,
)
from services.workspace_resolver import (
    create_project_name_to_workspace_id_map,
    lookup_workspace_display_name,
)

bp = Blueprint("export_api", __name__)
_logger = logging.getLogger(__name__)


def _get_state_dir() -> str:
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def _get_export_state() -> dict:
    """Read the export state file."""
    state_path = os.path.join(_get_state_dir(), "export_state.json")
    if os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            _logger.warning(
                "Could not read export state from %s: %s",
                state_path,
                e,
            )
    return {}


def _save_export_state(count: int):
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


@bp.route("/api/export/state")
def get_export_state():
    """Return the last export timestamp."""
    state = _get_export_state()
    return jsonify(state)


@bp.route("/api/export", methods=["POST"])
def export_chats():
    """Export chats as a zip archive.

    Exclusion rules (``EXCLUSION_RULES`` app config key) are evaluated against
    each chat's project name, title, and model.  Rules are loaded once at
    application startup; an app restart is required to pick up changes to the
    exclusion rules file.
    """
    try:
        body = request.get_json(silent=True) or {}
        since = "last" if body.get("since") == "last" else "all"

        workspace_path = resolve_workspace_path()

        # Determine last export timestamp for filtering
        last_export_ms = 0
        if since == "last":
            state = _get_export_state()
            ts_str = state.get("lastExportTime")
            if ts_str:
                last_export_ms = to_epoch_ms(ts_str)

        # ── Workspace scanning via service layer ──────────────────────────────
        workspace_entries = collect_workspace_entries(workspace_path)
        composer_id_to_ws = build_composer_id_to_workspace_id(workspace_path, workspace_entries)
        project_name_map = create_project_name_to_workspace_id_map(workspace_entries)

        # Build display-name and slug maps
        ws_id_to_slug: dict[str, str] = {}
        ws_id_to_display_name: dict[str, str] = {}
        for e in workspace_entries:
            display = lookup_workspace_display_name(workspace_path, e["name"])
            if display != e["name"]:
                ws_id_to_display_name[e["name"]] = display
                ws_id_to_slug[e["name"]] = slug(display)

        today = datetime.now().strftime("%Y-%m-%d")
        exported = []
        rules = current_app.config.get("EXCLUSION_RULES") or []

        # ── Database reading via service layer ────────────────────────────────
        with open_global_db(workspace_path) as (global_db, _):
            if global_db is None:
                return jsonify({"error": "Cursor global storage not found"}), 404

            bubble_map = load_bubble_map(global_db)
            code_block_diff_map = load_code_block_diff_map(global_db)

            try:
                composer_rows = global_db.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                    " AND value LIKE '%fullConversationHeadersOnly%'"
                    " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
                ).fetchall()
            except sqlite3.Error:
                composer_rows = []

            for row in composer_rows:
                composer_id = row["key"].split(":")[1]
                try:
                    cd = json.loads(row["value"])
                    headers = cd.get("fullConversationHeadersOnly") or []
                    if not headers:
                        continue

                    updated_at_ms = to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0
                    if since == "last" and updated_at_ms and updated_at_ms <= last_export_ms:
                        continue

                    ws_id = composer_id_to_ws.get(composer_id, "global")
                    ws_slug = "other-chats" if ws_id == "global" else (ws_id_to_slug.get(ws_id) or slug(ws_id[:12]))
                    ws_display_name = "Other chats" if ws_id == "global" else (ws_id_to_display_name.get(ws_id) or ws_slug)
                    title = cd.get("name") or f"Chat {composer_id[:8]}"
                    model_config = cd.get("modelConfig") or {}
                    model_name = model_config.get("modelName")
                    model_names = [model_name] if model_name and model_name != "default" else None

                    bubble_texts = []
                    for h in headers:
                        b = bubble_map.get(h.get("bubbleId"))
                        if b:
                            bt = extract_text_from_bubble(b)
                            if bt:
                                bubble_texts.append(bt)

                    searchable = build_searchable_text(
                        project_name=ws_display_name,
                        chat_title=title,
                        model_names=model_names,
                        chat_content_snippet="\n\n".join(bubble_texts) if bubble_texts else None,
                    )
                    if is_excluded_by_rules(rules, searchable):
                        continue

                    title_slug = slug(title)
                    ts_ms = updated_at_ms or int(datetime.now().timestamp() * 1000)
                    ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%dT%H-%M-%S")
                    filename = f"{ts_str}__{title_slug}__{composer_id[:8]}.md"
                    rel_path = os.path.join(today, ws_slug, "chat", filename)

                    md = cursor_ide_chat_to_markdown(
                        composer_data=cd,
                        composer_id=composer_id,
                        bubble_map=bubble_map,
                        code_block_diff_map=code_block_diff_map,
                        workspace_info={"ws_slug": ws_slug, "ws_display_name": ws_display_name},
                    )
                    exported.append({"path": rel_path, "content": md, "updatedAt": updated_at_ms})

                except Exception as e:
                    _logger.error(
                        "Error processing composer %s for export: %s (%s)",
                        composer_id,
                        e,
                        type(e).__name__,
                        exc_info=True,
                    )

        count = len(exported)
        if count == 0:
            return jsonify({"error": "No conversations to export" + (
                " since last export" if since == "last" else ""
            )}), 404

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["path"], entry["content"])

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
        return jsonify({"error": "Export failed"}), 500
