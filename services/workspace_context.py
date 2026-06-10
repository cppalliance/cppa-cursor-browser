"""Workspace determination ceremony — single orchestrator for shared maps."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Any

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


@dataclass(frozen=True)
class WorkspaceContext:
    """Precomputed workspace-resolution maps for conversation assignment."""

    workspace_entries: list[dict[str, Any]]
    invalid_workspace_ids: set[str]
    composer_id_to_workspace_id: dict[str, str]
    project_name_to_workspace_id: dict[str, str]
    workspace_path_to_id: dict[str, str]
    project_layouts_map: dict[str, list[str]]
    bubble_map: dict[str, dict[str, Any]]


def _entries(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if workspace_entries is not None:
        return workspace_entries
    return collect_workspace_entries(workspace_path)


def _assemble_context(
    entries: list[dict[str, Any]],
    *,
    invalid_workspace_ids: set[str],
    workspace_path_to_id: dict[str, str],
    composer_id_to_workspace_id: dict[str, str],
) -> WorkspaceContext:
    return WorkspaceContext(
        workspace_entries=entries,
        invalid_workspace_ids=invalid_workspace_ids,
        composer_id_to_workspace_id=composer_id_to_workspace_id,
        project_name_to_workspace_id=create_project_name_to_workspace_id_map(entries),
        workspace_path_to_id=workspace_path_to_id,
        project_layouts_map={},
        bubble_map={},
    )


def resolve_workspace_context(
    workspace_path: str,
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
) -> WorkspaceContext:
    """Full workspace maps with an uncached composer→workspace scan (CLI export)."""
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=collect_invalid_workspace_ids(entries),
        workspace_path_to_id=create_workspace_path_to_id_map(entries),
        composer_id_to_workspace_id=build_composer_id_to_workspace_id(
            workspace_path, entries,
        ),
    )


def resolve_workspace_context_cached(
    workspace_path: str,
    rules: list[Any],
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
    nocache: bool = False,
) -> WorkspaceContext:
    """Full workspace maps with a mtime-keyed composer map (listing / tabs)."""
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=collect_invalid_workspace_ids(entries),
        workspace_path_to_id=create_workspace_path_to_id_map(entries),
        composer_id_to_workspace_id=build_composer_id_to_workspace_id_cached(
            workspace_path, entries, rules, nocache=nocache,
        ),
    )


def resolve_workspace_context_minimal(
    workspace_path: str,
    *,
    workspace_entries: list[dict[str, Any]] | None = None,
) -> WorkspaceContext:
    """Entries, project-name, and composer maps only (HTTP export).

    Args:
        workspace_path: Cursor ``workspaceStorage`` root.
        workspace_entries: Pre-collected entries; when ``None``, scanned from disk.
    """
    entries = _entries(workspace_path, workspace_entries)
    return _assemble_context(
        entries,
        invalid_workspace_ids=set(),
        workspace_path_to_id={},
        composer_id_to_workspace_id=build_composer_id_to_workspace_id(
            workspace_path, entries,
        ),
    )


def enrich_workspace_context_from_global_db(
    ctx: WorkspaceContext,
    global_db: sqlite3.Connection,
    *,
    populate_project_layouts: bool = False,
    populate_bubble_map: bool = False,
) -> WorkspaceContext:
    """Return *ctx* with global KV maps loaded from an open global DB connection."""
    updates: dict[str, Any] = {}
    if populate_project_layouts:
        updates["project_layouts_map"] = load_project_layouts_map(global_db)
    if populate_bubble_map:
        updates["bubble_map"] = load_bubble_map(global_db)
    if not updates:
        return ctx
    return replace(ctx, **updates)
