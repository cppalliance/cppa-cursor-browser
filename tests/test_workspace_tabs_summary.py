"""Phase 2a regression — summary and single-tab endpoints must not full-scan bubbles.

Tests:
  - list_workspace_tab_summaries: no ``bubbleId:%`` global scan; returns
    id/title/timestamp/messageCount per tab; no ``bubbles`` field.
  - assemble_single_tab: only queries ``bubbleId:{composer_id}:%`` (scoped);
    returns correct single-tab structure matching the full-path shape.
  - assemble_single_tab returns 404 when composer doesn't belong to workspace.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_tabs import (
    assemble_single_tab,
    list_workspace_tab_summaries,
)

COMPOSER_ID = "composer-summary-test"
OTHER_COMPOSER_ID = "composer-other"
BUBBLE_ID_A = "bubble-a"
BUBBLE_ID_B = "bubble-b"
OTHER_BUBBLE_ID = "bubble-other"


def _make_fixture(base: str) -> str:
    """Create a minimal workspaceStorage + globalStorage; return ws_path."""
    ws_path = os.path.join(base, "workspaceStorage")
    os.makedirs(ws_path)
    global_dir = os.path.join(base, "globalStorage")
    os.makedirs(global_dir)

    db_path = os.path.join(global_dir, "state.vscdb")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")

    # Bubbles for COMPOSER_ID
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID_A}",
            json.dumps({"type": 1, "text": "user message", "createdAt": 1_739_200_000_000}),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{COMPOSER_ID}:{BUBBLE_ID_B}",
            json.dumps({"type": 2, "text": "ai response", "createdAt": 1_739_200_001_000}),
        ),
    )
    # Bubble belonging to a different composer — must NOT appear in scoped load
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{OTHER_COMPOSER_ID}:{OTHER_BUBBLE_ID}",
            json.dumps({"type": 1, "text": "other chat", "createdAt": 1_739_200_002_000}),
        ),
    )

    # composerData for COMPOSER_ID
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{COMPOSER_ID}",
            json.dumps({
                "name": "Summary Test Chat",
                "modelConfig": {"modelName": "claude-3-5-sonnet"},
                "fullConversationHeadersOnly": [
                    {"bubbleId": BUBBLE_ID_A, "type": 1},
                    {"bubbleId": BUBBLE_ID_B, "type": 2},
                ],
                "lastUpdatedAt": 1_739_300_000_000,
                "createdAt": 1_739_200_000_000,
            }),
        ),
    )
    # composerData for OTHER_COMPOSER_ID (should not bleed into single-tab response)
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{OTHER_COMPOSER_ID}",
            json.dumps({
                "name": "Other Chat",
                "modelConfig": {"modelName": "gpt-4o"},
                "fullConversationHeadersOnly": [
                    {"bubbleId": OTHER_BUBBLE_ID, "type": 1},
                ],
                "lastUpdatedAt": 1_739_300_000_000,
                "createdAt": 1_739_200_000_000,
            }),
        ),
    )
    conn.commit()
    conn.close()
    return ws_path


def _collect_queries(ws_path, fn):
    """Call fn(ws_path) while recording every SQL query; return (result, queries)."""
    import services.workspace_tabs as _ws_tabs_mod

    orig = _ws_tabs_mod.open_global_db
    executed: list[str] = []

    @contextmanager
    def _spy(workspace_path):
        with orig(workspace_path) as (conn, path):
            if conn is not None:
                conn.set_trace_callback(executed.append)
            yield conn, path

    with patch.object(_ws_tabs_mod, "open_global_db", _spy):
        result = fn(ws_path)
    return result, executed


class TestListWorkspaceTabSummaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws_path = _make_fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_global_bubble_scan(self):
        """list_workspace_tab_summaries must not issue a bubbleId:% LIKE query."""
        (payload, status), queries = _collect_queries(
            self.ws_path,
            lambda p: list_workspace_tab_summaries("global", p, rules=[], nocache=True),
        )
        self.assertTrue(queries, msg="expected SQL queries to be recorded")
        bubble_scans = [q for q in queries if "bubbleId:%" in q]
        self.assertEqual(
            bubble_scans,
            [],
            msg=f"list_workspace_tab_summaries ran a global bubble scan:\n{bubble_scans}",
        )

    def test_summary_tabs_have_no_bubbles_field(self):
        payload, status = list_workspace_tab_summaries("global", self.ws_path, rules=[])
        self.assertEqual(status, 200)
        tabs = payload.get("tabs", [])
        self.assertTrue(tabs, "Expected at least one summary tab")
        for tab in tabs:
            self.assertNotIn(
                "bubbles", tab,
                msg="Summary tabs must not contain a 'bubbles' field",
            )

    def test_summary_tab_fields(self):
        payload, _ = list_workspace_tab_summaries("global", self.ws_path, rules=[])
        tab = next((t for t in payload["tabs"] if t["id"] == COMPOSER_ID), None)
        self.assertIsNotNone(tab, msg="Expected COMPOSER_ID in summary tabs")
        self.assertEqual(tab["id"], COMPOSER_ID)
        self.assertIn("title", tab)
        self.assertIn("timestamp", tab)
        self.assertIn("messageCount", tab)
        self.assertEqual(tab["messageCount"], 2)

    def test_summary_metadata_modelsused(self):
        payload, _ = list_workspace_tab_summaries("global", self.ws_path, rules=[])
        tab = next((t for t in payload["tabs"] if t["id"] == COMPOSER_ID), None)
        self.assertIsNotNone(tab)
        meta = tab.get("metadata") or {}
        self.assertIn("modelsUsed", meta, msg="modelsUsed should appear in summary metadata")
        self.assertIn("claude-3-5-sonnet", meta["modelsUsed"])


class TestAssembleSingleTab(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws_path = _make_fixture(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scoped_bubble_query_only(self):
        """assemble_single_tab must query bubbleId:{composer_id}:% — not bubbleId:%."""
        (payload, status), queries = _collect_queries(
            self.ws_path,
            lambda p: assemble_single_tab("global", COMPOSER_ID, p, rules=[]),
        )
        self.assertTrue(queries, msg="expected SQL queries to be recorded")
        global_bubble_scans = [
            q for q in queries if "bubbleId:%" in q and f"bubbleId:{COMPOSER_ID}:%" not in q
        ]
        self.assertEqual(
            global_bubble_scans,
            [],
            msg=f"assemble_single_tab ran a non-scoped bubble scan:\n{global_bubble_scans}",
        )

    def test_scoped_mrc_load_no_invalid_workspaces(self):
        """Without invalid workspace folders, per-tab load must not full-scan MRC or composers."""
        (_, _), queries = _collect_queries(
            self.ws_path,
            lambda p: assemble_single_tab("global", COMPOSER_ID, p, rules=[]),
        )
        mrc_scans = [
            q for q in queries
            if "messageRequestContext:%" in q
            and f"messageRequestContext:{COMPOSER_ID}:%" not in q
        ]
        self.assertEqual(
            mrc_scans,
            [],
            msg=f"assemble_single_tab ran a global MRC scan:\n{mrc_scans}",
        )
        composer_scans = [q for q in queries if "composerData:%" in q]
        self.assertEqual(
            composer_scans,
            [],
            msg=f"assemble_single_tab scanned all composers for aliases:\n{composer_scans}",
        )

    def test_single_tab_structure(self):
        payload, status = assemble_single_tab("global", COMPOSER_ID, self.ws_path, rules=[])
        self.assertEqual(status, 200)
        self.assertIn("tab", payload)
        tab = payload["tab"]
        self.assertEqual(tab["id"], COMPOSER_ID)
        self.assertIn("title", tab)
        self.assertIn("timestamp", tab)
        self.assertIn("bubbles", tab)
        self.assertIn("codeBlockDiffs", tab)

    def test_single_tab_bubbles_only_from_this_composer(self):
        """Bubbles from OTHER_COMPOSER_ID must not appear in COMPOSER_ID's tab."""
        payload, status = assemble_single_tab("global", COMPOSER_ID, self.ws_path, rules=[])
        self.assertEqual(status, 200)
        texts = [b["text"] for b in payload["tab"]["bubbles"]]
        self.assertNotIn(
            "other chat", texts,
            msg="Bubble from a different composer leaked into the scoped tab",
        )
        # At least one bubble from COMPOSER_ID should be present
        self.assertTrue(
            any(t in ("user message", "ai response") for t in texts),
            msg=f"Expected bubbles from COMPOSER_ID, got: {texts}",
        )

    def test_single_tab_not_found_for_wrong_workspace(self):
        """assemble_single_tab returns 404 when composer is not in the workspace."""
        payload, status = assemble_single_tab(
            "nonexistent-ws-id", COMPOSER_ID, self.ws_path, rules=[]
        )
        self.assertEqual(status, 404)

    def test_single_tab_not_found_for_unknown_composer(self):
        payload, status = assemble_single_tab("global", "no-such-composer", self.ws_path, rules=[])
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
