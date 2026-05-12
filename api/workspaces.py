"""
API routes for workspaces — mirrors:
  src/app/api/workspaces/route.ts            GET /api/workspaces
  src/app/api/workspaces/[id]/route.ts       GET /api/workspaces/<id>
  src/app/api/workspaces/[id]/tabs/route.ts  GET /api/workspaces/<id>/tabs
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from flask import Blueprint, current_app, jsonify

from utils.workspace_path import resolve_workspace_path, get_cli_chats_path
from utils.cli_chat_reader import list_cli_projects
from utils.path_helpers import get_workspace_folder_paths, get_workspace_display_name
from utils.workspace_descriptor import _read_json_file
from services.workspace_resolver import (
    _infer_workspace_name_from_context,
    # Re-exported for back-compat with existing tests that import from api.workspaces
    # directly (test_invalid_workspace_aliases, test_workspace_assignment_fallback,
    # test_workspace_name_inference).  Production callers should import from
    # services.workspace_resolver instead.
    _determine_project_for_conversation,  # noqa: F401
    _infer_invalid_workspace_aliases,  # noqa: F401
)
from services.cli_tabs import _get_cli_workspace_tabs
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import assemble_workspace_tabs

bp = Blueprint("workspaces", __name__)


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces")
def list_workspaces():
    try:
        workspace_path = resolve_workspace_path()
        rules = current_app.config.get("EXCLUSION_RULES") or []
        projects = list_workspace_projects(workspace_path, rules)
        return jsonify(projects)
    except Exception as e:
        print(f"Failed to get workspaces: {e}")
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
                if cp["project_id"] == project_id:
                    last_ms = cp["last_updated_ms"]
                    return jsonify({
                        "id": workspace_id,
                        "name": cp["workspace_name"] or project_id[:12],
                        "path": cp["workspace_path"],
                        "folder": cp["workspace_path"],
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
            wd = _read_json_file(wj_path)
            folder_paths = get_workspace_folder_paths(wd)
            folder = folder_paths[0] if folder_paths else wd.get("folder")
            derived_name = get_workspace_display_name(wd)
            if derived_name:
                workspace_name = derived_name
            elif workspace_name == workspace_id:
                inferred = _infer_workspace_name_from_context(workspace_path, workspace_id)
                if inferred:
                    workspace_name = inferred
        except Exception:
            inferred = _infer_workspace_name_from_context(workspace_path, workspace_id)
            if inferred:
                workspace_name = inferred

        return jsonify({
            "id": workspace_id,
            "name": workspace_name,
            "path": db_path,
            "folder": folder,
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        })

    except Exception as e:
        print(f"Failed to get workspace: {e}")
        return jsonify({"error": "Failed to get workspace"}), 500


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>/tabs")
def get_workspace_tabs(workspace_id):
    if workspace_id.startswith("cli:"):
        return _get_cli_workspace_tabs(workspace_id)
    try:
        workspace_path = resolve_workspace_path()
        rules = current_app.config.get("EXCLUSION_RULES") or []
        payload, status = assemble_workspace_tabs(workspace_id, workspace_path, rules)
        return jsonify(payload), status
    except Exception as e:
        print(f"Failed to get workspace tabs: {e}")
        return jsonify({"error": "Failed to get workspace tabs"}), 500

