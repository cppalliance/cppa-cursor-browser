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

from api.flask_config import api_error, json_response

from utils.path_validation import WorkspacePathError, validate_workspace_path
from utils.workspace_path import set_workspace_path_override

bp = Blueprint("config_api", __name__)
_logger = logging.getLogger(__name__)

_WORKSPACE_PATH_ERROR_CODES: dict[str, str] = {
    "path is required": "path_required",
    "path does not exist": "path_not_found",
    "path is not a directory": "path_not_directory",
    (
        "path does not look like a Cursor workspaceStorage directory "
        "(no immediate subdirectory contains state.vscdb)"
    ): "path_not_workspace_storage",
}


def _workspace_path_error_code(message: str) -> str:
    return _WORKSPACE_PATH_ERROR_CODES.get(message, "invalid_workspace_path")


def _validate_path_error(
    message: str,
    code: str,
) -> Response | tuple[Response, int]:
    return json_response(
        {"valid": False, "error": message, "code": code, "workspaceCount": 0},
    )


def _parse_workspace_path_from_body(body: object) -> object | None:
    """Return the raw ``path`` field when *body* is a JSON object; else ``None``."""
    if not isinstance(body, dict):
        return None
    path: object = body.get("path", "")
    return path


def _canonicalize_workspace_path(raw: object) -> tuple[str | None, str | None]:
    """Return ``(canonical, None)`` or ``(None, error_message)`` on validation failure."""
    try:
        # validate_workspace_path raises WorkspacePathError for non-str/empty input.
        return validate_workspace_path(raw), None  # type: ignore[arg-type]
    except WorkspacePathError as e:
        return None, str(e)


@bp.route("/api/detect-environment")
def detect_environment() -> Response:
    """Detect runtime OS, WSL, and SSH-remote context (GET /api/detect-environment).

    Returns:
        JSON with ``os``, ``isWSL``, and ``isRemote``. Falls back to safe defaults
        on detection errors.
    """
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
    """Validate a workspace storage path without persisting it (POST /api/validate-path).

    Uses the same rules as :func:`set_workspace` (realpath, Cursor markers; issue #15).

    Args:
        path: Workspace storage root from JSON body ``{"path": "..."}``.

    Returns:
        JSON with ``valid``, ``workspaceCount``, and canonical ``path`` on success.
        ``valid`` is ``false`` when the path fails validation or contains no
        workspace folders with ``state.vscdb``. Invalid JSON body returns
        ``{"valid": false, "error": "invalid JSON body", "code": "invalid_json_body", "workspaceCount": 0}``.
        Path validation errors return
        ``{"valid": false, "error": "...", "code": "...", "workspaceCount": 0}``.
        500 with
        ``{"valid": false, "error": "Failed to validate path", "code": "validate_path_failed", "workspaceCount": 0}``
        on unexpected failure.
    """
    try:
        raw = _parse_workspace_path_from_body(request.get_json(silent=True))
        if raw is None:
            return _validate_path_error("invalid JSON body", "invalid_json_body")
        canonical, message = _canonicalize_workspace_path(raw)
        if message is not None:
            return _validate_path_error(message, _workspace_path_error_code(message))
        assert canonical is not None  # paired with successful validation above

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
        return json_response(
            {"valid": False, "error": "Failed to validate path", "code": "validate_path_failed", "workspaceCount": 0},
            500,
        )


@bp.route("/api/set-workspace", methods=["POST"])
def set_workspace() -> tuple[Response, int] | Response:
    """Persist a validated workspace storage path (POST /api/set-workspace).

    Args:
        path: Workspace storage root from JSON body ``{"path": "..."}``.
            Canonicalized via :func:`utils.path_validation.validate_workspace_path`
            before storing the thread-safe module override.

    Returns:
        ``{"success": true, "path": "..."}`` on success. 400 for invalid path or
        body; 500 when override storage fails.
    """
    # Reject non-dict JSON bodies (array / string / number / null). Without
    # this, get_json returns the value directly, the truthy fallback `or {}`
    # is bypassed, and `body.get("path", "")` raises AttributeError — which
    # the outer Exception handler then mis-reports as a 500 server error
    # instead of a 400 client error. (CodeRabbit on PR #16.)
    body = request.get_json(silent=True)
    raw = _parse_workspace_path_from_body(body)
    if raw is None:
        return api_error("request body must be a JSON object", "invalid_json_body", 400)
    # Validate the supplied path BEFORE storing the override (issue #15).
    # validate_workspace_path collapses `..` traversal AND resolves symlinks
    # via realpath, then enforces that the canonical target is an existing
    # directory containing Cursor workspace markers. Returns the canonical
    # path so we store that, not whatever the caller sent.
    try:
        canonical, message = _canonicalize_workspace_path(raw)
    except Exception:  # noqa: BLE001 — only here as a fallback
        return api_error("Failed to validate workspace path", "validate_workspace_path_failed", 500)
    if message is not None:
        return api_error(message, _workspace_path_error_code(message), 400)
    try:
        set_workspace_path_override(canonical)
    except Exception:  # noqa: BLE001 — keep the response shape structured JSON
        return api_error("Failed to set workspace path", "set_workspace_path_failed", 500)
    return json_response({"success": True, "path": canonical})


@bp.route("/api/get-username")
def get_username() -> Response:
    """Return the detected Windows/WSL username (GET /api/get-username).

    Returns:
        JSON ``{"username": "..."}``. Falls back to ``YOUR_USERNAME`` when
        detection fails.
    """
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
