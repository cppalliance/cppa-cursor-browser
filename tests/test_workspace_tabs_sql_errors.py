from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_tabs import assemble_workspace_tabs


def _seed_workspace_with_corrupt_global_db(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)
    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
        json.dump({"folder": "/tmp/proj"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    # Global DB exists but cursorDiskKV is missing → every LIKE query
    # in assemble_workspace_tabs raises sqlite3.OperationalError.
    conn = sqlite3.connect(os.path.join(global_root, "state.vscdb"))
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    return ws_root


class TestCursorDiskKvCorruptDoesNot500(unittest.TestCase):
    def test_missing_cursordiskkv_returns_empty_tabs_not_error(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_workspace_with_corrupt_global_db(tmp)
            with app.test_request_context("/api/workspaces/global/tabs"):
                try:
                    payload, status = assemble_workspace_tabs("global", ws_root, rules=[])
                except sqlite3.Error:
                    self.fail("sqlite3.Error should be caught, not propagated")

        self.assertEqual(status, 200)
        self.assertEqual(payload, {"tabs": []})


if __name__ == "__main__":
    unittest.main()
