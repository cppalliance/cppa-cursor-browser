from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_tabs import assemble_workspace_tabs


def _seed_workspace(parent: str) -> str:
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
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:cmp-1",
            json.dumps({
                "name": "Tab with bad header",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": 1_715_000_500_000,
                "fullConversationHeadersOnly": [
                    None,                              # malformed: non-dict
                    "not a dict either",               # malformed: string
                    {"bubbleId": "b-good", "type": 1}, # healthy
                ],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:cmp-1:b-good", json.dumps({"text": "hello"})),
    )
    conn.commit()
    conn.close()
    return ws_root


class TestNonDictHeaderDoesNotDropComposer(unittest.TestCase):
    def test_malformed_headers_skipped_composer_still_rendered(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_workspace(tmp)
            with app.test_request_context("/api/workspaces/global/tabs"):
                payload, status = assemble_workspace_tabs("global", ws_root, rules=[])

        self.assertEqual(status, 200)
        ids = [t["id"] for t in payload.get("tabs", [])]
        self.assertIn("cmp-1", ids)


def _seed_workspace_with_diff(parent: str, *, diff_timestamp: int | None) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)
    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w") as f:
        json.dump({"folder": "/tmp/proj"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    bubble_ts = 1_715_000_500_000
    conn = sqlite3.connect(os.path.join(global_root, "state.vscdb"))
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:cmp-d",
            json.dumps({
                "name": "Tab with diff",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": bubble_ts,
                "fullConversationHeadersOnly": [{"bubbleId": "b1", "type": 1}],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:cmp-d:b1", json.dumps({"text": "user msg", "createdAt": bubble_ts})),
    )
    diff_payload: dict = {"filePath": "src/main.py", "command": "format"}
    if diff_timestamp is not None:
        diff_payload["timestamp"] = diff_timestamp
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("codeBlockDiff:cmp-d:diff1", json.dumps(diff_payload)),
    )
    conn.commit()
    conn.close()
    return ws_root


class TestSyntheticBubbleTimestampNotNow(unittest.TestCase):
    def test_synthetic_uses_diff_timestamp_when_present(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        diff_ts = 1_715_000_700_000

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_workspace_with_diff(tmp, diff_timestamp=diff_ts)
            with app.test_request_context("/api/workspaces/global/tabs"):
                payload, _ = assemble_workspace_tabs("global", ws_root, rules=[])

        tab = next((t for t in payload["tabs"] if t["id"] == "cmp-d"), None)
        self.assertIsNotNone(tab)
        synthetic = next(b for b in tab["bubbles"] if b["text"].startswith("**Tool Action:**"))
        self.assertEqual(synthetic["timestamp"], diff_ts)

    def test_synthetic_falls_back_to_max_bubble_timestamp(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_workspace_with_diff(tmp, diff_timestamp=None)
            with app.test_request_context("/api/workspaces/global/tabs"):
                payload, _ = assemble_workspace_tabs("global", ws_root, rules=[])

        tab = next(t for t in payload["tabs"] if t["id"] == "cmp-d")
        synthetic = next(b for b in tab["bubbles"] if b["text"].startswith("**Tool Action:**"))
        # synthetic must NOT use datetime.now() — it should be at or before
        # the latest real bubble (which is the 1_715_000_500_000 we seeded,
        # far in the past relative to today's time).
        now_ms = int(__import__("datetime").datetime.now().timestamp() * 1000)
        self.assertLess(synthetic["timestamp"], now_ms - 10_000_000_000)


def _seed_workspace_with_tool_former(parent: str) -> str:
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
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "composerData:cmp-t",
            json.dumps({
                "name": "Tab with toolFormerData",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": 1_715_000_500_000,
                "fullConversationHeadersOnly": [{"bubbleId": "b-t", "type": 2}],
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:cmp-t:b-t",
            json.dumps({
                "text": "assistant message",
                "createdAt": 1_715_000_400_000,
                "toolFormerData": {"name": "tool-x"},
            }),
        ),
    )
    conn.commit()
    conn.close()
    return ws_root


class TestParseToolCallNonDictReturn(unittest.TestCase):
    def test_non_dict_parse_result_does_not_drop_composer(self) -> None:
        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []

        with tempfile.TemporaryDirectory() as tmp:
            ws_root = _seed_workspace_with_tool_former(tmp)
            # Force _parse_tool_call to return None — the previous code
            # would have stored ``tool_calls = [None]`` and crashed in the
            # display-text fallback with ``NoneType.get``.
            with patch("services.workspace_tabs._parse_tool_call", return_value=None):
                with app.test_request_context("/api/workspaces/global/tabs"):
                    payload, status = assemble_workspace_tabs("global", ws_root, rules=[])

        self.assertEqual(status, 200)
        ids = [t["id"] for t in payload.get("tabs", [])]
        self.assertIn("cmp-t", ids)


if __name__ == "__main__":
    unittest.main()
