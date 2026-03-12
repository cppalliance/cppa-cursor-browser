"""Path utility functions mirroring src/utils/path.ts"""

import os
import sys
from datetime import datetime
from urllib.parse import unquote


def expand_tilde_path(input_path: str) -> str:
    """Expand ~ in paths and handle macOS Library paths."""
    home = os.path.expanduser("~")

    # Handle paths that start with ~/
    if input_path.startswith("~/"):
        return os.path.join(home, input_path[2:])

    # If the path already contains the home directory, return as is
    if input_path.startswith(home):
        return input_path

    # Handle macOS Library paths that should start with home dir
    if "Library/Application Support" in input_path and not input_path.startswith(home):
        return os.path.join(home, input_path)

    return input_path


def normalize_file_path(file_path: str) -> str:
    """Normalize a file path: strip file:// protocol, URL-decode, fix slashes."""
    import re

    normalized = file_path
    # Remove file:// protocol
    normalized = re.sub(r"^file:///", "", normalized)
    normalized = re.sub(r"^file://", "", normalized)

    # URL-decode the path
    try:
        normalized = unquote(normalized)
    except Exception:
        pass

    # Normalize Windows-style paths: lowercase and unify slashes.
    # Done unconditionally for paths that look like Windows absolute paths
    # (e.g. "d:\foo" or "d:/foo") so that cross-platform reads (WSL, Linux
    # reading Cursor's Windows storage) get the same result as native Win32.
    if sys.platform == "win32":
        normalized = normalized.replace("/", "\\")
        normalized = re.sub(r"^\\([a-zA-Z]:)", r"\1", normalized)
        normalized = normalized.lower()
    elif re.match(r"^[a-zA-Z]:[/\\]", normalized):
        # Windows-style absolute path on a non-Windows host.
        normalized = normalized.replace("/", "\\")
        normalized = normalized.lower()

    return normalized


def to_epoch_ms(value) -> int:
    """Convert a timestamp value to epoch milliseconds.

    Handles:
      - int/float already in ms (> 1e12) or seconds (< 1e12)
      - ISO 8601 strings like '2026-02-03T20:39:54.017Z'
      - None / unrecognised → 0
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        if value > 1e12:
            return int(value)           # already ms
        if value > 0:
            return int(value * 1000)    # seconds → ms
        return 0
    if isinstance(value, str):
        try:
            # ISO 8601 with optional fractional seconds
            cleaned = value.rstrip("Z") + "+00:00" if value.endswith("Z") else value
            dt = datetime.fromisoformat(cleaned)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
        # Maybe it's a numeric string?
        try:
            return to_epoch_ms(float(value))
        except Exception:
            pass
    return 0


def get_workspace_folder_paths(workspace_data: dict) -> list:
    """Extract folder paths from workspace.json data.

    Supports legacy and newer multi-root entry shapes:
      - {"folder": "<path>"}
      - {"folder": {"path": "<path>"}}  (defensive)
      - {"folders": [{"path": "<path>"}]}
      - {"folders": [{"uri": {"path": "<path>"}}]}
      - {"folders": ["<path>"]}         (defensive)
    """

    def _extract_path(entry) -> str | None:
        if isinstance(entry, str):
            return entry
        if not isinstance(entry, dict):
            return None
        if isinstance(entry.get("path"), str):
            return entry["path"]
        uri = entry.get("uri")
        if isinstance(uri, str):
            return uri
        if isinstance(uri, dict):
            if isinstance(uri.get("path"), str):
                return uri["path"]
            if isinstance(uri.get("fsPath"), str):
                return uri["fsPath"]
        return None

    paths = []
    folder = workspace_data.get("folder")
    folder_path = _extract_path(folder)
    if folder_path:
        paths.append(folder_path)

    folders = workspace_data.get("folders")
    if isinstance(folders, list):
        for f in folders:
            p = _extract_path(f)
            if p:
                paths.append(p)
    return paths


def get_workspace_display_name(workspace_data: dict, fallback: str | None = None) -> str:
    """Return a user-friendly workspace name from workspace.json data."""
    for folder in get_workspace_folder_paths(workspace_data):
        raw = str(folder).strip()
        cleaned = raw.replace("\\", "/").rstrip("/")
        leaf = cleaned.split("/")[-1] if cleaned else ""
        if leaf:
            decoded = unquote(leaf)
            if decoded:
                return decoded
    return fallback or ""
