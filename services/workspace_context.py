"""Workspace determination ceremony — single orchestrator for shared maps."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from services.workspace_db import (
    build_composer_id_to_workspace_id,
    build_composer_id_to_workspace_id_cached,
    collect_invalid_workspace_ids,
    collect_workspace_entries,
    load_bubble_map,
    load_project_layouts_map,
)
from services.workspace_resolver import (
    create_project_name_to_workspace_id_map,
    create_workspace_path_to_id_map,
)

if TYPE_CHECKING:
    import sqlite3


@dataclass(frozen=True)
class WorkspaceContext:
    """Precomputed workspace-resolution maps for conversation assignment."""

    workspace_path: str
    workspace_entries: list[dict]
    invalid_workspace_ids: set[str]
    composer_id_to_workspace_id: dict[str, str]
    project_name_to_workspace_id: dict[str, str]
    workspace_path_to_id: dict[str, str]
    project_layouts_map: dict[str, list]
    bubble_map: dict[str, dict]


def resolve_workspace_context(
    workspace_path: str,
    *,
    workspace_entries: list[dict] | None = None,
    rules: list | None = None,
    nocache: bool = False,
    use_composer_cache: bool = False,
    include_invalid_workspace_ids: bool = True,
    include_workspace_path_map: bool = True,
    global_db: sqlite3.Connection | None = None,
    populate_project_layouts: bool = False,
    populate_bubble_map: bool = False,
) -> WorkspaceContext:
    """Run the workspace-determination ceremony and return a typed context.

    Always resolves ``workspace_entries`` (when not supplied), composer and
    project-name maps. Optional pieces are controlled by flags so lightweight
    consumers (e.g. HTTP export) can omit unused maps.

    Args:
        workspace_path: Cursor ``workspaceStorage`` root.
        workspace_entries: Pre-collected entries; when ``None``, scanned from disk.
        rules: Exclusion rules; required when ``use_composer_cache`` is ``True``.
        nocache: Skip the mtime-keyed composer-map disk cache.
        use_composer_cache: Use :func:`build_composer_id_to_workspace_id_cached`.
        include_invalid_workspace_ids: When ``False``, ``invalid_workspace_ids`` is empty.
        include_workspace_path_map: When ``False``, ``workspace_path_to_id`` is empty.
        global_db: Open global ``state.vscdb`` connection for optional KV loads.
        populate_project_layouts: Populate ``project_layouts_map`` from *global_db*.
        populate_bubble_map: Populate ``bubble_map`` from *global_db*.

    Returns:
        :class:`WorkspaceContext` with all requested maps populated.
    """
    entries = (
        workspace_entries
        if workspace_entries is not None
        else collect_workspace_entries(workspace_path)
    )
    invalid_ids = (
        collect_invalid_workspace_ids(entries)
        if include_invalid_workspace_ids
        else set()
    )
    project_name_map = create_project_name_to_workspace_id_map(entries)
    workspace_path_map = (
        create_workspace_path_to_id_map(entries)
        if include_workspace_path_map
        else {}
    )
    if use_composer_cache:
        if rules is None:
            raise ValueError("rules is required when use_composer_cache=True")
        composer_id_to_ws = build_composer_id_to_workspace_id_cached(
            workspace_path, entries, rules, nocache=nocache,
        )
    else:
        composer_id_to_ws = build_composer_id_to_workspace_id(workspace_path, entries)

    project_layouts: dict[str, list] = {}
    bubble_map: dict[str, dict] = {}
    if global_db is not None:
        if populate_project_layouts:
            project_layouts = load_project_layouts_map(global_db)
        if populate_bubble_map:
            bubble_map = load_bubble_map(global_db)

    return WorkspaceContext(
        workspace_path=workspace_path,
        workspace_entries=entries,
        invalid_workspace_ids=invalid_ids,
        composer_id_to_workspace_id=composer_id_to_ws,
        project_name_to_workspace_id=project_name_map,
        workspace_path_to_id=workspace_path_map,
        project_layouts_map=project_layouts,
        bubble_map=bubble_map,
    )


def enrich_workspace_context_from_global_db(
    ctx: WorkspaceContext,
    global_db: sqlite3.Connection,
    *,
    populate_project_layouts: bool = False,
    populate_bubble_map: bool = False,
) -> WorkspaceContext:
    """Return *ctx* with global KV maps loaded from an open global DB connection."""
    updates: dict = {}
    if populate_project_layouts:
        updates["project_layouts_map"] = load_project_layouts_map(global_db)
    if populate_bubble_map:
        updates["bubble_map"] = load_bubble_map(global_db)
    if not updates:
        return ctx
    return replace(ctx, **updates)
