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
from typing import Any

from flask import Blueprint, Response, request

from api.flask_config import exclusion_rules, json_response

from utils.workspace_path import resolve_workspace_path, get_cli_chats_path
from utils.cli_chat_reader import list_cli_projects
from utils.path_helpers import (
    get_workspace_folder_paths,
    get_workspace_display_name,
    warn_workspace_json_read,
)
from utils.workspace_descriptor import read_json_file
from services.workspace_resolver import (
    infer_workspace_name_from_context,
    lookup_workspace_display_name,
)
from services.cli_tabs import get_cli_workspace_tabs
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import (
    assemble_single_tab,
    assemble_workspace_tabs,
    list_workspace_tab_summaries,
)

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

def _request_nocache() -> bool:
    return request.args.get("nocache") in ("1", "true")


@bp.route("/api/workspaces")
def list_workspaces() -> tuple[Response, int] | Response:
    """List workspace projects for the sidebar (GET /api/workspaces).

    Honors ``?nocache=1`` to bypass the summary disk cache.

    Returns:
        JSON with ``projects`` and optional ``warnings``. 500 on failure.
    """
    try:
        workspace_path = resolve_workspace_path()
        rules = exclusion_rules()
        projects, warnings = list_workspace_projects(
            workspace_path, rules, nocache=_request_nocache(),
        )
        payload: dict[str, Any] = {"projects": projects}
        if warnings:
            payload["warnings"] = warnings
        return json_response(payload)
    except Exception:
        _logger.exception("Failed to get workspaces")
        return json_response({"error": "Failed to get workspaces"}, 500)
# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>")
def get_workspace(workspace_id: str) -> tuple[Response, int] | Response:
    """Return metadata for one workspace, global bucket, or CLI project.

    Args:
        workspace_id: Storage folder name, ``global``, or ``cli:<project_id>``.

    Returns:
        Workspace JSON (id, name, path, folder, lastModified). 404 when not found;
        500 on unexpected failure.
    """
    try:
        if workspace_id == "global":
            return json_response({
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
                return json_response({
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
            return json_response({"error": "CLI project not found"}, 404)
        workspace_path = resolve_workspace_path()
        db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
        wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")

        if not os.path.isfile(db_path):
            return json_response({"error": "Workspace not found"}, 404)
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

        return json_response({
            "id": workspace_id,
            "name": workspace_name,
            "path": db_path,
            "folder": folder,
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        })

    except Exception:
        _logger.exception("Failed to get workspace")
        return json_response({"error": "Failed to get workspace"}, 500)
# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>/tabs")
def get_workspace_tabs(workspace_id: str) -> tuple[Response, int] | Response:
    """List conversation tabs for a workspace (GET /api/workspaces/<id>/tabs).

    Args:
        workspace_id: Storage folder name or ``cli:<project_id>``.

    Query params: ``summary=1`` for lightweight tab headers only; ``nocache=1`` to
    bypass cache on summary requests.

    Returns:
        Tabs payload from :func:`services.workspace_tabs` helpers. 500 on failure.
    """
    if workspace_id.startswith("cli:"):
        try:
            return get_cli_workspace_tabs(workspace_id, exclusion_rules())
        except Exception:
            _logger.exception("Failed to get CLI workspace tabs")
            return json_response({"error": "Failed to get workspace tabs"}, 500)
    try:
        workspace_path = resolve_workspace_path()
        rules = exclusion_rules()
        summary = request.args.get("summary") in ("1", "true")
        if summary:
            payload, status = list_workspace_tab_summaries(
                workspace_id, workspace_path, rules, nocache=_request_nocache(),
            )
        else:
            payload, status = assemble_workspace_tabs(workspace_id, workspace_path, rules)
        return json_response(payload, status)
    except Exception:
        _logger.exception("Failed to get workspace tabs")
        return json_response({"error": "Failed to get workspace tabs"}, 500)
# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs/<composer_id>
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>/tabs/<composer_id>")
def get_workspace_tab(workspace_id: str, composer_id: str) -> tuple[Response, int] | Response:
    """Lazy-load one conversation tab (GET /api/workspaces/<id>/tabs/<composer_id>).

    Args:
        workspace_id: IDE workspace folder name (CLI workspaces return 400).
        composer_id: Composer UUID to load.

    Returns:
        Single-tab JSON from :func:`services.workspace_tabs.assemble_single_tab`.
        400 for CLI workspaces; 500 on unexpected failure.
    """
    if workspace_id.startswith("cli:"):
        return json_response({"error": "Per-tab lazy load is not supported for CLI workspaces"}, 400)
    try:
        workspace_path = resolve_workspace_path()
        rules = exclusion_rules()
        payload, status = assemble_single_tab(workspace_id, composer_id, workspace_path, rules)
        return json_response(payload, status)
    except Exception:
        _logger.exception("Failed to get workspace tab")
        return json_response({"error": "Failed to get workspace tab"}, 500)