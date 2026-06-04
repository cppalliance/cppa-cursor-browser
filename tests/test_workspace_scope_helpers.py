"""Tests for workspace-scope filtering and batched MRC layout loading."""

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

from services.workspace_db import (
    composer_mapped_to_other_workspace,
    fetch_composer_rows_by_ids,
    filter_composer_rows_for_workspace_scope,
    load_composer_rows_for_workspace_summary,
    load_project_layouts_for_composers,
    open_global_db,
)
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import list_workspace_tab_summaries

COMPOSER_A = "composer-ws-a"
COMPOSER_B = "composer-ws-b"
COMPOSER_EMPTY = "composer-empty-headers"
COMPOSER_GHOST = "composer-local-only"
BUBBLE_A = "bubble-a"
BUBBLE_B = "bubble-b"


def _write_workspace_json(parent: str, name: str, folder: str) -> None:
    ws_dir = os.path.join(parent, name)
    os.makedirs(ws_dir, exist_ok=True)
    wj = os.path.join(ws_dir, "workspace.json")
    with open(wj, "w", encoding="utf-8") as f:
        json.dump({"folder": folder}, f)


def _write_local_composers(ws_path: str, ws_id: str, composer_ids: list[str]) -> None:
    db_path = os.path.join(ws_path, ws_id, "state.vscdb")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
    payload = {
        "allComposers": [{"composerId": cid} for cid in composer_ids],
    }
    conn.execute(
        "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
        ("composer.composerData", json.dumps(payload)),
    )
    conn.commit()
    conn.close()


def _make_two_workspace_fixture(base: str) -> str:
    ws_path = os.path.join(base, "workspaceStorage")
    os.makedirs(ws_path)
    global_dir = os.path.join(base, "globalStorage")
    os.makedirs(global_dir)

    root_a = os.path.join(base, "project-a")
    root_b = os.path.join(base, "project-b")
    os.makedirs(root_a)
    os.makedirs(root_b)
    _write_workspace_json(ws_path, "ws-a", root_a)
    _write_workspace_json(ws_path, "ws-b", root_b)
    _write_local_composers(ws_path, "ws-a", [COMPOSER_A, COMPOSER_EMPTY, COMPOSER_GHOST])
    _write_local_composers(ws_path, "ws-b", [COMPOSER_B])

    db_path = os.path.join(global_dir, "state.vscdb")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")

    for cid, bubble_id, title in (
        (COMPOSER_A, BUBBLE_A, "Chat A"),
        (COMPOSER_B, BUBBLE_B, "Chat B"),
    ):
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{cid}:{bubble_id}",
                json.dumps({"type": 1, "text": title, "createdAt": 1_739_200_000_000}),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"composerData:{cid}",
                json.dumps({
                    "name": title,
                    "modelConfig": {"modelName": "gpt-4o"},
                    "fullConversationHeadersOnly": [{"bubbleId": bubble_id, "type": 1}],
                    "lastUpdatedAt": 1_739_300_000_000,
                    "createdAt": 1_739_200_000_000,
                }),
            ),
        )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{COMPOSER_EMPTY}",
            json.dumps({
                "name": "Empty headers",
                "fullConversationHeadersOnly": [],
                "lastUpdatedAt": 1_739_300_000_000,
                "createdAt": 1_739_200_000_000,
            }),
        ),
    )
    conn.commit()
    conn.close()
    return ws_path


class TestComposerScopeHelpers(unittest.TestCase):
    def test_composer_mapped_to_other_workspace(self) -> None:
        self.assertTrue(
            composer_mapped_to_other_workspace(
                "c1",
                {"c1": "ws-other"},
                {"ws-target"},
            )
        )
        self.assertFalse(
            composer_mapped_to_other_workspace(
                "c1",
                {"c1": "ws-target"},
                {"ws-target"},
            )
        )
        self.assertFalse(
            composer_mapped_to_other_workspace("c1", {}, {"ws-target"})
        )

    def test_filter_composer_rows_for_workspace_scope(self) -> None:
        rows = [
            {"key": "composerData:c-owned"},
            {"key": "composerData:c-other"},
            {"key": "composerData:c-unmapped"},
        ]
        mapping = {"c-owned": "ws-a", "c-other": "ws-b"}
        filtered, unmapped = filter_composer_rows_for_workspace_scope(
            rows, mapping, {"ws-a"},
        )
        keys = [r["key"] for r in filtered]
        self.assertIn("composerData:c-owned", keys)
        self.assertIn("composerData:c-unmapped", keys)
        self.assertNotIn("composerData:c-other", keys)
        self.assertEqual(unmapped, {"c-unmapped"})


class TestBatchedProjectLayouts(unittest.TestCase):
    def test_load_project_layouts_for_composers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(global_dir)
            db_path = os.path.join(global_dir, "state.vscdb")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    "messageRequestContext:cid-1:ctx",
                    json.dumps({"projectLayouts": [{"rootPath": "/roots/one"}]}),
                ),
            )
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (
                    "messageRequestContext:cid-2:ctx",
                    json.dumps({"projectLayouts": [{"rootPath": "/roots/two"}]}),
                ),
            )
            conn.commit()
            conn.close()

            ws_path = os.path.join(tmp, "workspaceStorage")
            os.makedirs(ws_path)
            with open_global_db(ws_path) as (gconn, _):
                assert gconn is not None
                layouts = load_project_layouts_for_composers(gconn, {"cid-1", "cid-2"})
            self.assertEqual(layouts["cid-1"], ["/roots/one"])
            self.assertEqual(layouts["cid-2"], ["/roots/two"])


class TestSummaryWorkspaceScope(unittest.TestCase):
    def test_summary_excludes_composer_mapped_to_other_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws_path = _make_two_workspace_fixture(tmp)
            payload, status = list_workspace_tab_summaries(
                "ws-b", ws_path, rules=[], nocache=True,
            )
            self.assertEqual(status, 200)
            ids = [t["id"] for t in payload["tabs"]]
            self.assertIn(COMPOSER_B, ids)
            self.assertNotIn(COMPOSER_A, ids)

    def test_summary_fetches_only_target_workspace_composers(self) -> None:
        """Sidebar for ws-b must not load composerData values for ws-a chats."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_path = _make_two_workspace_fixture(tmp)
            executed: list[str] = []

            import services.workspace_tabs as tabs_mod

            orig = tabs_mod.open_global_db

            from contextlib import contextmanager

            @contextmanager
            def _spy(path):
                with orig(path) as (conn, dbpath):
                    if conn is not None:
                        def _trace(sql: str) -> None:
                            executed.append(sql)

                        conn.set_trace_callback(_trace)
                    yield conn, dbpath

            from unittest.mock import patch

            with patch.object(tabs_mod, "open_global_db", _spy):
                list_workspace_tab_summaries("ws-b", ws_path, rules=[], nocache=True)

            value_scans = [
                sql for sql in executed
                if "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'" in sql
            ]
            self.assertEqual(
                value_scans,
                [],
                msg="summary path should not full-scan composerData values",
            )
            composer_a_fetches = [
                sql for sql in executed if COMPOSER_A in sql and "composerData" in sql
            ]
            self.assertEqual(
                composer_a_fetches,
                [],
                msg="summary for ws-b should not fetch ws-a composer payload",
            )


class TestProjectListSummaryCountAlignment(unittest.TestCase):
    def test_conversation_count_matches_tab_summary(self) -> None:
        """Project card conversationCount must match GET /tabs?summary=1 tab count."""
        with tempfile.TemporaryDirectory() as tmp:
            ws_path = _make_two_workspace_fixture(tmp)
            projects, _ = list_workspace_projects(ws_path, rules=[], nocache=True)
            project_a = next(p for p in projects if p["id"] == "ws-a")

            payload, status = list_workspace_tab_summaries(
                "ws-a", ws_path, rules=[], nocache=True,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                project_a["conversationCount"],
                len(payload["tabs"]),
                msg=(
                    f"project list count {project_a['conversationCount']} != "
                    f"summary tabs {len(payload['tabs'])}"
                ),
            )
            self.assertEqual(project_a["conversationCount"], 1)

    def test_null_composer_payload_skipped_silently(self) -> None:
        """Null global composerData payloads must not emit decode warnings."""
        null_composer = "composer-null-payload"
        with tempfile.TemporaryDirectory() as tmp:
            ws_path = os.path.join(tmp, "workspaceStorage")
            os.makedirs(ws_path)
            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(global_dir)
            root_a = os.path.join(tmp, "project-a")
            os.makedirs(root_a)
            _write_workspace_json(ws_path, "ws-a", root_a)
            _write_local_composers(ws_path, "ws-a", [null_composer])

            db_path = os.path.join(global_dir, "state.vscdb")
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
            conn.execute(
                "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                (f"composerData:{null_composer}", None),
            )
            conn.commit()
            conn.close()

            import logging
            from unittest.mock import patch

            with patch.object(
                logging.getLogger("services.workspace_tabs"),
                "warning",
            ) as warn_mock:
                payload, status = list_workspace_tab_summaries(
                    "ws-a", ws_path, rules=[], nocache=True,
                )
            self.assertEqual(status, 200)
            self.assertEqual(payload["tabs"], [])
            decode_warnings = [
                call for call in warn_mock.call_args_list
                if call.args and null_composer in str(call.args[0])
            ]
            self.assertEqual(decode_warnings, [])


if __name__ == "__main__":
    unittest.main()
