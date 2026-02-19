"""
Integration tests for CLI export exclusion filtering.

Run:
  python -m unittest tests.test_export_exclusion_filtering -v
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
EXPORT_SCRIPT = REPO_ROOT / "scripts" / "export.py"


class TestExportExclusionFiltering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fake_home = self.base / "home"
        self.fake_home.mkdir(parents=True, exist_ok=True)
        self.workspace_path = self.base / "workspaceStorage"
        self.global_storage_path = self.base / "globalStorage"
        self.workspace_path.mkdir(parents=True, exist_ok=True)
        self.global_storage_path.mkdir(parents=True, exist_ok=True)
        self.global_db_path = self.global_storage_path / "state.vscdb"
        self._create_global_db()

    def tearDown(self):
        self.tmp.cleanup()

    def _create_global_db(self):
        conn = sqlite3.connect(self.global_db_path)
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
        conn.close()

    def _insert_bubble(self, composer_id: str, bubble_id: str, bubble_obj: dict):
        conn = sqlite3.connect(self.global_db_path)
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (f"bubbleId:{composer_id}:{bubble_id}", json.dumps(bubble_obj)),
        )
        conn.commit()
        conn.close()

    def _insert_composer(self, composer_id: str, title: str, model_name: str, bubble_ids: list[str]):
        payload = {
            "name": title,
            "modelConfig": {"modelName": model_name},
            "fullConversationHeadersOnly": [{"bubbleId": bid, "type": 1} for bid in bubble_ids],
            "lastUpdatedAt": 1739300000000,
            "createdAt": 1739200000000,
        }
        conn = sqlite3.connect(self.global_db_path)
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (f"composerData:{composer_id}", json.dumps(payload)),
        )
        conn.commit()
        conn.close()

    def _run_export(self, rules_text: str):
        rules_file = self.base / "exclusion-rules.txt"
        rules_file.write_text(rules_text, encoding="utf-8")
        out_dir = self.base / "out"
        env = dict(os.environ)
        env["WORKSPACE_PATH"] = str(self.workspace_path)
        env["HOME"] = str(self.fake_home)
        env["USERPROFILE"] = str(self.fake_home)

        proc = subprocess.run(
            [
                sys.executable,
                str(EXPORT_SCRIPT),
                "--since",
                "all",
                "--no-zip",
                "--out",
                str(out_dir),
                "--exclude-rules",
                str(rules_file),
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, msg=f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}")
        return out_dir

    def _collect_exported_markdown(self, out_dir: Path):
        return sorted(out_dir.rglob("*.md"))

    def test_filters_by_chat_content_case_insensitive_substring(self):
        # "kwd" rule must match and exclude content containing "kwds".
        self._insert_bubble("cmp-kwd", "b-kwd-1", {"type": "user", "text": "Please summarize all kwds for Q1."})
        self._insert_bubble("cmp-safe", "b-safe-1", {"type": "user", "text": "Create a project roadmap for Q3."})
        self._insert_composer("cmp-kwd", "Finance thread", "gpt-4.1", ["b-kwd-1"])
        self._insert_composer("cmp-safe", "Roadmap notes", "gpt-4.1-mini", ["b-safe-1"])

        out_dir = self._run_export("kwd\n")
        md_files = self._collect_exported_markdown(out_dir)

        self.assertEqual(len(md_files), 1)
        content = md_files[0].read_text(encoding="utf-8").lower()
        self.assertIn("roadmap", content)
        self.assertNotIn("kwd", content)
        self.assertNotIn("kwds", content)

    def test_filters_by_metadata_model_name(self):
        # Rule matches model metadata even when message text doesn't include the term.
        self._insert_bubble("cmp-meta", "b-meta-1", {"type": "user", "text": "Debug API timeout behavior."})
        self._insert_bubble("cmp-safe", "b-safe-2", {"type": "assistant", "text": "Roadmap items are now listed."})
        self._insert_composer("cmp-meta", "API notes", "claude-3.5-sonnet", ["b-meta-1"])
        self._insert_composer("cmp-safe", "Roadmap", "gpt-4.1-mini", ["b-safe-2"])

        out_dir = self._run_export("claude-3.5-sonnet\n")
        md_files = self._collect_exported_markdown(out_dir)

        self.assertEqual(len(md_files), 1)
        content = md_files[0].read_text(encoding="utf-8").lower()
        self.assertIn("roadmap", content)
        self.assertNotIn("claude-3.5-sonnet", content)

    def test_filters_when_term_appears_after_long_prefix(self):
        # Regression: exclusion matching must scan beyond first 50k chars.
        very_long_text = ("a" * 60000) + " kwds appear near the tail"
        self._insert_bubble("cmp-long", "b-long-1", {"type": "assistant", "text": very_long_text})
        self._insert_bubble("cmp-safe", "b-safe-3", {"type": "assistant", "text": "General roadmap update."})
        self._insert_composer("cmp-long", "Long transcript", "gpt-4.1", ["b-long-1"])
        self._insert_composer("cmp-safe", "Roadmap", "gpt-4.1-mini", ["b-safe-3"])

        out_dir = self._run_export("kwd\n")
        md_files = self._collect_exported_markdown(out_dir)

        self.assertEqual(len(md_files), 1)
        content = md_files[0].read_text(encoding="utf-8").lower()
        self.assertIn("roadmap", content)
        self.assertNotIn("kwd", content)

    def test_writes_manifest_to_global_state_dir(self):
        self._insert_bubble("cmp-safe", "b-safe-4", {"type": "assistant", "text": "General roadmap update."})
        self._insert_composer("cmp-safe", "Roadmap", "gpt-4.1-mini", ["b-safe-4"])

        out_dir = self._run_export("kwd\n")
        local_manifest = out_dir / "manifest.jsonl"
        global_manifest = self.fake_home / ".cursor-chat-browser" / "manifest.jsonl"
        export_state = self.fake_home / ".cursor-chat-browser" / "export_state.json"

        self.assertTrue(local_manifest.is_file())
        self.assertTrue(global_manifest.is_file())
        self.assertTrue(export_state.is_file())

        global_lines = [l for l in global_manifest.read_text(encoding="utf-8").splitlines() if l.strip()]
        self.assertTrue(global_lines)
        row = json.loads(global_lines[0])
        self.assertIn("log_id", row)
        self.assertIn("path", row)
        self.assertTrue(Path(row["path"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
