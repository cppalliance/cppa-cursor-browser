from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone

_logger = logging.getLogger(__name__)

from utils.cli_chat_reader import list_cli_projects
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.path_helpers import (
    get_workspace_folder_paths,
    normalize_file_path,
    to_epoch_ms,
    warn_workspace_json_read,
)
from utils.workspace_descriptor import read_json_file
from models import ParseWarningCollector
from services.summary_cache import (
    fingerprint_workspace_storage,
    get_cached_projects,
    nocache_enabled,
    set_cached_projects,
)
from services.workspace_context import resolve_workspace_context
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    collect_workspace_entries,
    global_storage_db_path,
    load_project_layouts_for_composer,
    load_project_layouts_map,
    open_global_db,
)
from utils.workspace_path import get_cli_chats_path
from services.workspace_resolver import (
    determine_project_for_conversation,
    infer_invalid_workspace_aliases,
    infer_workspace_name_from_context,
    lookup_workspace_display_name,
)


def _composer_valid_for_listing(
    cd: dict,
    composer_id: str,
    parse_warnings: ParseWarningCollector,
) -> bool:
    """Lightweight list-path checks aligned with :class:`models.Composer` requirements."""
    if "fullConversationHeadersOnly" not in cd:
        return False
    created_at = cd.get("createdAt")
    if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
        _logger.warning(
            "Failed to parse Composer from composerData:%s: expected timestamp number for createdAt, got %s",
            composer_id,
            type(created_at).__name__,
        )
        parse_warnings.record_composer_skipped()
        return False
    headers = cd.get("fullConversationHeadersOnly")
    if not isinstance(headers, list):
        _logger.warning(
            "Failed to parse Composer from composerData:%s: fullConversationHeadersOnly must be a list",
            composer_id,
        )
        parse_warnings.record_composer_skipped()
        return False
    return True


def list_workspace_projects(
    workspace_path: str,
    rules: list,
    *,
    nocache: bool = False,
) -> tuple[list[dict], list[dict]]:
    """List workspace projects for GET /api/workspaces.

    Args:
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists from :func:`utils.exclusion_rules.load_rules`.
        nocache: When ``True``, skip the mtime-keyed disk cache (Phase 3).

    Returns:
        ``(projects, warnings)``. Each project dict has ``id``, ``name``,
        ``path`` (``workspace.json`` path), ``conversationCount``,
        ``lastModified`` (ISO 8601), and optional ``aliasIds`` / ``source``
        (``"cli"`` for Cursor CLI projects). *warnings* is a list of structured
        parse-error dicts (``type``, ``count``, ``detail``) from
        :meth:`models.ParseWarningCollector.to_api_list`; empty when no skips.
    """
    workspace_entries = collect_workspace_entries(workspace_path)
    gdb = global_storage_db_path(workspace_path)
    cli_path = get_cli_chats_path()
    fingerprint = fingerprint_workspace_storage(
        workspace_path,
        workspace_entries,
        global_db_path=gdb if os.path.isfile(gdb) else None,
        rules=rules,
        cli_chats_path=cli_path if os.path.isdir(cli_path) else None,
    )
    if not nocache_enabled(request_nocache=nocache):
        cached = get_cached_projects(fingerprint)
        if cached is not None:
            return cached

    projects, warnings = _build_workspace_projects_uncached(
        workspace_path, rules, workspace_entries, nocache=nocache,
    )
    if not nocache_enabled(request_nocache=nocache):
        set_cached_projects(fingerprint, projects, warnings)
    return projects, warnings


def _build_workspace_projects_uncached(
    workspace_path: str,
    rules: list,
    workspace_entries: list[dict],
    *,
    nocache: bool,
) -> tuple[list[dict], list[dict]]:
    parse_warnings = ParseWarningCollector()
    ctx = resolve_workspace_context(
        workspace_path,
        workspace_entries=workspace_entries,
        rules=rules,
        nocache=nocache,
        use_composer_cache=True,
    )
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id

    conversation_map: dict[str, list] = {}

    with open_global_db(workspace_path) as (global_db, _):
        if global_db:
            def _safe_fetchall(query: str, params: tuple = ()) -> list:
                try:
                    return global_db.execute(query, params).fetchall()
                except sqlite3.Error:
                    return []

            try:
                composer_rows = _safe_fetchall(COMPOSER_ROWS_WITH_HEADERS_SQL)

                project_layouts_map: dict[str, list] = {}
                if invalid_workspace_ids:
                    project_layouts_map = load_project_layouts_map(global_db)

                bubble_map: dict[str, dict] = {}
                invalid_workspace_aliases: dict[str, str] = {}
                if invalid_workspace_ids:
                    invalid_workspace_aliases = infer_invalid_workspace_aliases(
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
                    except (json.JSONDecodeError, TypeError, ValueError) as e:
                        _logger.warning(
                            "Failed to decode Composer from composerData:%s: %s",
                            cid,
                            e,
                        )
                        parse_warnings.record_composer_skipped()
                        continue
                    if not isinstance(cd, dict):
                        _logger.warning(
                            "Failed to parse Composer from composerData:%s: expected object, got %s",
                            cid,
                            type(cd).__name__,
                        )
                        parse_warnings.record_composer_skipped()
                        continue
                    if not _composer_valid_for_listing(cd, cid, parse_warnings):
                        continue
                    try:
                        if (
                            cid not in composer_id_to_ws
                            and cid not in project_layouts_map
                        ):
                            project_layouts_map[cid] = load_project_layouts_for_composer(
                                global_db, cid,
                            )
                        pid = determine_project_for_conversation(
                            cd, cid, project_layouts_map,
                            project_name_map, workspace_path_map,
                            workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids,
                        )
                        mapped_ws = composer_id_to_ws.get(cid)
                        if not pid and mapped_ws in invalid_workspace_ids:
                            pid = invalid_workspace_aliases.get(mapped_ws)
                        assigned = pid if pid else "global"

                        headers = cd.get("fullConversationHeadersOnly") or []
                        if not headers:
                            continue

                        conversation_map.setdefault(assigned, []).append({
                            "composerId": cid,
                            "name": cd.get("name") or f"Conversation {cid[:8]}",
                            "lastUpdatedAt": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0,
                            "createdAt": to_epoch_ms(cd.get("createdAt")) or 0,
                        })
                    except Exception as e:
                        _logger.warning(
                            "Failed to process Composer from composerData:%s: %s",
                            cid,
                            e,
                        )
                        parse_warnings.record_composer_processing_failure()
            except Exception as e:
                _logger.error(
                    "Failed to load composer rows from global storage: %s",
                    e,
                    exc_info=True,
                )

    # Group workspace entries by normalized folder path
    folder_to_entries: dict[str, list] = {}
    entry_folder_map: dict[str, str] = {}
    for entry in workspace_entries:
        norm_folder = ""
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            folders = get_workspace_folder_paths(wd)
            first_folder = folders[0] if folders else None
            if first_folder:
                norm_folder = normalize_file_path(first_folder)
        except Exception as e:
            warn_workspace_json_read(_logger, entry["name"], e)
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
        except Exception as e:
            _logger.warning(
                "Failed to resolve mtime for workspace folder %s: %s",
                norm_folder,
                e,
            )
            mtime = 0

        workspace_name = lookup_workspace_display_name(workspace_path, primary["name"])
        if workspace_name == primary["name"]:
            inferred = infer_workspace_name_from_context(workspace_path, primary["name"])
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
            if not isinstance(cp, dict):
                continue
            project_id = cp.get("project_id")
            if not isinstance(project_id, str) or not project_id:
                continue
            ws_name = cp.get("workspace_name") or project_id[:12]
            if is_excluded_by_rules(rules, ws_name):
                continue
            sessions = cp.get("sessions") or []
            if not isinstance(sessions, list):
                continue
            cli_convos = []
            for s in sessions:
                if not isinstance(s, dict):
                    continue
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
            last_ms = cp.get("last_updated_ms")
            projects.append({
                "id": f"cli:{project_id}",
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
        _logger.warning("Failed to load CLI projects: %s", e)

    projects.sort(key=lambda p: p["lastModified"], reverse=True)
    return projects, parse_warnings.to_api_list()
