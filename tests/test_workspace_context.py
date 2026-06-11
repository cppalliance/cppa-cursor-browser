"""Tests for workspace determination ceremony orchestrator (issue #91)."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

from services.workspace_context import (
    WorkspaceContext,
    enrich_workspace_context_from_global_db,
    resolve_workspace_context,
    resolve_workspace_context_cached,
    resolve_workspace_context_minimal,
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


def _add_workspace_without_folders(ws_root: str, ws_id: str) -> None:
    """Workspace folder with empty ``folders`` — treated as invalid by collect_invalid_workspace_ids."""
    ws_dir = os.path.join(ws_root, ws_id)
    os.makedirs(ws_dir)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folders": []}, f)


def _open_global_db(tmp: str) -> sqlite3.Connection:
    db_path = os.path.join(tmp, "global.vscdb")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def test_resolve_workspace_context_minimal():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        ctx = resolve_workspace_context_minimal(ws_root)
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


def test_resolve_workspace_context_populates_invalid_workspace_ids():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        _add_workspace_without_folders(ws_root, "invalidws")
        ctx = resolve_workspace_context(ws_root)
        assert ctx.invalid_workspace_ids == {"invalidws"}
        assert "abc123workspace" not in ctx.invalid_workspace_ids


def test_resolve_workspace_context_cached_passes_nocache():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        with patch(
            "services.workspace_context.build_composer_id_to_workspace_id_cached",
            return_value={},
        ) as mock_cached:
            resolve_workspace_context_cached(ws_root, [], nocache=True)
        mock_cached.assert_called_once()
        assert mock_cached.call_args.kwargs["nocache"] is True


def test_resolve_workspace_context_cached_uses_cached_composer_map():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        rules = [["token"]]
        cached_map = {"composer-abc": "abc123workspace"}
        with (
            patch(
                "services.workspace_context.build_composer_id_to_workspace_id_cached",
                return_value=cached_map,
            ) as mock_cached,
            patch(
                "services.workspace_context.build_composer_id_to_workspace_id",
            ) as mock_scan,
        ):
            ctx = resolve_workspace_context_cached(ws_root, rules)
        mock_cached.assert_called_once_with(
            ws_root, ctx.workspace_entries, rules, nocache=False,
        )
        mock_scan.assert_not_called()
        assert ctx.composer_id_to_workspace_id == cached_map
        assert ctx.invalid_workspace_ids == set()
        assert len(ctx.workspace_path_to_id) >= 1


def test_resolve_workspace_context_cached_accepts_pre_collected_entries():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        entries = [{"name": "x", "workspaceJsonPath": "/fake/workspace.json"}]
        with patch(
            "services.workspace_context.collect_workspace_entries",
            return_value=entries,
        ) as mock_collect:
            ctx = resolve_workspace_context_cached(
                ws_root, [], workspace_entries=entries,
            )
        mock_collect.assert_not_called()
        assert ctx.workspace_entries is entries


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


def test_resolve_workspace_context_minimal_accepts_pre_collected_entries():
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = _make_workspace_root(tmp)
        entries = [{"name": "x", "workspaceJsonPath": "/fake/workspace.json"}]
        with patch(
            "services.workspace_context.collect_workspace_entries",
            return_value=entries,
        ) as mock_collect:
            ctx = resolve_workspace_context_minimal(ws_root, workspace_entries=entries)
        mock_collect.assert_not_called()
        assert ctx.workspace_entries is entries


def test_enrich_populates_bubble_map():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = resolve_workspace_context(_make_workspace_root(tmp))
        conn = _open_global_db(tmp)
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
        loaded = enriched.bubble_map.get("bid1")
        assert loaded is not None
        assert loaded.text == "hi"
        assert ctx.bubble_map == {}


def test_enrich_populates_project_layouts_map():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = resolve_workspace_context(_make_workspace_root(tmp))
        conn = _open_global_db(tmp)
        mrc = {
            "projectLayouts": [json.dumps({"rootPath": "/tmp/myproject"})],
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("messageRequestContext:composer-1:ctx1", json.dumps(mrc)),
        )
        conn.commit()
        try:
            enriched = enrich_workspace_context_from_global_db(
                ctx, conn, populate_project_layouts=True,
            )
        finally:
            conn.close()
        assert enriched.project_layouts_map["composer-1"] == ["/tmp/myproject"]
        assert ctx.project_layouts_map == {}


def test_enrich_populates_both_global_maps():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = resolve_workspace_context(_make_workspace_root(tmp))
        conn = _open_global_db(tmp)
        mrc = {
            "projectLayouts": [json.dumps({"rootPath": "/tmp/myproject"})],
        }
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("messageRequestContext:composer-1:ctx1", json.dumps(mrc)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("bubbleId:cid1:bid1", json.dumps({"type": 1, "text": "hi"})),
        )
        conn.commit()
        try:
            enriched = enrich_workspace_context_from_global_db(
                ctx,
                conn,
                populate_project_layouts=True,
                populate_bubble_map=True,
            )
        finally:
            conn.close()
        assert enriched.project_layouts_map["composer-1"] == ["/tmp/myproject"]
        loaded = enriched.bubble_map.get("bid1")
        assert loaded is not None
        assert loaded.text == "hi"
        assert ctx.project_layouts_map == {}
        assert ctx.bubble_map == {}


def test_enrich_with_no_flags_returns_unchanged_context():
    with tempfile.TemporaryDirectory() as tmp:
        ctx = resolve_workspace_context(_make_workspace_root(tmp))
        conn = _open_global_db(tmp)
        conn.commit()
        try:
            result = enrich_workspace_context_from_global_db(ctx, conn)
        finally:
            conn.close()
        assert result is ctx
