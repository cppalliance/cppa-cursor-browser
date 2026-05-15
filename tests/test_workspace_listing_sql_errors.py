from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_listing import list_workspace_projects


def _seed_corrupt_global_db(parent: str) -> str:
    """Real globalStorage/state.vscdb with cursorDiskKV deliberately missing."""
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
        json.dump({"folder": "/tmp/proj"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    conn = sqlite3.connect(os.path.join(global_root, "state.vscdb"))
    conn.execute("CREATE TABLE other (x INTEGER)")
    conn.commit()
    conn.close()
    return ws_root


class TestListingCursorDiskKvCorruptDoesNotRaise(unittest.TestCase):
    def test_missing_cursordiskkv_returns_empty_list_not_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_corrupt_global_db(tmp)
            try:
                projects = list_workspace_projects(ws_root, rules=[])
            except sqlite3.Error:
                self.fail("sqlite3.Error should be caught inside _safe_fetchall")

            # No project rendered because no composer data resolved.  The
            # function returned a clean list (possibly empty, possibly just
            # CLI projects from real Cursor data if list_cli_projects finds any).
            self.assertIsInstance(projects, list)


if __name__ == "__main__":
    unittest.main()
