from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import sys
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any, cast

_logger = logging.getLogger(__name__)

from utils.path_helpers import (
    get_workspace_display_name,
    get_workspace_folder_paths,
    normalize_file_path,
    warn_workspace_json_read,
)
from utils.workspace_descriptor import basename_from_pathish, read_json_file
from services.workspace_db import load_project_layouts_map, open_global_db
from models import SchemaError, Workspace
from models.conversation import Bubble, Composer
from models.raw_access import (
    bubble_attached_file_uris,
    bubble_context,
    bubble_relevant_files,
    composer_code_block_data,
    composer_headers,
    composer_newly_created_files,
    conversation_header_bubble_id,
)


def lookup_workspace_display_name(workspace_path: str, workspace_id: str) -> str:
    """Resolve a display name for a workspace folder from storage.

    Args:
        workspace_path: Cursor workspace storage root.
        workspace_id: Workspace folder name, or ``"global"`` for unassigned chats.

    Returns:
        Human-readable name from ``workspace.json`` when parseable; ``"Other chats"``
        for ``global``; otherwise ``workspace_id``.
    """
    if workspace_id == "global":
        return "Other chats"
    wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
    try:
        workspace = Workspace.from_dict(read_json_file(wj_path), workspace_id=workspace_id)
        name = get_workspace_display_name(workspace.raw)
        if name:
            return name
    except (SchemaError, OSError, ValueError) as e:
        _logger.warning(
            "Failed to parse Workspace from %s: %s",
            workspace_id,
            e,
        )
    return workspace_id


def build_composer_ids_by_workspace(
    composer_id_to_workspace_id: dict[str, str],
) -> dict[str, list[str]]:
    """Invert ``composer_id → workspace_id`` for batch layout-based name inference."""
    out: dict[str, list[str]] = {}
    for composer_id, ws_id in composer_id_to_workspace_id.items():
        out.setdefault(ws_id, []).append(composer_id)
    return out


def infer_workspace_name_from_layouts(
    composer_ids: list[str],
    project_layouts_map: dict[str, list[str]],
) -> str | None:
    """Infer a display name from preloaded ``messageRequestContext`` layouts.

    Avoids per-workspace SQLite opens when the global layout map is already
    loaded for listing or tab-summary paths.
    """
    counts: dict[str, int] = {}
    for composer_id in composer_ids:
        for root_path in project_layouts_map.get(composer_id, []):
            hint = basename_from_pathish(root_path)
            if hint:
                counts[hint] = counts.get(hint, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def matching_workspace_ids_for_folder(
    workspace_id: str,
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
) -> set[str]:
    """Return workspace folder IDs that share the same on-disk project folder."""
    matching: set[str] = {workspace_id}
    if workspace_id == "global":
        return matching
    target_folder = ""
    wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
    try:
        wd = read_json_file(wj_path)
        folders = get_workspace_folder_paths(wd)
        first_folder = folders[0] if folders else None
        if first_folder:
            target_folder = normalize_file_path(first_folder)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        warn_workspace_json_read(_logger, workspace_id, e)
    if not target_folder:
        return matching
    for entry in workspace_entries:
        try:
            wd2 = read_json_file(entry["workspaceJsonPath"])
            folders2 = get_workspace_folder_paths(wd2)
            f2 = folders2[0] if folders2 else None
            if f2 and normalize_file_path(f2) == target_folder:
                matching.add(entry["name"])
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            warn_workspace_json_read(_logger, entry["name"], e)
    return matching


def _composer_ids_from_workspace_db(workspace_path: str, workspace_id: str) -> list[str]:
    """Read composer IDs registered in a workspace folder's ``state.vscdb``."""
    local_db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
    if not os.path.isfile(local_db_path):
        return []
    composer_ids: list[str] = []
    _db_uri = Path(local_db_path).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(_db_uri, uri=True)) as lconn:
            row = lconn.execute(
                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
            ).fetchone()
    except sqlite3.Error:
        return []
    if not (row and row[0]):
        return []
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    for c in (data.get("allComposers") or []):
        cid = c.get("composerId") if isinstance(c, dict) else None
        if cid:
            composer_ids.append(cid)
    return composer_ids


def infer_workspace_name_from_context(workspace_path: str, workspace_id: str) -> str | None:
    """Infer workspace name from ``projectLayouts`` when ``workspace.json`` is opaque.

    Args:
        workspace_path: Cursor workspace storage root.
        workspace_id: Workspace folder name (not ``"global"``).

    Returns:
        Most common folder basename from global ``messageRequestContext`` rows,
        or ``None`` when inference fails.
    """
    if workspace_id == "global":
        return "Other chats"

    composer_ids = _composer_ids_from_workspace_db(workspace_path, workspace_id)
    if not composer_ids:
        return None

    with open_global_db(workspace_path) as (gconn, _):
        if not gconn:
            return None
        layouts_map = load_project_layouts_map(gconn)
    return infer_workspace_name_from_layouts(composer_ids, layouts_map)


def get_project_from_file_path(
    file_path: str,
    workspace_entries: list[dict[str, Any]],
) -> str | None:
    """Map a file path to the workspace folder that contains it.

    Args:
        file_path: Absolute or URI-style file path.
        workspace_entries: Output of :func:`services.workspace_db.collect_workspace_entries`.

    Returns:
        Workspace folder name with the longest matching root path, or ``None``.
    """
    normalized_path = normalize_file_path(file_path)
    best_match = None
    best_len = 0
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                wp = normalize_file_path(folder)
                try:
                    is_within_workspace = os.path.commonpath([normalized_path, wp]) == wp
                except ValueError:
                    is_within_workspace = False
                if is_within_workspace and len(wp) > best_len:
                    best_len = len(wp)
                    best_match = entry["name"]
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)
    return best_match


def create_project_name_to_workspace_id_map(
    workspace_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Map workspace folder basenames to workspace folder names.

    Args:
        workspace_entries: Output of :func:`services.workspace_db.collect_workspace_entries`.

    Returns:
        Dict mapping last path segment (folder name) to workspace id.
    """
    mapping: dict[str, str] = {}
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                wp = re.sub(r"^file://", "", folder)
                parts = wp.replace("\\", "/").split("/")
                folder_name = parts[-1] if parts else None
                if folder_name:
                    mapping[folder_name] = entry["name"]
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)
    return mapping


def create_workspace_path_to_id_map(
    workspace_entries: list[dict[str, Any]],
) -> dict[str, str]:
    """Map normalized workspace root paths to workspace folder names.

    Args:
        workspace_entries: Output of :func:`services.workspace_db.collect_workspace_entries`.

    Returns:
        Dict mapping normalized folder paths to workspace ids.
    """
    out: dict[str, str] = {}
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                normalized = normalize_file_path(folder)
                out[normalized] = entry["name"]
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)
    return out


def determine_project_for_conversation(
    composer_data: Composer | dict[str, Any],
    composer_id: str,
    project_layouts_map: dict[str, list[str]],
    project_name_to_workspace_id: dict[str, str],
    workspace_path_to_id: dict[str, str],
    workspace_entries: list[dict[str, Any]],
    bubble_map: Mapping[str, Bubble],
    composer_id_to_workspace_id: dict[str, str] | None = None,
    invalid_workspace_ids: set[str] | None = None,
) -> str | None:
    """Resolve which workspace folder owns a composer conversation.

    Args:
        composer_data: Parsed ``composerData`` JSON for *composer_id*.
        composer_id: Composer UUID from the global DB key.
        project_layouts_map: ``{composer_id: [root_path, ...]}`` from global KV.
        project_name_to_workspace_id: Basename-to-workspace-folder map.
        workspace_path_to_id: Normalized root path to workspace folder map.
        workspace_entries: Output of :func:`services.workspace_db.collect_workspace_entries`.
        bubble_map: ``{bubble_id: Bubble}`` from global KV loaders.
        composer_id_to_workspace_id: Definitive per-workspace composer map; when
            ``None``, layout and path heuristics are used without this shortcut.
        invalid_workspace_ids: Workspace folders marked invalid; mapped IDs in
            this set are ignored when using *composer_id_to_workspace_id*.

    Returns:
        Workspace folder name, or ``None`` when no project can be determined.
    """
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
    newly = composer_newly_created_files(composer_data, composer_id)
    for file_entry in newly:
        uri = file_entry.get("uri") if isinstance(file_entry, dict) else None
        if isinstance(uri, dict) and uri.get("path"):
            pid = get_project_from_file_path(uri["path"], workspace_entries)
            if pid:
                return pid

    # Fallback: codeBlockData
    cbd = composer_code_block_data(composer_data, composer_id)
    if isinstance(cbd, dict):
        for fp in cbd.keys():
            pid = get_project_from_file_path(re.sub(r"^file://", "", fp), workspace_entries)
            if pid:
                return pid

    # Fallback: conversation headers -> bubble references (single pass)
    headers = composer_headers(composer_data, composer_id)
    path_segments: list[str] = []
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
        bubble_id = conversation_header_bubble_id(header, composer_id=composer_id)
        if not bubble_id:
            continue
        bubble = bubble_map.get(bubble_id)
        if not bubble:
            continue
        for fp in bubble_relevant_files(bubble, bubble_id):
            if fp:
                pid = get_project_from_file_path(fp, workspace_entries)
                if pid:
                    return pid
                path_segments.append(normalize_file_path(fp))
        for uri in bubble_attached_file_uris(bubble, bubble_id):
            if isinstance(uri, dict) and uri.get("path"):
                pid = get_project_from_file_path(uri["path"], workspace_entries)
                if pid:
                    return pid
                path_segments.append(normalize_file_path(uri["path"]))
        for fs_entry in (bubble_context(bubble, bubble_id).get("fileSelections") or []):
            if isinstance(fs_entry, dict):
                uri = fs_entry.get("uri")
                if isinstance(uri, dict) and uri.get("path"):
                    pid = get_project_from_file_path(uri["path"], workspace_entries)
                    if pid:
                        return pid
                    path_segments.append(normalize_file_path(uri["path"]))

    sep = "\\" if sys.platform == "win32" else "/"
    folder_name_to_ws = []
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            for folder in get_workspace_folder_paths(wd):
                name = re.sub(r"^file://", "", folder).replace("\\", "/").split("/")[-1]
                if name:
                    folder_name_to_ws.append({"name": name, "id": entry["name"]})
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)

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
        return cast(str, best_id)

    return None


def infer_invalid_workspace_aliases(
    composer_rows: list[sqlite3.Row],
    project_layouts_map: dict[str, list[str]],
    project_name_map: dict[str, str],
    workspace_path_map: dict[str, str],
    workspace_entries: list[dict[str, Any]],
    bubble_map: Mapping[str, Bubble],
    composer_id_to_ws: dict[str, str],
    invalid_workspace_ids: set[str],
) -> dict[str, str]:
    """Map invalid workspace IDs to valid replacements by majority vote.

    For each composer assigned to an *invalid_workspace_ids* entry, calls
    :func:`determine_project_for_conversation` without the definitive composer map
    and counts votes for inferred valid workspace folders.

    Args:
        composer_rows: Global ``composerData:*`` SQLite rows.
        project_layouts_map: Layout map passed to :func:`determine_project_for_conversation`.
        project_name_map: Basename map for path resolution.
        workspace_path_map: Normalized path map for path resolution.
        workspace_entries: Workspace folder entries from storage scan.
        bubble_map: ``{bubble_id: Bubble}`` for path resolution.
        composer_id_to_ws: Composer-to-workspace map (may point at invalid IDs).
        invalid_workspace_ids: Workspace folder names to reassign.

    Returns:
        ``{invalid_id: replacement_id}`` for IDs with at least one vote. Ties
        break by choosing the replacement with the highest vote count (first
        max in iteration order). Returns ``{}`` when no invalid ID receives votes.
    """
    votes: dict[str, dict[str, int]] = {}
    for row in composer_rows:
        cid = row["key"].split(":")[1]
        mapped = composer_id_to_ws.get(cid)
        if mapped not in invalid_workspace_ids:
            continue
        try:
            cd = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            _logger.warning(
                "Failed to decode Composer from composerData:%s: %s",
                cid,
                e,
            )
            continue
        if not isinstance(cd, dict):
            _logger.warning(
                "Failed to parse Composer from composerData:%s: expected object, got %s",
                cid,
                type(cd).__name__,
            )
            continue
        try:
            composer = Composer.from_dict(cd, composer_id=cid)
        except SchemaError as e:
            _logger.warning(
                "Failed to parse Composer from composerData:%s: %s",
                cid,
                e,
            )
            continue
        inferred = determine_project_for_conversation(
            composer,
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
