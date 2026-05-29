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
    get_cached_projects,
    set_cached_projects,
)


class TestSummaryCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_patch = patch.object(summary_cache, "CACHE_DIR", self.tmp.name)
        self.cache_patch.start()
        summary_cache.PROJECTS_CACHE_FILE = Path(self.tmp.name) / "projects.json"

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


if __name__ == "__main__":
    unittest.main()
