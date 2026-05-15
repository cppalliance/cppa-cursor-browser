from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

from flask import Flask

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from api.workspaces import bp as workspaces_bp


def _make_app():
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = []
    app.register_blueprint(workspaces_bp)
    return app


class TestGetWorkspaceCliMalformedProject(unittest.TestCase):
    def test_cli_project_missing_optional_fields_returns_200_with_safe_defaults(self) -> None:
        # CLI project record has the matching project_id but is missing
        # workspace_name / workspace_path / last_updated_ms. Direct dict
        # access would have KeyError'd; the safe .get() path returns 200.
        cli_project_minimal = {"project_id": "abcd1234567890"}
        with patch("api.workspaces.list_cli_projects", return_value=[cli_project_minimal]):
            client = _make_app().test_client()
            response = client.get("/api/workspaces/cli:abcd1234567890")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["id"], "cli:abcd1234567890")
        # Falls back to project_id[:12] when workspace_name is absent
        self.assertEqual(body["name"], "abcd12345678")
        # path/folder propagate None gracefully
        self.assertIsNone(body["path"])
        self.assertIsNone(body["folder"])
        # lastModified is a valid ISO string even with no timestamp
        self.assertIn("T", body["lastModified"])
        self.assertEqual(body["source"], "cli")

    def test_cli_project_with_non_dict_entry_does_not_500(self) -> None:
        # A non-dict entry in cli_projects must be skipped, not KeyError.
        garbage_then_real = [
            None,
            "not a dict",
            {"project_id": "real-id", "workspace_name": "Real", "workspace_path": "/tmp/x", "last_updated_ms": 0},
        ]
        with patch("api.workspaces.list_cli_projects", return_value=garbage_then_real):
            client = _make_app().test_client()
            response = client.get("/api/workspaces/cli:real-id")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["id"], "cli:real-id")
        self.assertEqual(body["name"], "Real")


if __name__ == "__main__":
    unittest.main()
