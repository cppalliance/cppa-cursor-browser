from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.workspace_listing import list_workspace_projects


class TestMalformedCliSessionSkipped(unittest.TestCase):
    def test_session_missing_session_id_is_skipped_not_dropping_project(self) -> None:
        cli_project = {
            "project_id": "proj-1",
            "workspace_name": "My Project",
            "workspace_path": "/tmp/proj-1",
            "last_updated_ms": 1_715_000_000_000,
            "sessions": [
                {"meta": {"name": "Bad session"}},  # missing session_id
                {"session_id": "sess-ok", "meta": {"name": "Good session"}},
            ],
        }

        with tempfile.TemporaryDirectory() as tmp, \
             patch("services.workspace_listing.list_cli_projects", return_value=[cli_project]):
            projects = list_workspace_projects(tmp, rules=[])

        cli_entries = [p for p in projects if p.get("source") == "cli"]
        self.assertEqual(len(cli_entries), 1, msg=f"expected one CLI project, got {cli_entries}")
        self.assertEqual(cli_entries[0]["conversationCount"], 1)


if __name__ == "__main__":
    unittest.main()
