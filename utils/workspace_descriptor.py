from __future__ import annotations

import json
import os
import re
import sys
from typing import Any
from urllib.parse import unquote, urlparse


def read_json_file(path: str) -> Any:
    """Read a workspace.json with Cursor indirection applied."""
    return _resolve_workspace_descriptor(path)


def _uri_or_path_to_fs_path(value: str, base_dir: str | None = None) -> str:
    """Convert a file URI or plain path to a filesystem path."""
    raw = (value or "").strip()
    if not raw:
        return ""

    if raw.startswith("file://"):
        parsed = urlparse(raw)
        path = unquote(parsed.path or "")
        if sys.platform == "win32" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return os.path.normpath(path)

    expanded = os.path.expanduser(raw)
    if base_dir and not os.path.isabs(expanded):
        expanded = os.path.join(base_dir, expanded)
    return os.path.normpath(expanded)


def _resolve_workspace_descriptor(path: str, depth: int = 0) -> Any:
    """Read a workspace descriptor, following {"workspace": ...} indirection and normalising relative folder paths."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Cursor workspaceStorage entry may point to an external workspace file.
    if (
        isinstance(data, dict)
        and data.get("workspace")
        and not data.get("folder")
        and not data.get("folders")
        and depth < 3
    ):
        target = _uri_or_path_to_fs_path(str(data.get("workspace", "")), base_dir=os.path.dirname(path))
        if target and os.path.isfile(target):
            return _resolve_workspace_descriptor(target, depth + 1)

    if not isinstance(data, dict):
        return data

    out = dict(data)
    base_dir = os.path.dirname(path)
    folders = out.get("folders")
    if isinstance(folders, list):
        normalized = []
        for folder in folders:
            if isinstance(folder, dict):
                fd = dict(folder)
                p = fd.get("path")
                if isinstance(p, str) and p:
                    if not p.startswith("file://") and not os.path.isabs(p):
                        fd["path"] = os.path.normpath(os.path.join(base_dir, p))
                normalized.append(fd)
            else:
                normalized.append(folder)
        out["folders"] = normalized
    return out


def basename_from_pathish(path_value: str | None) -> str | None:
    """Extract a readable leaf folder name from file URI or filesystem path."""
    if not path_value:
        return None
    cleaned = re.sub(r"^file://", "", str(path_value).strip())
    cleaned = unquote(cleaned).replace("\\", "/").rstrip("/")
    if not cleaned:
        return None
    parts = [p for p in cleaned.split("/") if p]
    if not parts:
        return None
    leaf = parts[-1]
    return leaf or None
