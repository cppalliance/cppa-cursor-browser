"""Tests for services/search_index.py (Phase 2 FTS index)."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from unittest.mock import patch

import pytest

from models import ParseWarningCollector
from services.search import search_global_storage
from services.search_index import (
    build_search_index,
    index_is_usable,
    query_composer_bubble_hits,
)


def _seed_global_db(global_root: str, composer_id: str, bubble_text: str) -> None:
    db_path = os.path.join(global_root, "state.vscdb")
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"composerData:{composer_id}",
                json.dumps({
                    "name": "Indexed conversation",
                    "createdAt": 1_780_000_000_000,
                    "lastUpdatedAt": 1_780_001_000_000,
                    "fullConversationHeadersOnly": [{"bubbleId": "b-idx-1"}],
                    "modelConfig": {"modelName": "gpt-4o"},
                }),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:b-idx-1",
                json.dumps({"type": "user", "text": bubble_text}),
            ),
        )
        conn.commit()


def _seed_workspace(ws_root: str, workspace_id: str, composer_id: str) -> None:
    ws_dir = os.path.join(ws_root, workspace_id)
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as fh:
        json.dump({"folder": "/projects/indexed"}, fh)
    db_path = os.path.join(ws_dir, "state.vscdb")
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"allComposers": [{"composerId": composer_id}]}),
            ),
        )
        conn.commit()


@pytest.fixture
def indexed_storage(monkeypatch, tmp_path):
    """Temp Cursor layout + isolated search index path."""
    ws_root = tmp_path / "workspaceStorage"
    global_root = tmp_path / "globalStorage"
    cache_dir = tmp_path / "cache"
    ws_root.mkdir()
    global_root.mkdir()
    cache_dir.mkdir()
    monkeypatch.setenv("WORKSPACE_PATH", str(ws_root))
    monkeypatch.setenv("CLI_CHATS_PATH", str(tmp_path / "cli-empty"))
    monkeypatch.delenv("CURSOR_CHAT_BROWSER_NO_SEARCH_INDEX", raising=False)
    monkeypatch.delenv("CURSOR_CHAT_BROWSER_NOCACHE", raising=False)
    os.makedirs(tmp_path / "cli-empty", exist_ok=True)

    composer_id = "cmp-indexed-1"
    term = "indexed-unique-sentinel-xyz"
    _seed_global_db(str(global_root), composer_id, f"please find {term}")
    _seed_workspace(str(ws_root), "ws-indexed-1", composer_id)

    with patch("services.search_index.CACHE_DIR", cache_dir), patch(
        "services.search_index.SEARCH_INDEX_POINTER_FILE", cache_dir / "search_index.active"
    ), patch("services.search_index.SEARCH_INDEX_FILE", cache_dir / "search_index.sqlite"):
        built = build_search_index(str(ws_root), [], force=True)
        assert built is True
        pointer = cache_dir / "search_index.active"
        assert pointer.is_file()
        index_path = cache_dir / pointer.read_text(encoding="utf-8").strip()
        assert index_path.is_file()
        yield {
            "ws_root": str(ws_root),
            "cache_dir": cache_dir,
            "index_path": index_path,
            "composer_id": composer_id,
            "term": term,
        }


def _index_patches(cache_dir):
    return (
        patch("services.search_index.CACHE_DIR", cache_dir),
        patch(
            "services.search_index.SEARCH_INDEX_POINTER_FILE",
            cache_dir / "search_index.active",
        ),
        patch("services.search_index.SEARCH_INDEX_FILE", cache_dir / "search_index.sqlite"),
    )


class TestSearchIndexBuild:
    def test_index_is_usable_after_build(self, indexed_storage):
        patches = _index_patches(indexed_storage["cache_dir"])
        with patches[0], patches[1], patches[2]:
            assert index_is_usable(indexed_storage["ws_root"], []) is True

    def test_bubble_fts_finds_term(self, indexed_storage):
        patches = _index_patches(indexed_storage["cache_dir"])
        with patches[0], patches[1], patches[2]:
            hits = query_composer_bubble_hits(
                indexed_storage["term"],
                since_ms=None,
            )
            assert indexed_storage["composer_id"] in hits
            assert any(indexed_storage["term"] in t for t in hits[indexed_storage["composer_id"]])


class TestSearchGlobalStorageUsesIndex:
    def test_search_global_storage_uses_index(self, indexed_storage):
        patches = _index_patches(indexed_storage["cache_dir"])
        with patches[0], patches[1], patches[2], patch(
            "services.search._search_global_storage_live_scan",
        ) as live_scan:
            results = search_global_storage(
                indexed_storage["ws_root"],
                indexed_storage["term"],
                indexed_storage["term"].lower(),
                [],
                ParseWarningCollector(),
                since_ms=None,
            )
            assert any(r["chatId"] == indexed_storage["composer_id"] for r in results)
            live_scan.assert_not_called()
