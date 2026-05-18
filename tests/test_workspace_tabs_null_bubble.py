"""Regression test for issue #50: NULL bubble value crashes GET /tabs.

A cursorDiskKV row with a NULL value column previously caused
json.loads(None) -> TypeError, which propagated as a 500 response.
The fix adds an explicit None-guard before json.loads in the bubble
loading loop of services/workspace_tabs.py.
"""

import json
import os
import sqlite3
import tempfile
import unittest


class TestNullBubbleValueDoesNotCrashTabs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = self.tmp.name

        # Minimal workspaceStorage layout expected by assemble_workspace_tabs.
        ws_dir = os.path.join(base, "workspaceStorage")
        os.makedirs(ws_dir)

        # Global storage with a cursorDiskKV table containing a NULL-value bubble row.
        global_dir = os.path.join(base, "globalStorage")
        os.makedirs(global_dir)
        db_path = os.path.join(global_dir, "state.vscdb")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            ("bubbleId:composer-abc:bubble-null", None),  # NULL value — the crash case
        )
        # Healthy bubble that should surface in the assembled tab.
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "bubbleId:composer-abc:bubble-ok",
                json.dumps({"type": 1, "text": "hello world", "createdAt": 1739200000000}),
            ),
        )
        # Composer referencing the healthy bubble — required for a tab to be built.
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "composerData:composer-abc",
                json.dumps({
                    "name": "Test Chat",
                    "modelConfig": {"modelName": "gpt-4o"},
                    "fullConversationHeadersOnly": [{"bubbleId": "bubble-ok", "type": 1}],
                    "lastUpdatedAt": 1739300000000,
                    "createdAt": 1739200000000,
                }),
            ),
        )
        conn.commit()
        conn.close()

        self.workspace_path = ws_dir

    def tearDown(self):
        self.tmp.cleanup()

    def test_null_bubble_row_is_skipped_without_exception(self):
        """assemble_workspace_tabs must not raise when a bubble row has NULL value."""
        from services.workspace_tabs import assemble_workspace_tabs

        try:
            _payload, status = assemble_workspace_tabs(
                workspace_id="global",
                workspace_path=self.workspace_path,
                rules=[],
            )
        except TypeError as exc:
            self.fail(f"NULL bubble row raised TypeError: {exc}")

        self.assertEqual(status, 200)

    def test_healthy_bubbles_still_load_when_null_row_present(self):
        """The healthy bubble surfaces in a tab even when a NULL row is present."""
        from services.workspace_tabs import assemble_workspace_tabs

        payload, status = assemble_workspace_tabs(
            workspace_id="global",
            workspace_path=self.workspace_path,
            rules=[],
        )
        self.assertEqual(status, 200)
        self.assertIsInstance(payload, dict)
        tabs = payload.get("tabs", [])
        self.assertEqual(len(tabs), 1, "Expected exactly one tab for composer-abc")

        tab = tabs[0]
        self.assertEqual(tab["id"], "composer-abc")
        self.assertEqual(tab["title"], "Test Chat")
        self.assertIn("bubbles", tab)
        self.assertIn("codeBlockDiffs", tab)

        bubbles = tab["bubbles"]
        self.assertEqual(len(bubbles), 1, "Expected exactly one bubble (null row skipped)")
        bubble = bubbles[0]
        self.assertEqual(bubble["type"], "user")
        self.assertEqual(bubble["text"], "hello world")
        self.assertIn("timestamp", bubble)


if __name__ == "__main__":
    unittest.main()
