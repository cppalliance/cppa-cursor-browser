"""
API routes for workspaces — mirrors:
  src/app/api/workspaces/route.ts            GET /api/workspaces
  src/app/api/workspaces/[id]/route.ts       GET /api/workspaces/<id>
  src/app/api/workspaces/[id]/tabs/route.ts  GET /api/workspaces/<id>/tabs
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from pathlib import Path
import sys
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from flask import Blueprint, current_app, jsonify

from utils.workspace_path import resolve_workspace_path, get_cli_chats_path
from utils.cli_chat_reader import (
    list_cli_projects,
    traverse_blobs,
    messages_to_bubbles,
)
from utils.path_helpers import (
    normalize_file_path,
    get_workspace_folder_paths,
    get_workspace_display_name,
    to_epoch_ms,
)
from utils.text_extract import extract_text_from_bubble, format_tool_action
from utils.tool_parser import parse_tool_call as _parse_tool_call
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules

bp = Blueprint("workspaces", __name__)
_logger = logging.getLogger(__name__)


def _get_workspace_display_name(workspace_path: str, workspace_id: str) -> str:
    """
    Return a human-readable display name for a workspace.

    Reads the workspace's ``workspace.json`` to extract the last path segment
    of the first configured folder, URL-decodes it, and returns it.  Falls back
    to ``"Other chats"`` for the virtual ``"global"`` workspace and to
    *workspace_id* if the JSON cannot be read.
    """
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


# ---------------------------------------------------------------------------
# Shared helpers (duplicated in tabs route in the Node.js project)
# ---------------------------------------------------------------------------

def _read_json_file(path: str):
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


def _resolve_workspace_descriptor(path: str, depth: int = 0):
    """
    Read and normalize a workspace descriptor.

    Handles indirection via {"workspace": "<uri|path>"} and resolves relative
    folder paths in multi-root workspace files against the file's directory.
    """
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


def _basename_from_pathish(path_value: str | None) -> str | None:
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


def _infer_workspace_name_from_context(workspace_path: str, workspace_id: str) -> str | None:
    """
    Infer workspace display name from projectLayouts of chats in this workspace.

    Useful when workspace.json only references a deleted/opaque workspace file.
    """
    if workspace_id == "global":
        return "Other chats"

    # Composer IDs from per-workspace state db
    local_db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
    if not os.path.isfile(local_db_path):
        return None
    composer_ids: list[str] = []
    try:
        # closing() guarantees .close() on scope exit (issue #17).
        # Path.as_uri() percent-encodes reserved chars (#, ?, spaces, etc.);
        # naive f"file:{path}" breaks sqlite URI parsing.
        _db_uri = Path(local_db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(_db_uri, uri=True)) as lconn:
            row = lconn.execute(
                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
            ).fetchone()
        if row and row[0]:
            data = json.loads(row[0])
            for c in (data.get("allComposers") or []):
                cid = c.get("composerId") if isinstance(c, dict) else None
                if cid:
                    composer_ids.append(cid)
    except Exception:
        return None
    if not composer_ids:
        return None

    # Gather folder-name hints from global messageRequestContext.projectLayouts
    counts: dict[str, int] = {}
    with _open_global_db(workspace_path) as (gconn, _):
        if not gconn:
            return None
        for cid in composer_ids:
            rows = gconn.execute(
                "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
                (f"messageRequestContext:{cid}:%",),
            ).fetchall()
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
                if normalized_path.startswith(wp) and len(wp) > best_len:
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
    except Exception:
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
        except Exception:
            invalid.add(entry["name"])
    return invalid


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
    """
    Infer replacement workspace IDs for invalid workspace entries.

    For each composer mapped to an invalid workspace ID, compute an evidence-
    based assignment (without trusting composer_id_to_ws). Use majority voting
    to map each invalid workspace ID to the most likely valid workspace ID.
    """
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


def _build_composer_id_to_workspace_id(workspace_path: str, workspace_entries: list) -> dict:
    """Build mapping: composerId -> workspaceId from per-workspace state.vscdb."""
    mapping = {}
    for entry in workspace_entries:
        db_path = os.path.join(workspace_path, entry["name"], "state.vscdb")
        if not os.path.isfile(db_path):
            continue
        try:
            # closing() guarantees .close() on scope exit (issue #17).
            with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                row = conn.execute(
                    "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                ).fetchone()
            if row and row[0]:
                data = json.loads(row[0])
                all_composers = data.get("allComposers")
                if isinstance(all_composers, list):
                    for c in all_composers:
                        cid = c.get("composerId")
                        if cid:
                            mapping[cid] = entry["name"]
        except Exception:
            pass
    return mapping


@contextmanager
def _open_global_db(workspace_path: str):
    """Yield (conn, path) for the global-storage SQLite db (read-only).

    Context-managed so the caller writes ``with _open_global_db(...) as (conn, _):``
    and the connection is guaranteed to close on scope exit, including on
    exception (issue #17). Yields ``(None, path)`` if the file is missing —
    callers branch on ``conn is None`` exactly as before.
    """
    global_db_path = os.path.join(workspace_path, "..", "globalStorage", "state.vscdb")
    global_db_path = os.path.normpath(global_db_path)
    if not os.path.isfile(global_db_path):
        yield None, global_db_path
        return
    conn = sqlite3.connect(f"file:{global_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn, global_db_path
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces")
def list_workspaces():
    try:
        workspace_path = resolve_workspace_path()
        workspace_entries = _collect_workspace_entries(workspace_path)
        invalid_workspace_ids = _collect_invalid_workspace_ids(workspace_entries)

        project_name_map = _create_project_name_to_workspace_id_map(workspace_entries)
        workspace_path_map = _create_workspace_path_to_id_map(workspace_entries)
        composer_id_to_ws = _build_composer_id_to_workspace_id(workspace_path, workspace_entries)

        conversation_map: dict[str, list] = {}

        # closing semantics now baked into the context manager (issue #17).
        with _open_global_db(workspace_path) as (global_db, _):
            if global_db:
                try:
                    # composerData rows
                    composer_rows = global_db.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND LENGTH(value) > 10"
                    ).fetchall()

                    # messageRequestContext rows -> project layouts
                    ctx_rows = global_db.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"
                    ).fetchall()
                    project_layouts_map: dict[str, list] = {}
                    for row in ctx_rows:
                        parts = row["key"].split(":")
                        if len(parts) < 2:
                            continue
                        cid = parts[1]
                        try:
                            ctx = json.loads(row["value"])
                            layouts = ctx.get("projectLayouts")
                            if isinstance(layouts, list):
                                if cid not in project_layouts_map:
                                    project_layouts_map[cid] = []
                                for layout in layouts:
                                    if isinstance(layout, str):
                                        try:
                                            obj = json.loads(layout)
                                            if isinstance(obj, dict) and obj.get("rootPath"):
                                                project_layouts_map[cid].append(obj["rootPath"])
                                        except Exception:
                                            pass
                        except Exception:
                            pass

                    # bubbleId rows for project detection
                    bubble_rows = global_db.execute(
                        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
                    ).fetchall()
                    bubble_map: dict[str, dict] = {}
                    for row in bubble_rows:
                        parts = row["key"].split(":")
                        if len(parts) >= 3:
                            bid = parts[2]
                            try:
                                b = json.loads(row["value"])
                                if isinstance(b, dict):
                                    bubble_map[bid] = b
                            except Exception:
                                pass

                    # Process each composer
                    invalid_workspace_aliases = _infer_invalid_workspace_aliases(
                        composer_rows=composer_rows,
                        project_layouts_map=project_layouts_map,
                        project_name_map=project_name_map,
                        workspace_path_map=workspace_path_map,
                        workspace_entries=workspace_entries,
                        bubble_map=bubble_map,
                        composer_id_to_ws=composer_id_to_ws,
                        invalid_workspace_ids=invalid_workspace_ids,
                    )
                    for row in composer_rows:
                        cid = row["key"].split(":")[1]
                        try:
                            cd = json.loads(row["value"])
                            pid = _determine_project_for_conversation(
                                cd, cid, project_layouts_map,
                                project_name_map, workspace_path_map,
                                workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids
                            )
                            mapped_ws = composer_id_to_ws.get(cid)
                            if not pid and mapped_ws in invalid_workspace_ids:
                                pid = invalid_workspace_aliases.get(mapped_ws)
                            assigned = pid if pid else "global"

                            headers = cd.get("fullConversationHeadersOnly") or []
                            has_bubbles = any(bubble_map.get(h.get("bubbleId")) for h in headers)
                            if not has_bubbles:
                                continue

                            conversation_map.setdefault(assigned, []).append({
                                "composerId": cid,
                                "name": cd.get("name") or f"Conversation {cid[:8]}",
                                "lastUpdatedAt": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0,
                                "createdAt": to_epoch_ms(cd.get("createdAt")) or 0,
                            })
                        except Exception:
                            pass
                except Exception:
                    pass

        # Exclusion rules (optional)
        rules = current_app.config.get("EXCLUSION_RULES") or []

        # Build project list — merge workspace entries sharing the same folder

        # Group workspace entries by normalized folder path
        folder_to_entries: dict[str, list] = {}
        entry_folder_map: dict[str, str] = {}  # entry_name -> normalized folder
        for entry in workspace_entries:
            norm_folder = ""
            try:
                wd = _read_json_file(entry["workspaceJsonPath"])
                folders = get_workspace_folder_paths(wd)
                first_folder = folders[0] if folders else None
                if first_folder:
                    norm_folder = normalize_file_path(first_folder)
            except Exception:
                pass
            if not norm_folder:
                norm_folder = entry["name"]  # fallback to workspace ID
            entry_folder_map[entry["name"]] = norm_folder
            folder_to_entries.setdefault(norm_folder, []).append(entry)

        projects = []
        seen_folders = set()
        for entry in workspace_entries:
            norm_folder = entry_folder_map[entry["name"]]
            if norm_folder in seen_folders:
                continue
            seen_folders.add(norm_folder)

            group = folder_to_entries[norm_folder]
            # Primary entry is the first one; use its ID as the canonical one
            primary = group[0]
            all_ws_ids = [e["name"] for e in group]

            try:
                mtime = max(
                    os.path.getmtime(os.path.join(workspace_path, e["name"], "state.vscdb"))
                    for e in group
                    if os.path.isfile(os.path.join(workspace_path, e["name"], "state.vscdb"))
                )
            except Exception:
                mtime = 0

            workspace_name = _get_workspace_display_name(workspace_path, primary["name"])
            if workspace_name == primary["name"]:
                inferred = _infer_workspace_name_from_context(workspace_path, primary["name"])
                workspace_name = inferred or f"Project {primary['name'][:8]}"

            # Skip entire workspace before iterating conversations
            if is_excluded_by_rules(rules, workspace_name):
                continue

            # Merge conversations from all workspace IDs in the group; apply exclusion rules
            convos = []
            for ws_id in all_ws_ids:
                for c in conversation_map.get(ws_id, []):
                    searchable = build_searchable_text(
                        project_name=workspace_name,
                        chat_title=c.get("name"),
                    )
                    if not is_excluded_by_rules(rules, searchable):
                        convos.append(c)

            # Hide workspace shells that currently have no visible conversations.
            if not convos:
                continue

            projects.append({
                "id": primary["name"],
                "name": workspace_name,
                "path": primary["workspaceJsonPath"],
                "conversationCount": len(convos),
                "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
                # Include all workspace IDs for this folder
                **({"aliasIds": all_ws_ids} if len(all_ws_ids) > 1 else {}),
            })

        # Global (unmatched) conversations; apply exclusion rules
        global_convos = [
            c for c in conversation_map.get("global", [])
            if not is_excluded_by_rules(
                rules,
                build_searchable_text(project_name="Other chats", chat_title=c.get("name")),
            )
        ]
        if global_convos:
            last_updated = max((c.get("lastUpdatedAt") or 0 for c in global_convos), default=0)
            projects.append({
                "id": "global",
                "name": "Other chats",
                "conversationCount": len(global_convos),
                "lastModified": (
                    datetime.fromtimestamp(last_updated / 1000, tz=timezone.utc).isoformat()
                    if last_updated > 0
                    else datetime.now(tz=timezone.utc).isoformat()
                ),
            })

        # --- Cursor CLI projects ---
        try:
            cli_projects = list_cli_projects(get_cli_chats_path())
            for cp in cli_projects:
                ws_name = cp["workspace_name"] or cp["project_id"][:12]
                if is_excluded_by_rules(rules, ws_name):
                    continue
                convos = []
                for s in cp["sessions"]:
                    session_name = s["meta"].get("name") or f"Session {s['session_id'][:8]}"
                    searchable = build_searchable_text(
                        project_name=ws_name,
                        chat_title=session_name,
                    )
                    if not is_excluded_by_rules(rules, searchable):
                        convos.append(session_name)
                if not convos:
                    continue
                last_ms = cp["last_updated_ms"]
                projects.append({
                    "id": f"cli:{cp['project_id']}",
                    "name": ws_name,
                    "conversationCount": len(convos),
                    "lastModified": (
                        datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                        if last_ms
                        else datetime.now(tz=timezone.utc).isoformat()
                    ),
                    "source": "cli",
                })
        except Exception:
            _logger.exception("Failed to load CLI projects")

        projects.sort(key=lambda p: p["lastModified"], reverse=True)
        return jsonify(projects)

    except Exception:
        _logger.exception("Failed to get workspaces")
        return jsonify({"error": "Failed to get workspaces"}), 500


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>
# ---------------------------------------------------------------------------

@bp.route("/api/workspaces/<workspace_id>")
def get_workspace(workspace_id):
    try:
        if workspace_id == "global":
            return jsonify({
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
                if cp["project_id"] == project_id:
                    last_ms = cp["last_updated_ms"]
                    return jsonify({
                        "id": workspace_id,
                        "name": cp["workspace_name"] or project_id[:12],
                        "path": cp["workspace_path"],
                        "folder": cp["workspace_path"],
                        "lastModified": (
                            datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                            if last_ms
                            else datetime.now(tz=timezone.utc).isoformat()
                        ),
                        "source": "cli",
                    })
            return jsonify({"error": "CLI project not found"}), 404

        workspace_path = resolve_workspace_path()
        db_path = os.path.join(workspace_path, workspace_id, "state.vscdb")
        wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")

        if not os.path.isfile(db_path):
            return jsonify({"error": "Workspace not found"}), 404

        mtime = os.path.getmtime(db_path)
        folder = None
        workspace_name = workspace_id
        try:
            wd = _read_json_file(wj_path)
            folder_paths = get_workspace_folder_paths(wd)
            folder = folder_paths[0] if folder_paths else wd.get("folder")
            derived_name = get_workspace_display_name(wd)
            if derived_name:
                workspace_name = derived_name
            elif workspace_name == workspace_id:
                inferred = _infer_workspace_name_from_context(workspace_path, workspace_id)
                if inferred:
                    workspace_name = inferred
        except Exception:
            inferred = _infer_workspace_name_from_context(workspace_path, workspace_id)
            if inferred:
                workspace_name = inferred

        return jsonify({
            "id": workspace_id,
            "name": workspace_name,
            "path": db_path,
            "folder": folder,
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        })

    except Exception:
        _logger.exception("Failed to get workspace")
        return jsonify({"error": "Failed to get workspace"}), 500


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs
# ---------------------------------------------------------------------------



def _get_cli_workspace_tabs(workspace_id: str):
    """Return tabs for a Cursor CLI project (``workspace_id`` starts with ``cli:``)."""
    try:
        project_id = workspace_id[4:]
        cli_projects = list_cli_projects(get_cli_chats_path())
        project = next((cp for cp in cli_projects if cp["project_id"] == project_id), None)
        if project is None:
            return jsonify({"error": "CLI project not found"}), 404

        rules = current_app.config.get("EXCLUSION_RULES") or []
        ws_name = project["workspace_name"] or project_id[:12]
        tabs = []

        for session in project["sessions"]:
            meta = session.get("meta", {})
            session_id = session["session_id"]
            created_ms: int = meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
            session_name = meta.get("name") or f"Session {session_id[:8]}"

            try:
                messages = traverse_blobs(session["db_path"])
            except Exception:  # noqa: BLE001 — best-effort per-session skip; one corrupted session must not 500 the endpoint, and the failure mode is logged with exc_info so the concrete type is preserved.
                _logger.warning("CLI: could not read session %s", session_id, exc_info=True)
                continue

            bubbles = messages_to_bubbles(messages, created_ms)
            if not bubbles:
                continue

            # Derive title from first user bubble when name is generic
            title = session_name
            if not title or title.startswith("New Agent"):
                for b in bubbles:
                    if b["type"] == "user" and b.get("text"):
                        first_lines = [ln for ln in b["text"].split("\n") if ln.strip()]
                        if first_lines:
                            title = first_lines[0][:100]
                            if len(title) == 100:
                                title += "..."
                        break

            searchable = build_searchable_text(project_name=ws_name, chat_title=title)
            if is_excluded_by_rules(rules, searchable):
                continue

            # Aggregate metadata
            total_tool_calls = 0
            tool_breakdown: dict = {}
            for b in bubbles:
                tcs = (b.get("metadata") or {}).get("toolCalls") or []
                total_tool_calls += len(tcs)
                for tc in tcs:
                    tn = tc.get("name", "unknown")
                    tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

            tab_meta: dict | None = None
            if total_tool_calls or tool_breakdown:
                tab_meta = {"totalToolCalls": total_tool_calls or None}
                if tool_breakdown:
                    tab_meta["toolBreakdown"] = tool_breakdown

            tab = {
                "id": session_id,
                "title": title,
                "timestamp": created_ms,
                "bubbles": [
                    {
                        "type": b["type"],
                        "text": b.get("text", ""),
                        "timestamp": b.get("timestamp", created_ms),
                        **({"metadata": b["metadata"]} if b.get("metadata") else {}),
                    }
                    for b in bubbles
                ],
                "source": "cli",
            }
            if tab_meta:
                tab_meta_clean = {k: v for k, v in tab_meta.items() if v is not None}
                if tab_meta_clean:
                    tab["metadata"] = tab_meta_clean

            tabs.append(tab)

        tabs.sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
        return jsonify({"tabs": tabs})

    except Exception:
        _logger.exception("Failed to get CLI workspace tabs")
        return jsonify({"error": "Failed to get CLI workspace tabs"}), 500


def _extract_chat_id_from_bubble_key(key: str) -> str | None:
    m = re.match(r"^bubbleId:([^:]+):", key)
    return m.group(1) if m else None


def _extract_chat_id_from_code_block_diff_key(key: str) -> str | None:
    m = re.match(r"^codeBlockDiff:([^:]+):", key)
    return m.group(1) if m else None


@bp.route("/api/workspaces/<workspace_id>/tabs")
def get_workspace_tabs(workspace_id):
    if workspace_id.startswith("cli:"):
        return _get_cli_workspace_tabs(workspace_id)

    # Global DB reads use `_open_global_db` (issue #17) — same lifecycle as
    # list_workspaces; connection closes when the `with` block exits.
    try:
        workspace_path = resolve_workspace_path()

        response = {"tabs": []}

        workspace_entries = _collect_workspace_entries(workspace_path)
        invalid_workspace_ids = _collect_invalid_workspace_ids(workspace_entries)
        project_name_map = _create_project_name_to_workspace_id_map(workspace_entries)
        workspace_path_map = _create_workspace_path_to_id_map(workspace_entries)
        composer_id_to_ws = _build_composer_id_to_workspace_id(workspace_path, workspace_entries)

        # Build set of all workspace IDs that share the same folder as workspace_id
        # (handles Cursor creating multiple workspace entries for the same project)
        matching_ws_ids = {workspace_id}
        if workspace_id != "global":
            target_folder = ""
            wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
            try:
                wd = _read_json_file(wj_path)
                folders = get_workspace_folder_paths(wd)
                first_folder = folders[0] if folders else None
                if first_folder:
                    target_folder = normalize_file_path(first_folder)
            except Exception:
                pass
            if target_folder:
                for entry in workspace_entries:
                    try:
                        wd2 = _read_json_file(entry["workspaceJsonPath"])
                        folders2 = get_workspace_folder_paths(wd2)
                        f2 = folders2[0] if folders2 else None
                        if f2 and normalize_file_path(f2) == target_folder:
                            matching_ws_ids.add(entry["name"])
                    except Exception:
                        pass

        bubble_map: dict[str, dict] = {}
        code_block_diff_map: dict[str, list] = {}
        message_request_context_map: dict[str, list] = {}

        with _open_global_db(workspace_path) as (global_db, _):
            if global_db is None:
                return jsonify({"error": "Global storage not found"}), 404

            workspace_display_name = _get_workspace_display_name(workspace_path, workspace_id)
            rules = current_app.config.get("EXCLUSION_RULES") or []

            # Load bubbles
            for row in global_db.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
                parts = row["key"].split(":")
                if len(parts) >= 3:
                    bid = parts[2]
                    try:
                        b = json.loads(row["value"])
                        if isinstance(b, dict):
                            bubble_map[bid] = b
                    except Exception:
                        pass
    
            # Load codeBlockDiffs
            for row in global_db.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"):
                chat_id = _extract_chat_id_from_code_block_diff_key(row["key"])
                if not chat_id:
                    continue
                try:
                    d = json.loads(row["value"])
                    code_block_diff_map.setdefault(chat_id, []).append({
                        **d,
                        "diffId": row["key"].split(":")[2] if len(row["key"].split(":")) > 2 else None,
                    })
                except Exception:
                    pass
    
            # Load messageRequestContext
            for row in global_db.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"):
                parts = row["key"].split(":")
                if len(parts) >= 3:
                    chat_id = parts[1]
                    context_id = parts[2]
                    try:
                        ctx = json.loads(row["value"])
                        message_request_context_map.setdefault(chat_id, []).append({
                            **ctx,
                            "contextId": context_id,
                        })
                    except Exception:
                        pass
    
            # Build projectLayoutsMap
            project_layouts_map: dict[str, list] = {}
            for row in global_db.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"):
                parts = row["key"].split(":")
                if len(parts) >= 2:
                    cid = parts[1]
                    try:
                        ctx = json.loads(row["value"])
                        layouts = ctx.get("projectLayouts")
                        if isinstance(layouts, list):
                            project_layouts_map.setdefault(cid, [])
                            for layout in layouts:
                                if isinstance(layout, str):
                                    try:
                                        obj = json.loads(layout)
                                        if isinstance(obj, dict) and obj.get("rootPath"):
                                            project_layouts_map[cid].append(obj["rootPath"])
                                    except Exception:
                                        pass
                    except Exception:
                        pass
    
            # Get composer data entries with conversations
            composer_rows = global_db.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                " AND value LIKE '%fullConversationHeadersOnly%'"
                " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
            ).fetchall()
    
            invalid_workspace_aliases = _infer_invalid_workspace_aliases(
                composer_rows=composer_rows,
                project_layouts_map=project_layouts_map,
                project_name_map=project_name_map,
                workspace_path_map=workspace_path_map,
                workspace_entries=workspace_entries,
                bubble_map=bubble_map,
                composer_id_to_ws=composer_id_to_ws,
                invalid_workspace_ids=invalid_workspace_ids,
            )
    
            for row in composer_rows:
                composer_id = row["key"].split(":")[1]
                try:
                    cd = json.loads(row["value"])
    
                    # Determine project
                    pid = _determine_project_for_conversation(
                        cd, composer_id, project_layouts_map,
                        project_name_map, workspace_path_map,
                        workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids
                    )
                    mapped_ws = composer_id_to_ws.get(composer_id)
                    if not pid and mapped_ws in invalid_workspace_ids:
                        pid = invalid_workspace_aliases.get(mapped_ws)
                    assigned = pid if pid else "global"
    
                    if assigned not in matching_ws_ids:
                        continue
    
                    headers = cd.get("fullConversationHeadersOnly") or []
    
                    # Build bubbles
                    bubbles = []
                    for header in headers:
                        bubble_id = header.get("bubbleId")
                        bubble = bubble_map.get(bubble_id)
                        if not bubble:
                            continue
    
                        is_user = header.get("type") == 1
                        msg_type = "user" if is_user else "ai"
                        text = extract_text_from_bubble(bubble)
    
                        # Append messageRequestContext info
                        context_text = ""
                        for ctx in message_request_context_map.get(composer_id, []):
                            if ctx.get("bubbleId") == bubble_id:
                                if ctx.get("gitStatusRaw"):
                                    context_text += f"\n\n**Git Status:**\n```\n{ctx['gitStatusRaw']}\n```"
                                tf = ctx.get("terminalFiles")
                                if isinstance(tf, list) and tf:
                                    context_text += "\n\n**Terminal Files:**"
                                    for f in tf:
                                        context_text += f"\n- {f.get('path', '')}"
                                af = ctx.get("attachedFoldersListDirResults")
                                if isinstance(af, list) and af:
                                    context_text += "\n\n**Attached Folders:**"
                                    for fld in af:
                                        files = fld.get("files")
                                        if isinstance(files, list) and files:
                                            context_text += f"\n\n**Folder:** {fld.get('path', 'Unknown')}"
                                            for fi in files:
                                                context_text += f"\n- {fi.get('name', '')} ({fi.get('type', '')})"
                                cr = ctx.get("cursorRules")
                                if isinstance(cr, list) and cr:
                                    context_text += "\n\n**Cursor Rules:**"
                                    for rule in cr:
                                        context_text += f"\n- {rule.get('name') or rule.get('description') or 'Rule'}"
                                sc = ctx.get("summarizedComposers")
                                if isinstance(sc, list) and sc:
                                    context_text += "\n\n**Related Conversations:**"
                                    for comp in sc:
                                        context_text += f"\n- {comp.get('name') or comp.get('composerId') or 'Conversation'}"
    
                        full_text = text + context_text
    
                        raw = bubble
                        token_count = raw.get("tokenCount")
    
                        # Tool calls
                        tool_calls = None
                        tfd = raw.get("toolFormerData")
                        if isinstance(tfd, dict):
                            tool_call = _parse_tool_call(tfd)
                            tool_calls = [tool_call]
    
                        # Thinking
                        thinking = None
                        thinking_duration_ms = None
                        if raw.get("thinking"):
                            thinking = raw["thinking"] if isinstance(raw["thinking"], str) else (raw["thinking"].get("text") if isinstance(raw["thinking"], dict) else None)
                            thinking_duration_ms = raw.get("thinkingDurationMs")
    
                        has_content = full_text.strip() or tool_calls or thinking
                        if not has_content:
                            continue
    
                        # Context window
                        ctx_window = raw.get("contextWindowStatusAtCreation") or {}
                        ctx_pct = ctx_window.get("percentageRemainingFloat") or ctx_window.get("percentageRemaining")
    
                        # Display text fallbacks
                        display_text = full_text.strip()
                        if not display_text and tool_calls:
                            tc = tool_calls[0]
                            display_text = f"**Tool: {tc.get('name', 'unknown')}**"
                            if tc.get("status"):
                                display_text += f" ({tc['status']})"
                        if not display_text and thinking:
                            display_text = thinking
    
                        # Build metadata for BOTH user and AI bubbles
                        bubble_meta = None
                        if bubble:
                            model_info = raw.get("modelInfo") or {}
                            model_name = model_info.get("modelName")
                            if model_name == "default":
                                model_name = None
    
                            if msg_type == "ai":
                                tc_dict = token_count if isinstance(token_count, dict) else {}
                                # Only include token counts if they are actually non-zero
                                in_tok = tc_dict.get("inputTokens") or 0
                                out_tok = tc_dict.get("outputTokens") or 0
                                cached_tok = tc_dict.get("cachedTokens") or 0
                                bubble_meta = {
                                    "modelName": model_name,
                                    "inputTokens": in_tok if in_tok > 0 else None,
                                    "outputTokens": out_tok if out_tok > 0 else None,
                                    "cachedTokens": cached_tok if cached_tok > 0 else None,
                                    "toolResultsCount": (len(tool_calls) if tool_calls else None) or (len(raw["toolResults"]) if isinstance(raw.get("toolResults"), list) and raw["toolResults"] else None),
                                    "toolResults": raw.get("toolResults") if isinstance(raw.get("toolResults"), list) and raw["toolResults"] else None,
                                    "toolCalls": tool_calls,
                                    "thinking": thinking,
                                    "thinkingDurationMs": thinking_duration_ms,
                                    "contextWindowPercent": ctx_pct,
                                }
                            elif msg_type == "user":
                                bubble_meta = {
                                    "modelName": model_name,
                                    "contextWindowPercent": ctx_pct,
                                }
                                # Context window token details from user bubbles
                                if ctx_window:
                                    tokens_used = ctx_window.get("tokensUsed", 0)
                                    token_limit = ctx_window.get("tokenLimit", 0)
                                    if tokens_used > 0:
                                        bubble_meta["contextTokensUsed"] = tokens_used
                                    if token_limit > 0:
                                        bubble_meta["contextTokenLimit"] = token_limit
    
                            # Strip None values and only include if something is set
                            if bubble_meta:
                                bubble_meta = {k: v for k, v in bubble_meta.items() if v is not None}
                                if not bubble_meta:
                                    bubble_meta = None
    
                        b_entry = {
                            "type": msg_type,
                            "text": display_text,
                            "timestamp": to_epoch_ms(bubble.get("createdAt")) or to_epoch_ms(bubble.get("timestamp")) or int(datetime.now().timestamp() * 1000),
                        }
                        if bubble_meta:
                            b_entry["metadata"] = bubble_meta
                        bubbles.append(b_entry)
    
                    if not bubbles:
                        continue
    
                    # Title
                    title = cd.get("name") or f"Conversation {composer_id[:8]}"
                    if not cd.get("name") and bubbles:
                        first_msg = bubbles[0].get("text", "")
                        if first_msg:
                            first_lines = [ln for ln in first_msg.split("\n") if ln.strip()]
                            if first_lines:
                                title = first_lines[0][:100]
                                if len(title) == 100:
                                    title += "..."
    
                    # Early exclusion check — run before expensive metadata aggregation
                    _early_model_config = cd.get("modelConfig") or {}
                    _early_model_name = _early_model_config.get("modelName")
                    _early_model_names = [_early_model_name] if _early_model_name and _early_model_name != "default" else None
                    if is_excluded_by_rules(rules, build_searchable_text(
                        project_name=workspace_display_name,
                        chat_title=title,
                        model_names=_early_model_names,
                    )):
                        continue
    
                    # Code block diffs as extra bubbles
                    diffs = code_block_diff_map.get(composer_id, [])
                    for diff in diffs:
                        diff_text = format_tool_action(diff)
                        if diff_text.strip():
                            bubbles.append({
                                "type": "ai",
                                "text": f"**Tool Action:**{diff_text}",
                                "timestamp": int(datetime.now().timestamp() * 1000),
                            })
    
                    bubbles.sort(key=lambda b: b.get("timestamp") or 0)
    
                    # Response time calculation
                    last_user_ts = None
                    for b in bubbles:
                        if b["type"] == "user":
                            last_user_ts = b.get("timestamp")
                        elif b["type"] == "ai" and last_user_ts is not None:
                            ts = b.get("timestamp")
                            if ts and ts > last_user_ts:
                                meta = b.setdefault("metadata", {})
                                meta["responseTimeMs"] = ts - last_user_ts
    
                    # Aggregate metadata
                    total_input = 0
                    total_output = 0
                    total_cached = 0
                    total_response_ms = 0
                    total_cost = 0.0
                    total_tool_calls = 0
                    total_thinking_ms = 0
                    models_set = set()
                    for b in bubbles:
                        m = b.get("metadata") or {}
                        if m.get("inputTokens"):
                            total_input += m["inputTokens"]
                        if m.get("outputTokens"):
                            total_output += m["outputTokens"]
                        if m.get("cachedTokens"):
                            total_cached += m["cachedTokens"]
                        if m.get("responseTimeMs"):
                            total_response_ms += m["responseTimeMs"]
                        if m.get("cost") is not None:
                            total_cost += m["cost"]
                        if m.get("modelName"):
                            models_set.add(m["modelName"])
                        if m.get("toolCalls"):
                            total_tool_calls += len(m["toolCalls"])
                        if m.get("thinkingDurationMs"):
                            total_thinking_ms += m["thinkingDurationMs"]
    
                    # Composer-level cost fallback
                    usage = cd.get("usageData") or {}
                    composer_cost = usage.get("cost") or usage.get("estimatedCost")
                    if isinstance(composer_cost, (int, float)) and total_cost == 0:
                        total_cost = composer_cost
    
                    # Composer-level lines/files changed
                    lines_added = cd.get("totalLinesAdded", 0)
                    lines_removed = cd.get("totalLinesRemoved", 0)
                    files_added = cd.get("addedFiles", 0)
                    files_removed = cd.get("removedFiles", 0)
    
                    # Context window progression from user bubbles
                    max_ctx_tokens = 0
                    ctx_token_limit = 0
                    for b in bubbles:
                        m = b.get("metadata") or {}
                        if m.get("contextTokensUsed", 0) > max_ctx_tokens:
                            max_ctx_tokens = m["contextTokensUsed"]
                        if m.get("contextTokenLimit", 0) > ctx_token_limit:
                            ctx_token_limit = m["contextTokenLimit"]
    
                    tab_meta = None
                    has_any = any([total_input, total_output, total_cached, total_response_ms,
                                  total_cost, models_set, total_tool_calls, total_thinking_ms,
                                  lines_added, lines_removed, files_added, files_removed,
                                  max_ctx_tokens])
                    if has_any:
                        tab_meta_raw = {
                            "totalInputTokens": total_input or None,
                            "totalOutputTokens": total_output or None,
                            "totalCachedTokens": total_cached or None,
                            "modelsUsed": list(models_set) if models_set else None,
                            "totalResponseTimeMs": total_response_ms or None,
                            "totalCost": total_cost if total_cost > 0 else None,
                            "totalToolCalls": total_tool_calls or None,
                            "totalThinkingDurationMs": total_thinking_ms or None,
                            "totalLinesAdded": lines_added if lines_added else None,
                            "totalLinesRemoved": lines_removed if lines_removed else None,
                            "totalFilesAdded": files_added if files_added else None,
                            "totalFilesRemoved": files_removed if files_removed else None,
                            "maxContextTokensUsed": max_ctx_tokens if max_ctx_tokens else None,
                            "contextTokenLimit": ctx_token_limit if ctx_token_limit else None,
                        }
                        tab_meta = {k: v for k, v in tab_meta_raw.items() if v is not None}
    
                    # Model config from composer data
                    model_config = cd.get("modelConfig") or {}
                    model_name_from_config = model_config.get("modelName")
                    if model_name_from_config and model_name_from_config != "default":
                        if not tab_meta:
                            tab_meta = {}
                        if not tab_meta.get("modelsUsed"):
                            tab_meta["modelsUsed"] = [model_name_from_config]
                        elif model_name_from_config not in tab_meta["modelsUsed"]:
                            tab_meta["modelsUsed"].insert(0, model_name_from_config)
    
                    tab = {
                        "id": composer_id,
                        "title": title,
                        "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
                        "bubbles": [{
                            "type": b["type"],
                            "text": b.get("text", ""),
                            "timestamp": b.get("timestamp", 0),
                            **({"metadata": b["metadata"]} if b.get("metadata") else {}),
                        } for b in bubbles],
                        "codeBlockDiffs": diffs,
                    }
                    if tab_meta:
                        tab["metadata"] = tab_meta
    
                    response["tabs"].append(tab)
    
                except Exception:  # noqa: BLE001 — best-effort per-composer skip in a read-many loop; one malformed row must not 500 the tabs endpoint, and exc_info captures the concrete type for debugging.
                    _logger.warning("Error parsing composer data for %s", composer_id, exc_info=True)
    
            # Sort tabs by timestamp descending (newest first)
            response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
    
            return jsonify(response)

    except Exception:
        _logger.exception("Failed to get workspace tabs")
        return jsonify({"error": "Failed to get workspace tabs"}), 500
