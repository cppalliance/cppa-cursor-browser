"""
API routes for workspaces — mirrors:
  src/app/api/workspaces/route.ts            GET /api/workspaces
  src/app/api/workspaces/[id]/route.ts       GET /api/workspaces/<id>
  src/app/api/workspaces/[id]/tabs/route.ts  GET /api/workspaces/<id>/tabs
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify

from utils.workspace_path import resolve_workspace_path, get_cli_chats_path
from utils.cli_chat_reader import list_cli_projects
from utils.path_helpers import (
    get_workspace_folder_paths,
    get_workspace_display_name,
    warn_workspace_json_read,
)
from utils.workspace_descriptor import read_json_file
from services.workspace_resolver import (
    determine_project_for_conversation,
    infer_invalid_workspace_aliases,
    infer_workspace_name_from_context,
    lookup_workspace_display_name,
)
from services.cli_tabs import get_cli_workspace_tabs
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import assemble_workspace_tabs

# Re-exported for back-compat with tests that import from api.workspaces directly.
_determine_project_for_conversation = determine_project_for_conversation  # noqa: F401
_infer_invalid_workspace_aliases = infer_invalid_workspace_aliases  # noqa: F401
_get_workspace_display_name = lookup_workspace_display_name  # noqa: F401
_infer_workspace_name_from_context = infer_workspace_name_from_context  # noqa: F401

# Re-exported for tests/test_models_wired_at_read_sites.py — the typed-model
# spy harness patches `workspaces_mod.Bubble` / `.Composer` / `.Workspace` to
# verify that production read paths actually call from_dict. The classes
# themselves are wired inside the services modules now (post-#25 split);
# importing them here keeps the spy resolution stable.
from models import Bubble, Composer, Workspace  # noqa: F401

bp = Blueprint("workspaces", __name__)
_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces")
def list_workspaces():
    try:
        workspace_path = resolve_workspace_path()
        rules = current_app.config.get("EXCLUSION_RULES") or []
        projects, warnings = list_workspace_projects(workspace_path, rules)
        payload: dict = {"projects": projects}
        if warnings:
            payload["warnings"] = warnings
        return jsonify(payload)
    except Exception:
        _logger.exception("Failed to get workspaces")
        return jsonify({"error": "Failed to get workspaces"}), 500


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>")
def get_workspace(workspace_id):
    try:
        if workspace_id == "global":
            return jsonify({
                "id": "global",
                "name": "Other chats",
                "path": None,
                "folder": None,
                "lastModified": datetime.now(tz=timezone.utc).isoformat(),
            })

        if workspace_id.startswith("cli:"):
            project_id = workspace_id[4:]
            cli_projects = list_cli_projects(get_cli_chats_path())
            for cp in cli_projects:
                if not isinstance(cp, dict) or cp.get("project_id") != project_id:
                    continue
                last_ms = cp.get("last_updated_ms")
                workspace_path_field = cp.get("workspace_path")
                return jsonify({
                    "id": workspace_id,
                    "name": cp.get("workspace_name") or project_id[:12],
                    "path": workspace_path_field,
                    "folder": workspace_path_field,
                    "lastModified": (
                        datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                        if last_ms
                        else datetime.now(tz=timezone.utc).isoformat()
                    ),
                    "source": "cli",
                })
            return jsonify({"error": "CLI project not found"}), 404

        workspace_path = resolve_workspace_path()
        db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
        wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")

        if not os.path.isfile(db_path):
            return jsonify({"error": "Workspace not found"}), 404

        mtime = os.path.getmtime(db_path)
        folder = None
        workspace_name = workspace_id
        try:
            wd = read_json_file(wj_path)
            folder_paths = get_workspace_folder_paths(wd)
            folder = folder_paths[0] if folder_paths else wd.get("folder")
            derived_name = get_workspace_display_name(wd)
            if derived_name:
                workspace_name = derived_name
            elif workspace_name == workspace_id:
                inferred = infer_workspace_name_from_context(workspace_path, workspace_id)
                if inferred:
                    workspace_name = inferred
        except Exception as e:
            warn_workspace_json_read(_logger, workspace_id, e)
            inferred = infer_workspace_name_from_context(workspace_path, workspace_id)
            if inferred:
                workspace_name = inferred

        return jsonify({
            "id": workspace_id,
            "name": workspace_name,
            "path": db_path,
            "folder": folder,
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        })

    except Exception:
        _logger.exception("Failed to get workspace")
        return jsonify({"error": "Failed to get workspace"}), 500


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>/tabs")
def get_workspace_tabs(workspace_id):
    if workspace_id.startswith("cli:"):
        return get_cli_workspace_tabs(workspace_id)
    try:
        workspace_path = resolve_workspace_path()
        rules = current_app.config.get("EXCLUSION_RULES") or []
        payload, status = assemble_workspace_tabs(workspace_id, workspace_path, rules)
        return jsonify(payload), status
    except Exception:
        _logger.exception("Failed to get workspace tabs")
        return jsonify({"error": "Failed to get workspace tabs"}), 500

