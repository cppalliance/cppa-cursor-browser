"""Phase 1 regression — GET /api/workspaces must not scan global bubbleId rows.

Verifies that list_workspace_projects() never issues a
``SELECT … LIKE 'bubbleId:%'`` query against global storage and still returns
the correct project card shape.
"""

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

import services.workspace_listing as _ws_listing_mod
from services.workspace_listing import list_workspace_projects

COMPOSER_ID = "composer-list-perf"
BUBBLE_ID = "bubble-list-perf"


def _make_fixture(base: str) -> str:
    """Create a minimal workspaceStorage with global KV rows; return ws_path."""
    ws_path = os.path.join(base, "workspaceStorage")
    os.makedirs(ws_path)
    global_dir = os.path.join(base, "globalStorage")
    os.makedirs(global_dir)

    db_path = os.path.join(global_dir, "state.vscdb")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")

    # A bubble row that must NOT be read on the list path.
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID}",
            json.dumps({"type": 1, "text": "hello", "createdAt": 1_739_200_000_000}),
        ),
    )
    # A composerData row with a non-empty fullConversationHeadersOnly — the
    # summary path uses this to decide whether to include the conversation.
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{COMPOSER_ID}",
            json.dumps({
                "name": "Perf Test Chat",
                "modelConfig": {"modelName": "gpt-4o"},
                "fullConversationHeadersOnly": [{"bubbleId": BUBBLE_ID, "type": 1}],
                "lastUpdatedAt": 1_739_300_000_000,
                "createdAt": 1_739_200_000_000,
            }),
        ),
    )
    conn.commit()
    conn.close()
    return ws_path


def _make_fixture_with_invalid_workspace(base: str) -> str:
    ws_path = _make_fixture(base)
    invalid_dir = os.path.join(ws_path, "invalid-ws")
    os.makedirs(invalid_dir)
    with open(os.path.join(invalid_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folders": []}, f)
    return ws_path


class TestListWorkspaceProjectsNoBubbleScan(unittest.TestCase):
    """list_workspace_projects must not query bubbleId rows from global storage."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_global_bubble_id_query(self):
        ws_path = _make_fixture(self.tmp.name)

        executed_queries: list[str] = []

        original_open_global_db = _ws_listing_mod.open_global_db

        from contextlib import contextmanager

        @contextmanager
        def _spying_open_global_db(workspace_path):
            with original_open_global_db(workspace_path) as (conn, path):
                if conn is not None:
                    conn.set_trace_callback(executed_queries.append)
                yield conn, path

        with patch.object(_ws_listing_mod, "open_global_db", _spying_open_global_db):
            list_workspace_projects(ws_path, rules=[], nocache=True)

        self.assertTrue(executed_queries, msg="expected SQL queries to be recorded")
        bubble_scans = [q for q in executed_queries if "bubbleId:%" in q]
        self.assertEqual(
            bubble_scans,
            [],
            msg=f"list_workspace_projects issued a global bubbleId:% scan:\n{bubble_scans}",
        )

    def test_projects_still_returned_without_bubble_scan(self):
        """Conversations present in fullConversationHeadersOnly appear even without bubbles."""
        ws_path = _make_fixture(self.tmp.name)

        projects, warnings = list_workspace_projects(ws_path, rules=[])

        # The fixture has no workspace entries (no state.vscdb / workspace.json),
        # so the conversation lands in the "global" bucket.
        global_bucket = next((p for p in projects if p["id"] == "global"), None)
        self.assertIsNotNone(
            global_bucket,
            msg=f"Expected a 'global' project bucket; got: {[p['id'] for p in projects]}",
        )
        self.assertGreaterEqual(global_bucket["conversationCount"], 1)

    def test_output_shape_preserved(self):
        """Project cards still carry id, name, conversationCount, lastModified."""
        ws_path = _make_fixture(self.tmp.name)
        projects, _ = list_workspace_projects(ws_path, rules=[])
        for p in projects:
            self.assertIn("id", p)
            self.assertIn("name", p)
            self.assertIn("conversationCount", p)
            self.assertIn("lastModified", p)

    def test_nocache_bypasses_alias_disk_cache(self):
        ws_path = _make_fixture_with_invalid_workspace(self.tmp.name)
        with patch(
            "services.summary_cache.get_cached_invalid_workspace_aliases",
        ) as mock_get:
            mock_get.return_value = {"invalid-ws": "global"}
            list_workspace_projects(ws_path, rules=[], nocache=True)
        mock_get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
