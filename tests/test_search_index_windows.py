"""Regression: index rebuild must not replace an open SQLite file on Windows."""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from services.search_index import _publish_active_index


@pytest.fixture
def locked_index_layout(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    old_db = cache_dir / "search_index.oldgen.sqlite"
    with contextlib.closing(sqlite3.connect(old_db)) as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
        conn.execute("INSERT INTO t VALUES (1)")
        conn.commit()
    (cache_dir / "search_index.active").write_text(old_db.name, encoding="utf-8")

    ws_root = tmp_path / "workspaceStorage"
    global_root = tmp_path / "globalStorage"
    ws_root.mkdir()
    global_root.mkdir()
    gdb = global_root / "state.vscdb"
    with contextlib.closing(sqlite3.connect(gdb)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.commit()

    monkeypatch.setenv("WORKSPACE_PATH", str(ws_root))
    return cache_dir, old_db


def test_publish_active_index_does_not_replace_open_db(locked_index_layout):
    cache_dir, old_db = locked_index_layout
    new_db = cache_dir / "search_index.newgen.sqlite"
    new_db.write_bytes(b"sqlite placeholder")

    # Simulate an open reader on the old generation (Windows lock).
    with contextlib.closing(sqlite3.connect(old_db)) as _reader:
        with patch("services.search_index.CACHE_DIR", cache_dir), patch(
            "services.search_index.SEARCH_INDEX_POINTER_FILE",
            cache_dir / "search_index.active",
        ):
            _publish_active_index(new_db)
            pointer = (cache_dir / "search_index.active").read_text(encoding="utf-8").strip()
            assert pointer == new_db.name
            assert old_db.is_file()
