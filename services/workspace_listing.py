from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from utils.cli_chat_reader import list_cli_projects
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.path_helpers import (
    get_workspace_folder_paths,
    normalize_file_path,
    to_epoch_ms,
)
from utils.workspace_descriptor import _read_json_file
from utils.workspace_path import get_cli_chats_path
from services.workspace_db import (
    _build_composer_id_to_workspace_id,
    _collect_invalid_workspace_ids,
    _collect_workspace_entries,
    _open_global_db,
)
from services.workspace_resolver import (
    _create_project_name_to_workspace_id_map,
    _create_workspace_path_to_id_map,
    _determine_project_for_conversation,
    _get_workspace_display_name,
    _infer_invalid_workspace_aliases,
    _infer_workspace_name_from_context,
)


def list_workspace_projects(workspace_path: str, rules: list) -> list[dict]:
    """Return the sorted project list that GET /api/workspaces renders."""
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
                composer_rows = global_db.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND LENGTH(value) > 10"
                ).fetchall()

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
                                        layout = json.loads(layout)
                                    except Exception:
                                        continue
                                if isinstance(layout, dict) and layout.get("rootPath"):
                                    project_layouts_map[cid].append(layout["rootPath"])
                    except Exception:
                        pass

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
                            workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids,
                        )
                        mapped_ws = composer_id_to_ws.get(cid)
                        if not pid and mapped_ws in invalid_workspace_ids:
                            pid = invalid_workspace_aliases.get(mapped_ws)
                        assigned = pid if pid else "global"

                        headers = cd.get("fullConversationHeadersOnly") or []
                        has_bubbles = any(
                            bubble_map.get(h.get("bubbleId"))
                            for h in headers
                            if isinstance(h, dict)
                        )
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

    # Group workspace entries by normalized folder path
    folder_to_entries: dict[str, list] = {}
    entry_folder_map: dict[str, str] = {}
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

    projects: list[dict] = []
    seen_folders: set = set()
    for entry in workspace_entries:
        norm_folder = entry_folder_map[entry["name"]]
        if norm_folder in seen_folders:
            continue
        seen_folders.add(norm_folder)

        group = folder_to_entries[norm_folder]
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

        if is_excluded_by_rules(rules, workspace_name):
            continue

        convos = []
        for ws_id in all_ws_ids:
            for c in conversation_map.get(ws_id, []):
                searchable = build_searchable_text(
                    project_name=workspace_name,
                    chat_title=c.get("name"),
                )
                if not is_excluded_by_rules(rules, searchable):
                    convos.append(c)

        if not convos:
            continue

        projects.append({
            "id": primary["name"],
            "name": workspace_name,
            "path": primary["workspaceJsonPath"],
            "conversationCount": len(convos),
            "lastModified": datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
            **({"aliasIds": all_ws_ids} if len(all_ws_ids) > 1 else {}),
        })

    # Global (unmatched) conversations
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

    # Cursor CLI projects
    try:
        cli_projects = list_cli_projects(get_cli_chats_path())
        for cp in cli_projects:
            ws_name = cp["workspace_name"] or cp["project_id"][:12]
            if is_excluded_by_rules(rules, ws_name):
                continue
            cli_convos = []
            for s in cp["sessions"]:
                session_id = s.get("session_id")
                if not session_id:
                    continue
                meta = s.get("meta") or {}
                session_name = meta.get("name") or f"Session {session_id[:8]}"
                searchable = build_searchable_text(
                    project_name=ws_name,
                    chat_title=session_name,
                )
                if not is_excluded_by_rules(rules, searchable):
                    cli_convos.append(session_name)
            if not cli_convos:
                continue
            last_ms = cp["last_updated_ms"]
            projects.append({
                "id": f"cli:{cp['project_id']}",
                "name": ws_name,
                "conversationCount": len(cli_convos),
                "lastModified": (
                    datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).isoformat()
                    if last_ms
                    else datetime.now(tz=timezone.utc).isoformat()
                ),
                "source": "cli",
            })
    except Exception as e:
        print(f"Failed to load CLI projects: {e}")

    projects.sort(key=lambda p: p["lastModified"], reverse=True)
    return projects
