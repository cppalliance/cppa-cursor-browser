"""Regression test for issue #50: NULL bubble value crashes GET /tabs.

A cursorDiskKV row with a NULL value column previously caused
json.loads(None) → TypeError, which propagated as a 500 response.
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
        # Also insert a healthy bubble so we verify good rows still load.
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "bubbleId:composer-abc:bubble-ok",
                json.dumps({"type": 1, "text": "hello", "createdAt": 0}),
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

        # Should complete without TypeError (or any exception).
        try:
            payload, status = assemble_workspace_tabs(
                workspace_id="global",
                workspace_path=self.workspace_path,
                rules=[],
            )
        except TypeError as exc:
            self.fail(f"NULL bubble row raised TypeError: {exc}")

        # The endpoint must return 200 (or 404 only if global storage is absent —
        # our setup provides it, so 200 is expected).
        self.assertEqual(status, 200)

    def test_healthy_bubbles_still_load_when_null_row_present(self):
        """Healthy bubble rows in the same table are not dropped by the None-guard."""
        from services.workspace_tabs import assemble_workspace_tabs

        payload, status = assemble_workspace_tabs(
            workspace_id="global",
            workspace_path=self.workspace_path,
            rules=[],
        )
        self.assertEqual(status, 200)
        # The response payload must be a dict (no 500 error shape).
        self.assertIsInstance(payload, dict)
        self.assertIn("tabs", payload)


if __name__ == "__main__":
    unittest.main()
