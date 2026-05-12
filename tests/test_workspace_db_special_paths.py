from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_db import (
    _build_composer_id_to_workspace_id,
    _open_global_db,
)


def _make_state_vscdb(path: str, composer_id: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
        ("composer.composerData", json.dumps({"allComposers": [{"composerId": composer_id}]})),
    )
    conn.commit()
    conn.close()


def _make_global_state(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        ("composerData:probe", json.dumps({"name": "probe"})),
    )
    conn.commit()
    conn.close()


class TestSqliteUriEncoding(unittest.TestCase):
    def _build_fixture(self, parent: str) -> str:
        # workspace dir contains a space — naive f"file:{path}" mis-parses.
        ws_root = os.path.join(parent, "Cursor User", "workspaceStorage")
        os.makedirs(ws_root, exist_ok=True)
        ws_dir = os.path.join(ws_root, "ws-with spaces")
        os.makedirs(ws_dir, exist_ok=True)
        _make_state_vscdb(os.path.join(ws_dir, "state.vscdb"), "cid-space")
        global_dir = os.path.join(parent, "Cursor User", "globalStorage")
        os.makedirs(global_dir, exist_ok=True)
        _make_global_state(os.path.join(global_dir, "state.vscdb"))
        return ws_root

    def test_build_composer_id_to_workspace_id_handles_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = self._build_fixture(tmp)
            entries = [{"name": "ws-with spaces", "workspaceJsonPath": ""}]
            mapping = _build_composer_id_to_workspace_id(ws_root, entries)
            self.assertEqual(mapping, {"cid-space": "ws-with spaces"})

    def test_open_global_db_handles_spaces(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = self._build_fixture(tmp)
            with _open_global_db(ws_root) as (conn, path):
                self.assertIsNotNone(conn)
                row = conn.execute(
                    "SELECT key FROM cursorDiskKV WHERE key = 'composerData:probe'"
                ).fetchone()
                self.assertIsNotNone(row)


class TestOpenGlobalDbConnectFailure(unittest.TestCase):
    def test_sqlite_connect_error_yields_none_conn(self):
        with tempfile.TemporaryDirectory() as tmp:
            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(global_dir, exist_ok=True)
            _make_global_state(os.path.join(global_dir, "state.vscdb"))
            ws_root = os.path.join(tmp, "workspaceStorage")
            os.makedirs(ws_root, exist_ok=True)

            with patch(
                "services.workspace_db.sqlite3.connect",
                side_effect=sqlite3.OperationalError("simulated open failure"),
            ):
                with _open_global_db(ws_root) as (conn, path):
                    self.assertIsNone(conn)
                    self.assertTrue(path.endswith("state.vscdb"))


if __name__ == "__main__":
    unittest.main()
