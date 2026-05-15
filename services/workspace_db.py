from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from utils.path_helpers import get_workspace_folder_paths
from utils.workspace_descriptor import _read_json_file


def _collect_workspace_entries(workspace_path: str) -> list[dict]:
    """Scan workspace directory and return entries with workspace.json."""
    entries = []
    try:
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if os.path.isdir(full):
                wj = os.path.join(full, "workspace.json")
                if os.path.isfile(wj):
                    entries.append({"name": name, "workspaceJsonPath": wj})
    except OSError:
        # workspace_path missing / not readable / not a directory — return what
        # we have so far. OSError covers FileNotFoundError, PermissionError,
        # and NotADirectoryError.
        pass
    return entries


def _collect_invalid_workspace_ids(workspace_entries: list[dict]) -> set[str]:
    """Workspace IDs whose descriptors have no resolvable folder paths."""
    invalid: set[str] = set()
    for entry in workspace_entries:
        try:
            wd = _read_json_file(entry["workspaceJsonPath"])
            folders = get_workspace_folder_paths(wd)
            if not folders:
                invalid.add(entry["name"])
        except (OSError, ValueError, KeyError, TypeError):
            # OSError: workspace.json unreadable. ValueError covers
            # json.JSONDecodeError. KeyError / TypeError: malformed entry
            # dict. Any of these mean we can't resolve folders → mark invalid,
            # matching the pre-narrowing behaviour.
            invalid.add(entry["name"])
    return invalid


def _build_composer_id_to_workspace_id(workspace_path: str, workspace_entries: list) -> dict:
    """Build mapping: composerId -> workspaceId from per-workspace state.vscdb."""
    mapping: dict = {}
    for entry in workspace_entries:
        db_path = os.path.join(workspace_path, entry["name"], "state.vscdb")
        if not os.path.isfile(db_path):
            continue
        # closing() guarantees .close() on scope exit (issue #17).
        # Path.as_uri() percent-encodes reserved chars; ``f"file:{path}"``
        # breaks sqlite URI parsing on paths with spaces, ``#``, etc.
        db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        row: tuple | None = None
        try:
            with closing(sqlite3.connect(db_uri, uri=True)) as conn:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                ).fetchone()
        except sqlite3.Error:
            continue
        if not (row and row[0]):
            continue
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, ValueError):
            continue
        all_composers = data.get("allComposers") if isinstance(data, dict) else None
        if not isinstance(all_composers, list):
            continue
        for c in all_composers:
            if not isinstance(c, dict):
                continue
            cid = c.get("composerId")
            if cid:
                mapping[cid] = entry["name"]
    return mapping


@contextmanager
def _open_global_db(workspace_path: str):
    """Yield (conn, path) for the global-storage SQLite db (read-only); (None, path) if the file is missing."""
    global_db_path = os.path.join(workspace_path, "..", "globalStorage", "state.vscdb")
    global_db_path = os.path.normpath(global_db_path)
    if not os.path.isfile(global_db_path):
        yield None, global_db_path
        return
    db_uri = Path(global_db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.Error:
        yield None, global_db_path
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn, global_db_path
    finally:
        conn.close()
