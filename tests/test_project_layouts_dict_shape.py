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


def _make_workspace_storage(parent: str, *, layout_as_dict: bool) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    target_folder = os.path.join(parent, "real-project")
    os.makedirs(target_folder, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
        json.dump({"folder": f"file://{target_folder}"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    layout_payload: object
    if layout_as_dict:
        layout_payload = {"rootPath": target_folder}
    else:
        layout_payload = json.dumps({"rootPath": target_folder})

    gdb = os.path.join(global_root, "state.vscdb")
    conn = sqlite3.connect(gdb)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            "composerData:cmp-1",
            json.dumps({
                "name": "Test composer",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": 1_715_000_500_000,
                "fullConversationHeadersOnly": [{"bubbleId": "b-1"}],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            "messageRequestContext:cmp-1:ctx-1",
            json.dumps({"projectLayouts": [layout_payload]}),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        ("bubbleId:cmp-1:b-1", json.dumps({"type": "user", "text": "hello"})),
    )
    conn.commit()
    conn.close()
    return ws_root


class TestProjectLayoutsDictShape(unittest.TestCase):
    def _assert_assigned_to_workspace(self, ws_root: str) -> None:
        projects = list_workspace_projects(ws_root, rules=[])
        ids = [p["id"] for p in projects]
        self.assertIn("ws-a", ids, msg=f"expected composer routed to ws-a, got {ids}")

    def test_string_shaped_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _make_workspace_storage(tmp, layout_as_dict=False)
            self._assert_assigned_to_workspace(ws_root)

    def test_dict_shaped_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _make_workspace_storage(tmp, layout_as_dict=True)
            self._assert_assigned_to_workspace(ws_root)


if __name__ == "__main__":
    unittest.main()
