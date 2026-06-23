"""Tests for structured logging at model parse sites (issue #66)."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import assemble_workspace_tabs


def _seed_listing_with_drifted_composer(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    target_folder = os.path.join(parent, "real-project")
    os.makedirs(target_folder, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": f"file://{target_folder}"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    conn = sqlite3.connect(os.path.join(global_root, "state.vscdb"))
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:cmp-drift",
            json.dumps({
                "name": "Drifted composer",
                "fullConversationHeadersOnly": [{"bubbleId": "b-1"}],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:cmp-drift:b-1", json.dumps({"type": "user", "text": "hello"})),
    )
    conn.commit()
    conn.close()
    return ws_root


def _seed_tabs_with_drifted_bubble(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    proj_dir = os.path.join(ws_dir, "proj")
    os.makedirs(proj_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": f"file://{proj_dir}"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    conn = sqlite3.connect(os.path.join(global_root, "state.vscdb"))
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:cmp-ok",
            json.dumps({
                "name": "Good tab",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": 1_715_000_500_000,
                "fullConversationHeadersOnly": [
                    {"bubbleId": "b-bad", "type": 1},
                    {"bubbleId": "b-good", "type": 1},
                ],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:cmp-ok:b-bad", json.dumps("not-a-dict")),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:cmp-ok:b-good", json.dumps({"text": "hello"})),
    )
    conn.commit()
    conn.close()
    return ws_root


class TestParseFailureLogging(unittest.TestCase):
    def test_listing_logs_composer_schema_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_listing_with_drifted_composer(tmp)
            with self.assertLogs("services.workspace_composer_scan", level="WARNING") as cm:
                list_workspace_projects(ws_root, rules=[])

        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("Composer" in m and "cmp-drift" in m for m in messages),
            f"expected Composer parse warning for cmp-drift, got: {messages}",
        )

    def test_workspace_tabs_logs_bubble_json_decode_failure(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_tabs_with_drifted_bubble(tmp)
            global_db = os.path.join(tmp, "globalStorage", "state.vscdb")
            with closing(sqlite3.connect(global_db)) as conn:
                conn.execute(
                    "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                    ("bubbleId:cmp-ok:b-json", "{not valid json"),
                )
                conn.commit()
            with self.assertLogs("services.workspace_db", level="WARNING") as cm:
                with app.test_request_context("/api/workspaces/global/tabs"):
                    _payload, _status = assemble_workspace_tabs("global", ws_root, rules=[])

        self.assertEqual(_status, 200)
        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("decode Bubble" in m and "b-json" in m for m in messages),
            f"expected JSON decode warning for b-json, got: {messages}",
        )

    def test_workspace_tabs_logs_composer_json_decode_failure(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_tabs_with_drifted_bubble(tmp)
            global_db = os.path.join(tmp, "globalStorage", "state.vscdb")
            bad_composer_value = (
                '{"fullConversationHeadersOnly": [{"bubbleId": "b1"}], "createdAt":'
            )
            with closing(sqlite3.connect(global_db)) as conn:
                conn.execute(
                    "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                    ("composerData:cmp-json", bad_composer_value),
                )
                conn.commit()
            with self.assertLogs("services.workspace_tabs", level="WARNING") as cm:
                with app.test_request_context("/api/workspaces/global/tabs"):
                    _payload, _status = assemble_workspace_tabs("global", ws_root, rules=[])

        self.assertEqual(_status, 200)
        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("decode Composer" in m and "cmp-json" in m for m in messages),
            f"expected JSON decode warning for cmp-json, got: {messages}",
        )

    def test_workspace_tabs_logs_bubble_schema_drift(self) -> None:
        from flask import Flask

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_tabs_with_drifted_bubble(tmp)
            with self.assertLogs("services.workspace_db", level="WARNING") as cm:
                with app.test_request_context("/api/workspaces/global/tabs"):
                    payload, status = assemble_workspace_tabs("global", ws_root, rules=[])

        self.assertEqual(status, 200)
        self.assertIn("cmp-ok", [t["id"] for t in payload.get("tabs", [])])
        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("Bubble" in m and "b-bad" in m for m in messages),
            f"expected Bubble parse warning for b-bad, got: {messages}",
        )


if __name__ == "__main__":
    unittest.main()
