"""
Integration tests for exclusion filtering in /api/search output.

Run:
  python -m unittest tests.test_search_exclusion_filtering -v
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

# Ensure project root is importable when running directly.
_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from api.search import bp as search_bp
from utils.exclusion_rules import load_rules


class TestSearchExclusionFiltering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base_dir = self._tmp.name
        self.workspace_path = os.path.join(self.base_dir, "workspaceStorage")
        self.global_storage_path = os.path.join(self.base_dir, "globalStorage")
        os.makedirs(self.workspace_path, exist_ok=True)
        os.makedirs(self.global_storage_path, exist_ok=True)

        self.ws_kwd_id = "workspace-kwd"
        self.ws_kwd_dir = os.path.join(self.workspace_path, self.ws_kwd_id)
        os.makedirs(self.ws_kwd_dir, exist_ok=True)
        with open(os.path.join(self.ws_kwd_dir, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "file:///d%3A/_hjb_cpp/gigs/options/kwds"}, f)

        self.ws_public_id = "workspace-public"
        self.ws_public_dir = os.path.join(self.workspace_path, self.ws_public_id)
        os.makedirs(self.ws_public_dir, exist_ok=True)
        with open(os.path.join(self.ws_public_dir, "workspace.json"), "w", encoding="utf-8") as f:
            json.dump({"folder": "file:///d%3A/_hjb_cpp/gigs/options/public-project"}, f)

        self._build_workspace_dbs()
        self._build_global_db()

        self._old_workspace_path = os.environ.get("WORKSPACE_PATH")
        os.environ["WORKSPACE_PATH"] = self.workspace_path

        # Point CLI chats path at an empty temp dir so real sessions don't leak in.
        self._cli_chats_path = os.path.join(self.base_dir, "cli_chats")
        os.makedirs(self._cli_chats_path, exist_ok=True)
        self._old_cli_chats_path = os.environ.get("CLI_CHATS_PATH")
        os.environ["CLI_CHATS_PATH"] = self._cli_chats_path

        app = Flask(__name__)
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        app.register_blueprint(search_bp)
        self.client = app.test_client()
        self.app = app

    def tearDown(self):
        if self._old_workspace_path is None:
            os.environ.pop("WORKSPACE_PATH", None)
        else:
            os.environ["WORKSPACE_PATH"] = self._old_workspace_path
        if self._old_cli_chats_path is None:
            os.environ.pop("CLI_CHATS_PATH", None)
        else:
            os.environ["CLI_CHATS_PATH"] = self._old_cli_chats_path
        self._tmp.cleanup()

    def _build_workspace_dbs(self):
        db_path = os.path.join(self.ws_kwd_dir, "state.vscdb")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")

        # Used by /api/search to map composer IDs to workspace IDs.
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps(
                    {
                        "allComposers": [
                            {"composerId": "cmp-kwd"},
                        ]
                    }
                ),
            ),
        )

        # Legacy chat storage (fallback path in /api/search).
        legacy_chat = {
            "tabs": [
                {
                    "tabId": "tab-kwd",
                    "chatTitle": "kwd Archive Thread",
                    "lastSendTime": "2026-02-11T15:00:00Z",
                    "metadata": {"model": "gpt-4.1"},
                    "bubbles": [
                        {"type": "user", "text": "Where is kwd 2026-001?"},
                        {"type": "assistant", "text": "kwd metadata is attached."},
                    ],
                }
            ]
        }
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            ("workbench.panel.aichat.view.aichat.chatdata", json.dumps(legacy_chat)),
        )

        conn.commit()
        conn.close()

        db_path_public = os.path.join(self.ws_public_dir, "state.vscdb")
        conn_public = sqlite3.connect(db_path_public)
        conn_public.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
        conn_public.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"allComposers": [{"composerId": "cmp-roadmap"}]}),
            ),
        )
        conn_public.commit()
        conn_public.close()

    def _build_global_db(self):
        db_path = os.path.join(self.global_storage_path, "state.vscdb")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")

        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "bubbleId:cmp-kwd:b-kwd-1",
                json.dumps({"type": "user", "text": "Please extract kwd PDF metadata."}),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "bubbleId:cmp-kwd:b-kwd-2",
                json.dumps({"type": "assistant", "text": "kwd details parsed successfully."}),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "bubbleId:cmp-roadmap:b-roadmap-1",
                json.dumps({"type": "user", "text": "Create a roadmap for Q3 delivery."}),
            ),
        )

        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "composerData:cmp-kwd",
                json.dumps(
                    {
                        "name": "kwd PDF metadata extraction",
                        "modelConfig": {"modelName": "gpt-4.1"},
                        "fullConversationHeadersOnly": [
                            {"bubbleId": "b-kwd-1"},
                            {"bubbleId": "b-kwd-2"},
                        ],
                        "lastUpdatedAt": 1739270000000,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                "composerData:cmp-roadmap",
                json.dumps(
                    {
                        "name": "Roadmap planning notes",
                        "modelConfig": {"modelName": "claude-3.5-sonnet"},
                        "fullConversationHeadersOnly": [
                            {"bubbleId": "b-roadmap-1"},
                        ],
                        "lastUpdatedAt": 1739271000000,
                    }
                ),
            ),
        )

        conn.commit()
        conn.close()

    def _set_rules(self, rules_text: str):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
            f.write(rules_text)
            path = f.name
        try:
            self.app.config["EXCLUSION_RULES"] = load_rules(path)
        finally:
            os.unlink(path)

    def _search(self, query: str, search_type: str = "all"):
        resp = self.client.get(f"/api/search?q={query}&type={search_type}")
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIsInstance(payload, dict)
        self.assertIn("results", payload)
        return payload["results"]

    def test_exact_exclusion_keywords_hide_matches_case_insensitive(self):
        self._set_rules("kwd\n")

        results_lower = self._search("kwd", "all")
        results_upper = self._search("KWD", "all")

        self.assertEqual(results_lower, [])
        self.assertEqual(results_upper, [])

    def test_non_excluded_query_still_returns_visible_results(self):
        self._set_rules("kwd\n")

        results = self._search("roadmap", "all")

        self.assertTrue(results)
        self.assertTrue(any((r.get("chatTitle") or "").lower().find("roadmap") != -1 for r in results))
        self.assertTrue(all((r.get("chatTitle") or "").lower().find("kwd") == -1 for r in results))

    def test_filtering_uses_workspace_title_and_metadata(self):
        # Workspace folder resolves to ".../kwds" which must exclude kwd-workspace chat output.
        self._set_rules("kwds\n")
        results_by_workspace = self._search("archive", "all")
        self.assertEqual(results_by_workspace, [])

        # Metadata match (model name) must also exclude the matching composer entry.
        self._set_rules("gpt-4.1\n")
        results_by_metadata = self._search("extraction", "all")
        self.assertEqual(results_by_metadata, [])


if __name__ == "__main__":
    unittest.main()
