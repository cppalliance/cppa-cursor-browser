from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.cli_tabs import get_cli_workspace_tabs


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
            response = get_cli_workspace_tabs("cli:proj-1", [])

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
            response = get_cli_workspace_tabs("cli:proj-1", [])

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        tab_ids = [t["id"] for t in payload["tabs"]]
        self.assertEqual(tab_ids, ["sess-good"])


class TestMalformedCliProjectsListLookup(unittest.TestCase):
    """``cp["project_id"]`` in the lookup generator (line 17) used to
    KeyError out of the entire endpoint when cli_projects contained a
    non-dict entry. The lookup now filters with isinstance(cp, dict)."""

    def test_non_dict_entries_in_cli_projects_list_do_not_500(self) -> None:
        app = _make_app()
        garbage_then_real = [None, "not a dict", 42, _fake_project("proj-1", [_fake_session("sess-1")])]

        def fake_traverse_blobs(db_path):
            return ["ok"]

        def fake_messages_to_bubbles(messages, created_ms):
            return [{"type": "user", "text": "hi", "timestamp": created_ms}]

        with app.test_request_context("/api/workspaces/cli:proj-1/tabs"), \
             patch("services.cli_tabs.list_cli_projects", return_value=garbage_then_real), \
             patch("services.cli_tabs.traverse_blobs", side_effect=fake_traverse_blobs), \
             patch("services.cli_tabs.messages_to_bubbles", side_effect=fake_messages_to_bubbles):
            response = get_cli_workspace_tabs("cli:proj-1", [])

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual([t["id"] for t in payload["tabs"]], ["sess-1"])

    def test_project_missing_workspace_name_uses_fallback(self) -> None:
        app = _make_app()
        project = {
            "project_id": "proj-min",
            "sessions": [{"session_id": "sess-min", "db_path": "/tmp/x.db", "meta": {}}],
        }

        with app.test_request_context("/api/workspaces/cli:proj-min/tabs"), \
             patch("services.cli_tabs.list_cli_projects", return_value=[project]), \
             patch("services.cli_tabs.traverse_blobs", return_value=["ok"]), \
             patch("services.cli_tabs.messages_to_bubbles",
                   return_value=[{"type": "user", "text": "hi", "timestamp": 1}]):
            response = get_cli_workspace_tabs("cli:proj-min", [])

        self.assertEqual(response.status_code, 200)
        # Tab still rendered — ws_name fallback (project_id[:12]) used for searchable text.
        self.assertEqual(len(response.get_json()["tabs"]), 1)

    def test_project_missing_sessions_returns_200_empty_tabs(self) -> None:
        app = _make_app()
        # ``project["sessions"]`` would KeyError before the fix.
        project = {"project_id": "proj-empty", "workspace_name": "Empty"}

        with app.test_request_context("/api/workspaces/cli:proj-empty/tabs"), \
             patch("services.cli_tabs.list_cli_projects", return_value=[project]):
            response = get_cli_workspace_tabs("cli:proj-empty", [])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"tabs": []})


if __name__ == "__main__":
    unittest.main()
