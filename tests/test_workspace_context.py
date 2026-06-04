"""Tests for workspace determination ceremony orchestrator (issue #91)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from services.workspace_context import (
    WorkspaceContext,
    enrich_workspace_context_from_global_db,
    resolve_workspace_context,
)


def _make_workspace_root(tmp: str) -> str:
    ws_root = os.path.join(tmp, "workspaceStorage")
    os.makedirs(ws_root)
    ws_id = "abc123workspace"
    ws_dir = os.path.join(ws_root, ws_id)
    os.makedirs(ws_dir)
    wj = {
        "folders": [{"path": "file:///tmp/myproject"}],
    }
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump(wj, f)
    return ws_root


def test_resolve_workspace_context_minimal_flags():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        ctx = resolve_workspace_context(
            ws_root,
            include_invalid_workspace_ids=False,
            include_workspace_path_map=False,
        )
        assert isinstance(ctx, WorkspaceContext)
        assert len(ctx.workspace_entries) == 1
        assert ctx.invalid_workspace_ids == set()
        assert ctx.workspace_path_to_id == {}
        assert ctx.project_layouts_map == {}
        assert ctx.bubble_map == {}


def test_resolve_workspace_context_full_workspace_maps():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        ctx = resolve_workspace_context(ws_root)
        assert len(ctx.workspace_entries) == 1
        assert "myproject" in ctx.project_name_to_workspace_id
        assert ctx.project_name_to_workspace_id["myproject"] == "abc123workspace"
        assert len(ctx.workspace_path_to_id) >= 1


def test_resolve_workspace_context_requires_rules_for_cache():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        with pytest.raises(ValueError, match="rules is required"):
            resolve_workspace_context(ws_root, use_composer_cache=True)


def test_resolve_workspace_context_accepts_pre_collected_entries():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        entries = [{"name": "x", "workspaceJsonPath": "/fake/workspace.json"}]
        with patch(
            "services.workspace_context.collect_workspace_entries",
            return_value=entries,
        ) as mock_collect:
            ctx = resolve_workspace_context(ws_root, workspace_entries=entries)
        mock_collect.assert_not_called()
        assert ctx.workspace_entries is entries


def test_enrich_workspace_context_from_global_db():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        ctx = resolve_workspace_context(ws_root)
        db_path = os.path.join(tmp, "global.vscdb")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("bubbleId:cid1:bid1", json.dumps({"type": 1, "text": "hi"})),
        )
        conn.commit()
        try:
            enriched = enrich_workspace_context_from_global_db(
                ctx, conn, populate_bubble_map=True,
            )
        finally:
            conn.close()
        assert enriched.bubble_map.get("bid1") is not None
        assert ctx.bubble_map == {}
