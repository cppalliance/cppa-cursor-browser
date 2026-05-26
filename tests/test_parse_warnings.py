"""Incomplete-result signaling when parse failures occur (issue #67)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from app import create_app
from models.parse_warnings import ParseWarningCollector
from services.workspace_listing import list_workspace_projects
from services.workspace_tabs import assemble_workspace_tabs


def _seed_clean_workspace(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-clean")
    os.makedirs(ws_dir, exist_ok=True)
    target = os.path.join(parent, "proj")
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": f"file://{target}"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    with sqlite3.connect(os.path.join(global_root, "state.vscdb")) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            (
                "composerData:cmp-ok",
                json.dumps({
                    "name": "Good chat",
                    "createdAt": 1_715_000_000_000,
                    "lastUpdatedAt": 1_715_000_500_000,
                    "fullConversationHeadersOnly": [{"bubbleId": "b-1"}],
                }),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            ("bubbleId:cmp-ok:b-1", json.dumps({"text": "hello", "type": 1})),
        )
        conn.commit()
    return ws_root


def _seed_listing_with_drift(parent: str) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, "ws-a")
    os.makedirs(ws_dir, exist_ok=True)
    target = os.path.join(parent, "real-project")
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": f"file://{target}"}, f)
    sqlite3.connect(os.path.join(ws_dir, "state.vscdb")).close()

    with sqlite3.connect(os.path.join(global_root, "state.vscdb")) as conn:
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
    return ws_root


class TestParseWarningCollector(unittest.TestCase):
    def test_clean_collector_emits_no_warnings(self) -> None:
        collector = ParseWarningCollector()
        self.assertFalse(collector.has_warnings)
        self.assertEqual(collector.to_api_list(), [])

    def test_collector_emits_structured_warnings(self) -> None:
        collector = ParseWarningCollector()
        collector.record_composer_skipped(2)
        collector.record_bubble_skipped(1)
        warnings = collector.to_api_list()
        self.assertEqual(len(warnings), 2)
        self.assertEqual(warnings[0]["type"], "parse_error")
        self.assertEqual(warnings[0]["count"], 2)
        self.assertIn("conversation", warnings[0]["detail"])
        self.assertEqual(warnings[1]["count"], 1)
        self.assertIn("message", warnings[1]["detail"])


class TestServiceParseWarnings(unittest.TestCase):
    def test_listing_clean_results_have_no_warnings(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = _seed_clean_workspace(tmp)
            with patch("services.workspace_listing.list_cli_projects", return_value=[]):
                projects, warnings = list_workspace_projects(ws_root, rules=[])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertTrue(any(p.get("conversationCount", 0) > 0 for p in projects))
        self.assertEqual(warnings, [])

    def test_listing_schema_drift_emits_warning_without_project(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = _seed_listing_with_drift(tmp)
            with patch("services.workspace_listing.list_cli_projects", return_value=[]):
                projects, warnings = list_workspace_projects(ws_root, rules=[])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["type"], "parse_error")
        self.assertEqual(warnings[0]["count"], 1)
        self.assertFalse(any(p.get("conversationCount", 0) > 0 for p in projects))

    def test_listing_warnings_when_decode_fails(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = _seed_clean_workspace(tmp)
            global_db = os.path.join(tmp, "globalStorage", "state.vscdb")
            with sqlite3.connect(global_db) as conn:
                conn.execute(
                    "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                    ("composerData:cmp-bad-json", '{"broken":1'),
                )
                conn.commit()
            with patch("services.workspace_listing.list_cli_projects", return_value=[]):
                _projects, warnings = list_workspace_projects(ws_root, rules=[])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["type"], "parse_error")
        self.assertEqual(warnings[0]["count"], 1)

    def test_tabs_clean_results_have_no_warnings(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_dir = os.path.join(tmp, "workspaceStorage")
            global_dir = os.path.join(tmp, "globalStorage")
            os.makedirs(ws_dir)
            os.makedirs(global_dir)
            db_path = os.path.join(global_dir, "state.vscdb")
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
                conn.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (
                        "composerData:cmp-ok",
                        json.dumps({
                            "name": "Clean tab",
                            "createdAt": 1_715_000_000_000,
                            "fullConversationHeadersOnly": [{"bubbleId": "b-ok", "type": 1}],
                        }),
                    ),
                )
                conn.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("bubbleId:cmp-ok:b-ok", json.dumps({"text": "hello", "type": 1})),
                )
                conn.commit()
            payload, status = assemble_workspace_tabs("global", ws_dir, rules=[])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(status, 200)
        self.assertNotIn("warnings", payload)
        self.assertEqual(len(payload.get("tabs", [])), 1)

    def test_tabs_reports_bubble_and_composer_parse_failures(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = os.path.join(tmp, "workspaceStorage")
            global_root = os.path.join(tmp, "globalStorage")
            os.makedirs(ws_root)
            os.makedirs(global_root)
            with sqlite3.connect(os.path.join(global_root, "state.vscdb")) as conn:
                conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
                conn.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    (
                        "composerData:cmp-ok",
                        json.dumps({
                            "name": "Tab",
                            "createdAt": 1_715_000_000_000,
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
                    ("bubbleId:cmp-ok:b-good", json.dumps({"text": "hi"})),
                )
                conn.execute(
                    "INSERT INTO cursorDiskKV VALUES (?, ?)",
                    ("composerData:cmp-bad", "{broken"),
                )
                conn.commit()
            payload, status = assemble_workspace_tabs("global", ws_root, rules=[])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(status, 200)
        self.assertIn("warnings", payload)
        types = {w["type"] for w in payload["warnings"]}
        self.assertEqual(types, {"parse_error"})
        counts = {w["count"] for w in payload["warnings"]}
        self.assertIn(1, counts)
        self.assertTrue(any(w["count"] >= 1 for w in payload["warnings"]))


class TestApiParseWarnings(unittest.TestCase):
    def setUp(self) -> None:
        self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_workspaces_api_object_when_clean(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = _seed_clean_workspace(tmp)
            with patch("api.workspaces.resolve_workspace_path", return_value=ws_root), \
                 patch("services.workspace_listing.list_cli_projects", return_value=[]):
                res = self.client.get("/api/workspaces")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("projects", data)
        self.assertNotIn("warnings", data)

    def test_workspaces_api_object_when_warnings(self) -> None:
        tmp = tempfile.mkdtemp()
        try:
            ws_root = _seed_clean_workspace(tmp)
            global_db = os.path.join(tmp, "globalStorage", "state.vscdb")
            with sqlite3.connect(global_db) as conn:
                conn.execute(
                    "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
                    ("composerData:cmp-bad-json", '{"broken":1'),
                )
                conn.commit()
            with patch("api.workspaces.resolve_workspace_path", return_value=ws_root), \
                 patch("services.workspace_listing.list_cli_projects", return_value=[]):
                res = self.client.get("/api/workspaces")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIsInstance(data, dict)
        self.assertIn("projects", data)
        self.assertIn("warnings", data)
        self.assertEqual(data["warnings"][0]["type"], "parse_error")


if __name__ == "__main__":
    unittest.main()
