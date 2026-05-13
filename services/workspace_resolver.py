from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from utils.path_helpers import (
    get_workspace_display_name,
    get_workspace_folder_paths,
    normalize_file_path,
)
from utils.workspace_descriptor import _basename_from_pathish, _read_json_file
from services.workspace_db import _open_global_db


def _get_workspace_display_name(workspace_path: str, workspace_id: str) -> str:
    """Return human-readable workspace name; "Other chats" for global, workspace_id if unreadable."""
    if workspace_id == "global":
        return "Other chats"
    wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
    try:
        wd = _read_json_file(wj_path)
        name = get_workspace_display_name(wd)
        if name:
            return name
    except Exception:
        pass
    return workspace_id


def _infer_workspace_name_from_context(workspace_path: str, workspace_id: str) -> str | None:
    """Infer workspace name from projectLayouts when workspace.json is opaque."""
    if workspace_id == "global":
        return "Other chats"

    # Composer IDs from per-workspace state db
    local_db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
    if not os.path.isfile(local_db_path):
        return None
    composer_ids: list[str] = []
    # closing() guarantees .close() on scope exit (issue #17).
    # Path.as_uri() percent-encodes reserved chars (#, ?, spaces, etc.);
    # naive f"file:{path}" breaks sqlite URI parsing.
    _db_uri = Path(local_db_path).resolve().as_uri() + "?mode=ro"
    row: tuple | None = None
    try:
        with closing(sqlite3.connect(_db_uri, uri=True)) as lconn:
            row = lconn.execute(
                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
            ).fetchone()
    except sqlite3.Error:
        return None
    if row and row[0]:
        try:
            data = json.loads(row[0])
        except (json.JSONDecodeError, ValueError):
            return None
        for c in (data.get("allComposers") or []):
            cid = c.get("composerId") if isinstance(c, dict) else None
            if cid:
                composer_ids.append(cid)
    if not composer_ids:
        return None

    # Gather folder-name hints from global messageRequestContext.projectLayouts
    counts: dict[str, int] = {}
    with _open_global_db(workspace_path) as (gconn, _):
        if not gconn:
            return None
        for cid in composer_ids:
            try:
                rows = gconn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
                    (f"messageRequestContext:{cid}:%",),
                ).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                try:
                    ctx = json.loads(row["value"])
                except Exception:
                    continue
                layouts = ctx.get("projectLayouts")
                if not isinstance(layouts, list):
                    continue
                for layout in layouts:
                    obj = None
                    if isinstance(layout, str):
                        try:
                            obj = json.loads(layout)
                        except Exception:
                            obj = None
                    elif isinstance(layout, dict):
                        obj = layout
                    if not isinstance(obj, dict):
                        continue
                    hint = _basename_from_pathish(obj.get("rootPath"))
                    if hint:
                        counts[hint] = counts.get(hint, 0) + 1

    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _get_project_from_file_path(
    file_path: str,
    workspace_entries: list[dict],
) -> str | None:
    normalized_path = normalize_file_path(file_path)
    best_match = None
    best_len = 0
    for entry in workspace_entries:
        try:
            wd = _read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                wp = normalize_file_path(folder)
                try:
                    is_within_workspace = os.path.commonpath([normalized_path, wp]) == wp
                except ValueError:
                    is_within_workspace = False
                if is_within_workspace and len(wp) > best_len:
                    best_len = len(wp)
                    best_match = entry["name"]
        except Exception:
            pass
    return best_match


def _create_project_name_to_workspace_id_map(workspace_entries):
    mapping = {}
    for entry in workspace_entries:
        try:
            wd = _read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                wp = re.sub(r"^file://", "", folder)
                parts = wp.replace("\\", "/").split("/")
                folder_name = parts[-1] if parts else None
                if folder_name:
                    mapping[folder_name] = entry["name"]
        except Exception:
            pass
    return mapping


def _create_workspace_path_to_id_map(workspace_entries):
    out = {}
    for entry in workspace_entries:
        try:
            wd = _read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                normalized = normalize_file_path(folder)
                out[normalized] = entry["name"]
        except Exception:
            pass
    return out


def _determine_project_for_conversation(
    composer_data: dict,
    composer_id: str,
    project_layouts_map: dict,
    project_name_to_workspace_id: dict,
    workspace_path_to_id: dict,
    workspace_entries: list,
    bubble_map: dict,
    composer_id_to_workspace_id: dict | None = None,
    invalid_workspace_ids: set[str] | None = None,
) -> str | None:
    # Primary: definitive per-workspace mapping
    if composer_id_to_workspace_id and composer_id in composer_id_to_workspace_id:
        mapped = composer_id_to_workspace_id[composer_id]
        if not invalid_workspace_ids or mapped not in invalid_workspace_ids:
            return mapped

    # Try projectLayouts
    project_layouts = project_layouts_map.get(composer_id, [])
    for root_path in project_layouts:
        normalized = normalize_file_path(root_path)
        workspace_id = workspace_path_to_id.get(normalized)
        if not workspace_id:
            parts = root_path.replace("\\", "/").split("/")
            folder_name = parts[-1] if parts else ""
            workspace_id = project_name_to_workspace_id.get(folder_name, "")
        if workspace_id:
            return workspace_id

    # Fallback: newlyCreatedFiles
    newly = composer_data.get("newlyCreatedFiles") or []
    for file_entry in newly:
        uri = file_entry.get("uri") if isinstance(file_entry, dict) else None
        if isinstance(uri, dict) and uri.get("path"):
            pid = _get_project_from_file_path(uri["path"], workspace_entries)
            if pid:
                return pid

    # Fallback: codeBlockData
    cbd = composer_data.get("codeBlockData")
    if isinstance(cbd, dict):
        for fp in cbd.keys():
            pid = _get_project_from_file_path(re.sub(r"^file://", "", fp), workspace_entries)
            if pid:
                return pid

    # Fallback: conversation headers -> bubble references
    headers = composer_data.get("fullConversationHeadersOnly") or []
    for header in headers:
        if not isinstance(header, dict):
            continue
        bubble = bubble_map.get(header.get("bubbleId"))
        if not bubble:
            continue
        for fp in (bubble.get("relevantFiles") or []):
            if fp:
                pid = _get_project_from_file_path(fp, workspace_entries)
                if pid:
                    return pid
        for uri in (bubble.get("attachedFileCodeChunksUris") or []):
            if isinstance(uri, dict) and uri.get("path"):
                pid = _get_project_from_file_path(uri["path"], workspace_entries)
                if pid:
                    return pid
        for fs_entry in (bubble.get("context", {}).get("fileSelections") or []):
            if isinstance(fs_entry, dict):
                uri = fs_entry.get("uri")
                if isinstance(uri, dict) and uri.get("path"):
                    pid = _get_project_from_file_path(uri["path"], workspace_entries)
                    if pid:
                        return pid

    # Last fallback: path-segment matching
    path_segments = []
    for f in newly:
        if isinstance(f, dict):
            uri = f.get("uri")
            if isinstance(uri, dict) and uri.get("path"):
                path_segments.append(normalize_file_path(uri["path"]))
    if isinstance(cbd, dict):
        for fp in cbd.keys():
            path_segments.append(normalize_file_path(re.sub(r"^file://", "", fp)))
    for header in headers:
        if not isinstance(header, dict):
            continue
        bubble = bubble_map.get(header.get("bubbleId"))
        if not bubble:
            continue
        for fp in (bubble.get("relevantFiles") or []):
            if fp:
                path_segments.append(normalize_file_path(fp))
        for uri in (bubble.get("attachedFileCodeChunksUris") or []):
            if isinstance(uri, dict) and uri.get("path"):
                path_segments.append(normalize_file_path(uri["path"]))
        for fs_entry in (bubble.get("context", {}).get("fileSelections") or []):
            if isinstance(fs_entry, dict):
                uri = fs_entry.get("uri")
                if isinstance(uri, dict) and uri.get("path"):
                    path_segments.append(normalize_file_path(uri["path"]))

    sep = "\\" if sys.platform == "win32" else "/"
    folder_name_to_ws = []
    for entry in workspace_entries:
        try:
            wd = _read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                name = re.sub(r"^file://", "", folder).replace("\\", "/").split("/")[-1]
                if name:
                    folder_name_to_ws.append({"name": name, "id": entry["name"]})
        except Exception:
            pass

    best_id = None
    best_len = 0
    for p in path_segments:
        for item in folder_name_to_ws:
            needle = sep + item["name"] + sep
            needle_end = sep + item["name"]
            if needle in p or p.endswith(needle_end):
                if len(item["name"]) > best_len:
                    best_len = len(item["name"])
                    best_id = item["id"]
    if best_id:
        return best_id

    return None


def _infer_invalid_workspace_aliases(
    composer_rows: list,
    project_layouts_map: dict,
    project_name_map: dict,
    workspace_path_map: dict,
    workspace_entries: list,
    bubble_map: dict,
    composer_id_to_ws: dict,
    invalid_workspace_ids: set[str],
) -> dict[str, str]:
    """Majority-vote each invalid workspace ID to its most likely valid replacement."""
    votes: dict[str, dict[str, int]] = {}
    for row in composer_rows:
        cid = row["key"].split(":")[1]
        mapped = composer_id_to_ws.get(cid)
        if mapped not in invalid_workspace_ids:
            continue
        try:
            cd = json.loads(row["value"])
        except Exception:
            continue
        inferred = _determine_project_for_conversation(
            cd,
            cid,
            project_layouts_map,
            project_name_map,
            workspace_path_map,
            workspace_entries,
            bubble_map,
            composer_id_to_workspace_id=None,
            invalid_workspace_ids=None,
        )
        if inferred and inferred not in invalid_workspace_ids:
            votes.setdefault(mapped, {})
            votes[mapped][inferred] = votes[mapped].get(inferred, 0) + 1

    aliases: dict[str, str] = {}
    for invalid_id, counts in votes.items():
        if not counts:
            continue
        aliases[invalid_id] = max(counts.items(), key=lambda kv: kv[1])[0]
    return aliases
