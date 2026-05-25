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
            projects, _warnings = list_workspace_projects(tmp, rules=[])

        cli_entries = [p for p in projects if p.get("source") == "cli"]
        self.assertEqual(len(cli_entries), 1, msg=f"expected one CLI project, got {cli_entries}")
        self.assertEqual(cli_entries[0]["conversationCount"], 1)


class TestMalformedCliProjectRecordSkipped(unittest.TestCase):
    def test_non_dict_entries_are_skipped(self) -> None:
        # A None / string / int entry in cli_projects must not KeyError out
        # of the whole CLI section.
        garbage_then_real = [
            None,
            "not a dict",
            42,
            {
                "project_id": "real",
                "workspace_name": "Real Project",
                "last_updated_ms": 1_715_000_000_000,
                "sessions": [{"session_id": "sess-1", "meta": {"name": "S1"}}],
            },
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch("services.workspace_listing.list_cli_projects", return_value=garbage_then_real):
            projects, _warnings = list_workspace_projects(tmp, rules=[])

        cli_entries = [p for p in projects if p.get("source") == "cli"]
        self.assertEqual(len(cli_entries), 1)
        self.assertEqual(cli_entries[0]["id"], "cli:real")

    def test_project_missing_project_id_skipped(self) -> None:
        # Without project_id we can't render a meaningful CLI entry; skip.
        bad = [
            {"workspace_name": "Orphan", "sessions": [{"session_id": "sess-x"}]},
            {"project_id": "ok", "workspace_name": "OK",
             "sessions": [{"session_id": "sess-ok", "meta": {"name": "S"}}]},
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch("services.workspace_listing.list_cli_projects", return_value=bad):
            projects, _warnings = list_workspace_projects(tmp, rules=[])

        cli_entries = [p for p in projects if p.get("source") == "cli"]
        self.assertEqual([p["id"] for p in cli_entries], ["cli:ok"])

    def test_project_missing_optional_fields_renders_with_defaults(self) -> None:
        # Only project_id + sessions present; workspace_name / last_updated_ms
        # absent. Must still render (name falls back to project_id[:12],
        # lastModified falls back to now()).
        minimal = [{
            "project_id": "minimal-id",
            "sessions": [{"session_id": "sess-min", "meta": {"name": "S"}}],
        }]
        with tempfile.TemporaryDirectory() as tmp, \
             patch("services.workspace_listing.list_cli_projects", return_value=minimal):
            projects, _warnings = list_workspace_projects(tmp, rules=[])

        cli_entries = [p for p in projects if p.get("source") == "cli"]
        self.assertEqual(len(cli_entries), 1)
        self.assertEqual(cli_entries[0]["id"], "cli:minimal-id")
        self.assertEqual(cli_entries[0]["name"], "minimal-id"[:12])
        self.assertIn("T", cli_entries[0]["lastModified"])


if __name__ == "__main__":
    unittest.main()
