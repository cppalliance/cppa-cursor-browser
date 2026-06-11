"""
API routes for configuration — mirrors:
  src/app/api/detect-environment/route.ts  GET /api/detect-environment
  src/app/api/validate-path/route.ts       POST /api/validate-path
  src/app/api/set-workspace/route.ts       POST /api/set-workspace
  src/app/api/get-username/route.ts        GET /api/get-username
"""

import logging
import os
import subprocess
import sys

from flask import Blueprint, Response, request

from api.flask_config import json_response

from utils.path_validation import WorkspacePathError, validate_workspace_path
from utils.workspace_path import set_workspace_path_override

bp = Blueprint("config_api", __name__)
_logger = logging.getLogger(__name__)


@bp.route("/api/detect-environment")
def detect_environment() -> Response:
    try:
        is_wsl = False
        is_remote = bool(
            os.environ.get("SSH_CONNECTION")
            or os.environ.get("SSH_CLIENT")
            or os.environ.get("SSH_TTY")
        )

        if sys.platform != "win32":
            try:
                release = subprocess.check_output(
                    ["uname", "-r"], text=True, stderr=subprocess.DEVNULL
                ).lower()
                is_wsl = "microsoft" in release or "wsl" in release
            except Exception:
                pass

        return json_response({
            "os": sys.platform,
            "isWSL": is_wsl,
            "isRemote": is_remote,
        })

    except Exception as e:
        _logger.warning(
            "Failed to detect environment: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return json_response({"os": "unknown", "isWSL": False, "isRemote": False})


@bp.route("/api/validate-path", methods=["POST"])
def validate_path() -> tuple[Response, int] | Response:
    """Same path rules as POST /api/set-workspace: realpath, markers (issue #15)."""
    try:
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return json_response(
                {"valid": False, "error": "invalid JSON body", "workspaceCount": 0}
            )
        raw = body.get("path", "")
        try:
            canonical = validate_workspace_path(raw)
        except WorkspacePathError as e:
            return json_response({"valid": False, "error": str(e), "workspaceCount": 0})

        workspace_count = 0
        for name in os.listdir(canonical):
            full = os.path.join(canonical, name)
            if os.path.isdir(full):
                db = os.path.join(full, "state.vscdb")
                if os.path.isfile(db):
                    workspace_count += 1

        return json_response(
            {
                "valid": workspace_count > 0,
                "workspaceCount": workspace_count,
                "path": canonical,
            }
        )

    except Exception as e:
        _logger.error(
            "Validation error: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return json_response({"valid": False, "error": "Failed to validate path"}, 500)
@bp.route("/api/set-workspace", methods=["POST"])
def set_workspace() -> tuple[Response, int] | Response:
    # Reject non-dict JSON bodies (array / string / number / null). Without
    # this, get_json returns the value directly, the truthy fallback `or {}`
    # is bypassed, and `body.get("path", "")` raises AttributeError — which
    # the outer Exception handler then mis-reports as a 500 server error
    # instead of a 400 client error. (CodeRabbit on PR #16.)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return json_response({"error": "request body must be a JSON object"}, 400)
    raw = body.get("path", "")
    # Validate the supplied path BEFORE storing the override (issue #15).
    # validate_workspace_path collapses `..` traversal AND resolves symlinks
    # via realpath, then enforces that the canonical target is an existing
    # directory containing Cursor workspace markers. Returns the canonical
    # path so we store that, not whatever the caller sent.
    try:
        canonical = validate_workspace_path(raw)
    except WorkspacePathError as e:
        return json_response({"error": str(e)}, 400)
    except Exception:  # noqa: BLE001 — only here as a fallback
        return json_response({"error": "Failed to validate workspace path"}, 500)
    try:
        set_workspace_path_override(canonical)
    except Exception:  # noqa: BLE001 — keep the response shape structured JSON
        return json_response({"error": "Failed to set workspace path"}, 500)
    return json_response({"success": True, "path": canonical})


@bp.route("/api/get-username")
def get_username() -> Response:
    try:
        username = "YOUR_USERNAME"

        if sys.platform == "win32":
            username = os.environ.get("USERNAME") or os.getlogin()
        else:
            try:
                output = subprocess.check_output(
                    ["cmd.exe", "/c", "echo", "%USERNAME%"],
                    text=True,
                    stderr=subprocess.DEVNULL,
                )
                username = output.strip()
            except Exception:
                import getpass
                username = getpass.getuser()

        return json_response({"username": username})

    except Exception as e:
        _logger.warning(
            "Failed to get username: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return json_response({"username": "YOUR_USERNAME"})
