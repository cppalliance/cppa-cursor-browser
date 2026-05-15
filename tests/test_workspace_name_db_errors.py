from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_resolver import _infer_workspace_name_from_context


def _seed_local_state(workspace_path: str, workspace_id: str) -> None:
    """Local state.vscdb with one composer.composerData row + composer ID."""
    ws_dir = os.path.join(workspace_path, workspace_id)
    os.makedirs(ws_dir, exist_ok=True)
    db = os.path.join(ws_dir, "state.vscdb")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable VALUES (?, ?)",
        (
            "composer.composerData",
            json.dumps({"allComposers": [{"composerId": "cid-x"}]}),
        ),
    )
    conn.commit()
    conn.close()


class TestGlobalQueryErrorSwallowed(unittest.TestCase):
    def test_corrupt_cursordiskkv_does_not_propagate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # _open_global_db reads ``<workspace_path>/../globalStorage`` — so
            # workspaceStorage must be a child of tmp, not tmp itself.
            ws_root = os.path.join(tmp, "workspaceStorage")
            os.makedirs(ws_root, exist_ok=True)
            _seed_local_state(ws_root, "ws-corrupt")

            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(global_dir, exist_ok=True)
            gdb = os.path.join(global_dir, "state.vscdb")
            conn = sqlite3.connect(gdb)
            # Schema deliberately missing cursorDiskKV so the LIKE query
            # inside _infer_workspace_name_from_context raises
            # sqlite3.OperationalError("no such table").
            conn.execute("CREATE TABLE other (x INTEGER)")
            conn.commit()
            conn.close()

            try:
                result = _infer_workspace_name_from_context(ws_root, "ws-corrupt")
            except sqlite3.Error:
                self.fail("query error should be caught, not propagated")
            self.assertIsNone(result)


class TestLocalQueryErrorSwallowed(unittest.TestCase):
    def test_corrupt_local_state_vscdb_returns_none_not_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = os.path.join(tmp, "workspaceStorage")
            ws_dir = os.path.join(ws_root, "ws-bad-local")
            os.makedirs(ws_dir, exist_ok=True)
            # Local state.vscdb exists but has no ItemTable → execute raises
            conn = sqlite3.connect(os.path.join(ws_dir, "state.vscdb"))
            conn.execute("CREATE TABLE other (x INTEGER)")
            conn.commit()
            conn.close()

            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(global_dir, exist_ok=True)
            sqlite3.connect(os.path.join(global_dir, "state.vscdb")).close()

            try:
                result = _infer_workspace_name_from_context(ws_root, "ws-bad-local")
            except sqlite3.Error:
                self.fail("local query error should be caught, not propagated")
            self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
