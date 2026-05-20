from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from utils.path_helpers import get_workspace_folder_paths
from utils.workspace_descriptor import _read_json_file


# ── Global-DB KV loaders ────────────────────────────────────────────────────
# Each function accepts an already-opened sqlite3.Connection (row_factory must
# be set to sqlite3.Row by the caller, as _open_global_db does) and returns
# a populated dict.  sqlite3.Error is caught internally so a missing or
# corrupt table cannot propagate to callers.


def _load_bubble_map(global_db) -> dict[str, dict]:
    """Load all ``bubbleId:*`` KV entries into ``{bubble_id: bubble_dict}``.

    Skips rows whose JSON value is not a dict; JSON parse errors are silently
    discarded so a single malformed row cannot block the rest.
    """
    bubble_map: dict[str, dict] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchall()
    except sqlite3.Error:
        return bubble_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        bid = parts[2]
        try:
            b = json.loads(row["value"])
            if isinstance(b, dict):
                bubble_map[bid] = b
        except Exception:
            pass
    return bubble_map


def _load_project_layouts_map(global_db) -> dict[str, list]:
    """Load ``projectLayouts`` from ``messageRequestContext:*`` KV entries.

    Returns ``{composer_id: [root_path_str, ...]}``.  String-encoded layout
    objects are JSON-decoded before the ``rootPath`` field is extracted.
    """
    layouts_map: dict[str, list] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"
        ).fetchall()
    except sqlite3.Error:
        return layouts_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 2:
            continue
        cid = parts[1]
        try:
            ctx = json.loads(row["value"])
            layouts = ctx.get("projectLayouts")
            if not isinstance(layouts, list):
                continue
            layouts_map.setdefault(cid, [])
            for layout in layouts:
                try:
                    o = json.loads(layout) if isinstance(layout, str) else layout
                    if isinstance(o, dict) and o.get("rootPath"):
                        layouts_map[cid].append(o["rootPath"])
                except Exception:
                    pass
        except Exception:
            pass
    return layouts_map


def _load_code_block_diff_map(global_db) -> dict[str, list]:
    """Load ``codeBlockDiff:*`` KV entries into ``{composer_id: [diff_dict]}``.

    Each diff dict contains all fields from the raw JSON value plus a
    ``diffId`` key taken from the third path component of the KV key.
    """
    diff_map: dict[str, list] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"
        ).fetchall()
    except sqlite3.Error:
        return diff_map
    for row in rows:
        parts = row["key"].split(":")
        cid = parts[1] if len(parts) > 1 else None
        if not cid:
            continue
        try:
            d = json.loads(row["value"])
            if isinstance(d, dict):
                diff_map.setdefault(cid, []).append({
                    **d,
                    "diffId": parts[2] if len(parts) > 2 else None,
                })
        except Exception:
            pass
    return diff_map


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
