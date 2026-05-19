"""Workspace path detection mirroring src/utils/workspace-path.ts"""

from __future__ import annotations

import os
import sys
import subprocess
import threading

from .path_helpers import expand_tilde_path

# Module-level override set via POST /api/set-workspace (or --base-dir).
# Reads and writes are serialized by _workspace_path_lock so threaded WSGI
# workers (gunicorn --threads, waitress, etc.) always see the latest override
# from another thread and resolve_workspace_path's snapshot+expand stays consistent.
_workspace_path_lock = threading.Lock()
_workspace_path_override: str | None = None


def set_workspace_path_override(path: str | None) -> None:
    global _workspace_path_override
    with _workspace_path_lock:
        _workspace_path_override = path


def get_workspace_path_override() -> str | None:
    with _workspace_path_lock:
        return _workspace_path_override


def get_default_workspace_path() -> str:
    """Detect the default Cursor workspace storage path based on OS."""
    home = os.path.expanduser("~")
    release = os.uname().release.lower() if hasattr(os, "uname") else ""
    is_wsl = "microsoft" in release or "wsl" in release
    is_remote = bool(
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
        or os.environ.get("SSH_TTY")
    )

    if is_wsl:
        username = os.getenv("USER", "")
        try:
            output = subprocess.check_output(
                ["cmd.exe", "/c", "echo", "%USERNAME%"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
            username = output.strip()
        except Exception:
            pass
        return f"/mnt/c/Users/{username}/AppData/Roaming/Cursor/User/workspaceStorage"

    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Roaming", "Cursor", "User", "workspaceStorage")
    elif sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Cursor", "User", "workspaceStorage")
    elif sys.platform == "linux":
        if is_remote:
            return os.path.join(home, ".cursor-server", "data", "User", "workspaceStorage")
        return os.path.join(home, ".config", "Cursor", "User", "workspaceStorage")
    else:
        return os.path.join(home, "workspaceStorage")


def resolve_workspace_path() -> str:
    """Return the effective workspace path (override > env var > default).

    Override comes from POST /api/set-workspace (validated). ``WORKSPACE_PATH``
    is only tilde-expanded — trusted-operator escape hatch, not the same checks
    as the API (issue #15).
    """
    with _workspace_path_lock:
        override = _workspace_path_override
    if override:
        return expand_tilde_path(override)
    env_path = os.environ.get("WORKSPACE_PATH", "").strip()
    if env_path:
        return expand_tilde_path(env_path)
    return get_default_workspace_path()


def get_cli_chats_path() -> str:
    """Return the Cursor CLI chats directory (~/.cursor/chats).

    This is where the ``agent`` CLI stores chat sessions, independent of
    platform and completely separate from the IDE workspace storage.

    Override with the ``CLI_CHATS_PATH`` environment variable (useful in tests).
    """
    env_path = os.environ.get("CLI_CHATS_PATH", "").strip()
    if env_path:
        return expand_tilde_path(env_path)
    return os.path.join(os.path.expanduser("~"), ".cursor", "chats")
