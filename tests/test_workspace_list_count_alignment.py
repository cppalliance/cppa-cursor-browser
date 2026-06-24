"""Regression tests for issue #95 — list/summary count alignment and scan hygiene."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3

import pytest

from models import ParseWarningCollector
from services.workspace_composer_scan import parse_composer_data_row
from services.workspace_db import build_composer_id_to_workspace_id, collect_workspace_entries
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import (
    assemble_single_tab,
    assemble_workspace_tabs,
    list_workspace_tab_summaries,
)
from tests._fixture_ids import HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID


def _seed_global_db(path: str, *, extra_rows: list[tuple[str, str | None]] | None = None) -> None:
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"composerData:{HAPPY_COMPOSER_ID}",
                json.dumps({
                    "name": "Aligned conversation",
                    "createdAt": 1_715_000_000_000,
                    "lastUpdatedAt": 1_715_000_500_000,
                    "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}],
                    "modelConfig": {"modelName": "gpt-4o"},
                }),
            ),
        )
        if extra_rows:
            for key, value in extra_rows:
                conn.execute(
                    "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                    (key, value),
                )
        conn.commit()


def _seed_workspace(ws_root: str, ws_id: str, project_folder: str, composer_id: str) -> None:
    ws_dir = os.path.join(ws_root, ws_id)
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": project_folder}, f)
    with contextlib.closing(sqlite3.connect(os.path.join(ws_dir, "state.vscdb"))) as conn:
        conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"allComposers": [{"composerId": composer_id}]}),
            ),
        )
        conn.commit()


def _layout(tmp_path, *, extra_rows: list[tuple[str, str | None]] | None = None) -> str:
    ws_root = tmp_path / "workspaceStorage"
    global_root = tmp_path / "globalStorage"
    ws_root.mkdir()
    global_root.mkdir()
    project_folder = tmp_path / "proj"
    project_folder.mkdir()
    _seed_workspace(str(ws_root), HAPPY_WORKSPACE_ID, str(project_folder), HAPPY_COMPOSER_ID)
    _seed_global_db(str(global_root / "state.vscdb"), extra_rows=extra_rows)
    return str(ws_root)


def test_project_card_count_matches_summary_tabs(tmp_path):
    ws_root = _layout(tmp_path)
    projects, _ = list_workspace_projects(ws_root, rules=[], nocache=True)
    card = next(p for p in projects if p["id"] == HAPPY_WORKSPACE_ID)

    payload, status = list_workspace_tab_summaries(
        HAPPY_WORKSPACE_ID, ws_root, rules=[], nocache=True,
    )
    assert status == 200
    assert card["conversationCount"] == len(payload["tabs"])


def test_null_composer_placeholder_skipped_silently():
    warnings = ParseWarningCollector()
    assert parse_composer_data_row(
        "composerData:empty-state-draft",
        None,
        parse_warnings=warnings,
    ) is None
    assert warnings.to_api_list() == []


def test_parallel_composer_registry_covers_all_workspaces(tmp_path):
    ws_root = tmp_path / "workspaceStorage"
    ws_root.mkdir()
    ids = [f"ws-{i}" for i in range(6)]
    for i, ws_id in enumerate(ids):
        folder = tmp_path / f"proj-{i}"
        folder.mkdir()
        _seed_workspace(str(ws_root), ws_id, str(folder), f"cmp-{i}")

    entries = collect_workspace_entries(str(ws_root))
    mapping = build_composer_id_to_workspace_id(str(ws_root), entries)
    assert len(mapping) == len(ids)
    for i, ws_id in enumerate(ids):
        assert mapping[f"cmp-{i}"] == ws_id


def test_summary_and_full_tabs_share_assignment(tmp_path):
    """Summary and full /tabs agree on which composers belong after assignment."""
    ws_root = _layout(tmp_path)
    global_db = os.path.join(tmp_path, "globalStorage", "state.vscdb")
    with contextlib.closing(sqlite3.connect(global_db)) as conn:
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{HAPPY_COMPOSER_ID}:b1",
                json.dumps({"type": "user", "text": "hello", "bubbleId": "b1"}),
            ),
        )
        conn.commit()

    summary, summary_status = list_workspace_tab_summaries(
        HAPPY_WORKSPACE_ID, ws_root, rules=[], nocache=True,
    )
    full, full_status = assemble_workspace_tabs(
        HAPPY_WORKSPACE_ID, ws_root, rules=[],
    )
    assert summary_status == 200
    assert full_status == 200
    summary_ids = {t["id"] for t in summary["tabs"]}
    full_ids = {t["id"] for t in full["tabs"]}
    assert summary_ids
    assert full_ids
    assert summary_ids == full_ids
    assert HAPPY_COMPOSER_ID in summary_ids


def test_single_tab_null_composer_placeholder_returns_404(tmp_path):
    ws_root = _layout(
        tmp_path,
        extra_rows=[("composerData:empty-state-draft", None)],
    )
    payload, status = assemble_single_tab(
        "global", "empty-state-draft", ws_root, rules=[],
    )
    assert status == 404
    assert "error" in payload


def test_excluded_model_dropped_from_list_and_summary(tmp_path):
    ws_root = _layout(tmp_path)
    rules = [["gpt-4o"]]

    projects, _ = list_workspace_projects(ws_root, rules=rules, nocache=True)
    card = next((p for p in projects if p["id"] == HAPPY_WORKSPACE_ID), None)
    payload, status = list_workspace_tab_summaries(
        HAPPY_WORKSPACE_ID, ws_root, rules=rules, nocache=True,
    )
    assert status == 200
    if card is None:
        assert len(payload["tabs"]) == 0
    else:
        assert card["conversationCount"] == len(payload["tabs"])
