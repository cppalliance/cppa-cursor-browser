from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.cli_tabs import _get_cli_workspace_tabs


def _make_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = []
    return app


def _fake_project(project_id: str, sessions: list[dict]) -> dict:
    return {
        "project_id": project_id,
        "workspace_name": "Test Project",
        "workspace_path": "/tmp/test",
        "last_updated_ms": 1_715_000_000_000,
        "sessions": sessions,
    }


def _fake_session(session_id: str) -> dict:
    return {
        "session_id": session_id,
        "db_path": f"/tmp/{session_id}.db",
        "meta": {"createdAt": 1_715_000_000_000, "name": "Session"},
    }


class TestMessagesToBubblesFailureIsolation(unittest.TestCase):
    def test_failing_session_does_not_500_endpoint(self) -> None:
        app = _make_app()
        project = _fake_project("proj-1", [
            _fake_session("sess-bad"),
            _fake_session("sess-good"),
        ])

        def fake_messages_to_bubbles(messages, created_ms):
            if messages == ["bad"]:
                raise RuntimeError("simulated bubble conversion failure")
            return [{"type": "user", "text": "hi", "timestamp": created_ms}]

        def fake_traverse_blobs(db_path):
            return ["bad"] if "sess-bad" in db_path else ["ok"]

        with app.test_request_context("/api/workspaces/cli:proj-1/tabs"), \
             patch("services.cli_tabs.list_cli_projects", return_value=[project]), \
             patch("services.cli_tabs.traverse_blobs", side_effect=fake_traverse_blobs), \
             patch("services.cli_tabs.messages_to_bubbles", side_effect=fake_messages_to_bubbles):
            response = _get_cli_workspace_tabs("cli:proj-1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        tab_ids = [t["id"] for t in payload["tabs"]]
        self.assertNotIn("sess-bad", tab_ids)
        self.assertIn("sess-good", tab_ids)


class TestMalformedSessionRecordSkipped(unittest.TestCase):
    def test_session_missing_session_id_is_skipped_not_500(self) -> None:
        app = _make_app()
        project = _fake_project("proj-1", [
            {"db_path": "/tmp/missing-id.db", "meta": {}},  # no session_id
            _fake_session("sess-good"),
        ])

        def fake_traverse_blobs(db_path):
            return ["ok"]

        def fake_messages_to_bubbles(messages, created_ms):
            return [{"type": "user", "text": "hi", "timestamp": created_ms}]

        with app.test_request_context("/api/workspaces/cli:proj-1/tabs"), \
             patch("services.cli_tabs.list_cli_projects", return_value=[project]), \
             patch("services.cli_tabs.traverse_blobs", side_effect=fake_traverse_blobs), \
             patch("services.cli_tabs.messages_to_bubbles", side_effect=fake_messages_to_bubbles):
            response = _get_cli_workspace_tabs("cli:proj-1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        tab_ids = [t["id"] for t in payload["tabs"]]
        self.assertEqual(tab_ids, ["sess-good"])


if __name__ == "__main__":
    unittest.main()
