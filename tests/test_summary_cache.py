"""Phase 3 — mtime-keyed summary disk cache."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services import summary_cache
from pathlib import Path

from services.summary_cache import (
    fingerprint_workspace_storage,
    get_cached_invalid_workspace_aliases,
    get_cached_projects,
    set_cached_invalid_workspace_aliases,
    set_cached_projects,
)


class TestSummaryCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(summary_cache, "CACHE_DIR", self.tmp.name)
        self.cache_patch.start()
        summary_cache.PROJECTS_CACHE_FILE = Path(self.tmp.name) / "projects.json"
        summary_cache.INVALID_WORKSPACE_ALIASES_CACHE_FILE = (
            Path(self.tmp.name) / "invalid-workspace-aliases.json"
        )

    def tearDown(self):
        self.cache_patch.stop()
        self.tmp.cleanup()

    def test_cache_hit_when_fingerprint_unchanged(self):
        fp = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        projects = [{"id": "a", "name": "A", "conversationCount": 1, "lastModified": "x"}]
        warnings: list = []
        set_cached_projects(fp, projects, warnings)
        hit = get_cached_projects(fp)
        self.assertIsNotNone(hit)
        assert hit is not None
        self.assertEqual(hit[0], projects)

    def test_cache_miss_when_fingerprint_changes(self):
        fp1 = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        fp2 = {**fp1, "global_db_mtime_ns": 101}
        set_cached_projects(fp1, [{"id": "a"}], [])
        self.assertIsNone(get_cached_projects(fp2))

    def test_nocache_env(self):
        with patch.dict(os.environ, {"CURSOR_CHAT_BROWSER_NOCACHE": "1"}):
            self.assertTrue(summary_cache.nocache_enabled())

    def test_fingerprint_includes_workspace_files(self):
        with tempfile.TemporaryDirectory() as ws:
            entry_dir = os.path.join(ws, "entry1")
            os.makedirs(entry_dir)
            db = os.path.join(entry_dir, "state.vscdb")
            with open(db, "wb") as f:
                f.write(b"x")
            entries = [{"name": "entry1", "workspaceJsonPath": os.path.join(entry_dir, "workspace.json")}]
            fp = fingerprint_workspace_storage(ws, entries, global_db_path=None, rules=[])
            self.assertTrue(fp["workspace_files"])

    def test_workspace_files_fingerprint_round_trip(self):
        """JSON cache round-trip must match freshly computed workspace_files fingerprints."""
        with tempfile.TemporaryDirectory() as ws:
            entry_dir = os.path.join(ws, "entry1")
            os.makedirs(entry_dir)
            db = os.path.join(entry_dir, "state.vscdb")
            with open(db, "wb") as f:
                f.write(b"x")
            entries = [{"name": "entry1", "workspaceJsonPath": os.path.join(entry_dir, "workspace.json")}]
            fp = fingerprint_workspace_storage(ws, entries, global_db_path=None, rules=[])
            projects = [{"id": "a", "name": "A", "conversationCount": 1, "lastModified": "x"}]
            warnings: list = []
            set_cached_projects(fp, projects, warnings)
            fp2 = fingerprint_workspace_storage(ws, entries, global_db_path=None, rules=[])
            hit = get_cached_projects(fp2)
            self.assertIsNotNone(hit, msg="cache miss after JSON round-trip of workspace_files")
            assert hit is not None
            self.assertEqual(hit[0], projects)

    def test_invalid_workspace_aliases_cache_hit(self):
        fp = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        aliases = {"broken-ws": "good-ws"}
        set_cached_invalid_workspace_aliases(fp, aliases)
        hit = get_cached_invalid_workspace_aliases(fp)
        self.assertEqual(hit, aliases)

    def test_invalid_workspace_aliases_cache_miss_on_fingerprint_change(self):
        fp1 = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        fp2 = {**fp1, "global_db_mtime_ns": 101}
        set_cached_invalid_workspace_aliases(fp1, {"broken-ws": "good-ws"})
        self.assertIsNone(get_cached_invalid_workspace_aliases(fp2))

    def test_invalid_workspace_aliases_rejects_non_string_entries(self):
        fp = {"version": 1, "workspace_path": "/ws", "global_db_mtime_ns": 100}
        summary_cache._write_cache_file(
            summary_cache.INVALID_WORKSPACE_ALIASES_CACHE_FILE,
            {
                "fingerprint": fp,
                "invalid_workspace_aliases": {"broken-ws": 123},
            },
        )
        self.assertIsNone(get_cached_invalid_workspace_aliases(fp))


if __name__ == "__main__":
    unittest.main()
