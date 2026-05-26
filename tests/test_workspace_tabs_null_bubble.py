"""Regression test for issue #50: NULL bubble value crashes GET /tabs.

A cursorDiskKV row with a NULL value column previously caused
json.loads(None) -> TypeError, which propagated as a 500 response.
Bubble rows with NULL or invalid JSON values are skipped in
``services/workspace_tabs.py`` without raising.
"""

import json
import os
import sqlite3
import tempfile
import unittest

from services.workspace_tabs import assemble_workspace_tabs

# cursorDiskKV keys use typed prefixes; tabs[].id is the bare suffix only
# (assemble_workspace_tabs: composer_id = row["key"].split(":")[1]).
COMPOSER_ID = "composer-abc"
COMPOSER_KV_KEY = f"composerData:{COMPOSER_ID}"


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
            (f"bubbleId:{COMPOSER_ID}:bubble-null", None),  # NULL value — the crash case
        )
        # Healthy bubble that should surface in the assembled tab.
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{COMPOSER_ID}:bubble-ok",
                json.dumps({"type": 1, "text": "hello world", "createdAt": 1739200000000}),
            ),
        )
        # Composer referencing the healthy bubble — required for a tab to be built.
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                COMPOSER_KV_KEY,
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
        try:
            with self.assertLogs("services.workspace_tabs", level="WARNING") as cm:
                _payload, status = assemble_workspace_tabs(
                    workspace_id="global",
                    workspace_path=self.workspace_path,
                    rules=[],
                )
        except TypeError as exc:
            self.fail(f"NULL bubble row raised TypeError: {exc}")

        self.assertEqual(status, 200, "NULL bubble row must not turn tabs load into an error response")
        messages = [r.getMessage() for r in cm.records]
        self.assertTrue(
            any("NULL value" in m and "bubble-null" in m for m in messages),
            f"expected NULL-value warning for bubble-null row, got: {messages}",
        )

    def test_healthy_bubbles_still_load_when_null_row_present(self):
        """The healthy bubble surfaces in a tab even when a NULL row is present."""
        payload, status = assemble_workspace_tabs(
            workspace_id="global",
            workspace_path=self.workspace_path,
            rules=[],
        )
        self.assertEqual(status, 200, "tabs endpoint must succeed when only the null bubble row is bad")
        self.assertIsInstance(payload, dict, "tabs response must be a JSON object envelope")
        tabs = payload.get("tabs", [])
        self.assertEqual(len(tabs), 1, f"Expected exactly one tab for {COMPOSER_ID}")

        tab = tabs[0]
        # GET /tabs and workspace.html ?tab= use bare composer id, not the KV key.
        self.assertEqual(tab["id"], COMPOSER_ID, "tab id must be bare composer id (KV key suffix only)")
        self.assertNotEqual(tab["id"], COMPOSER_KV_KEY, "tab id must not include composerData: prefix")
        self.assertEqual(tab["title"], "Test Chat", "composer name from seeded cursorDiskKV row")
        self.assertIn("bubbles", tab, "tab payload must include bubbles for the conversation view")
        self.assertIn("codeBlockDiffs", tab, "tab payload must include codeBlockDiffs field (may be empty)")

        bubbles = tab["bubbles"]
        self.assertEqual(len(bubbles), 1, "Expected exactly one bubble (null row skipped)")
        bubble = bubbles[0]
        self.assertEqual(bubble["type"], "user", "header type 1 maps to user bubble")
        self.assertEqual(bubble["text"], "hello world", "healthy bubble text must surface in the tab")
        self.assertIn("timestamp", bubble, "bubble must carry a timestamp for ordering/display")


if __name__ == "__main__":
    unittest.main()
