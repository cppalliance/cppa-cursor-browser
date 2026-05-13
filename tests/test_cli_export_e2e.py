from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import export as export_script  # noqa: E402


HAPPY_COMPOSER_ID = "cmp-export-happy"
HAPPY_BUBBLE_ID = "bub-export-happy"
HAPPY_WORKSPACE_ID = "ws-export-happy"


def _make_global_state_db(path: str, *, last_updated_ms: int = 1_715_000_500_000) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"composerData:{HAPPY_COMPOSER_ID}",
            json.dumps({
                "name": "Export E2E conversation",
                "createdAt": 1_715_000_000_000,
                "lastUpdatedAt": last_updated_ms,
                "fullConversationHeadersOnly": [
                    {"bubbleId": HAPPY_BUBBLE_ID, "type": 1},
                ],
                "modelConfig": {"modelName": "gpt-4o"},
            }),
        ),
    )
    conn.execute(
        "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
        (
            f"bubbleId:{HAPPY_COMPOSER_ID}:{HAPPY_BUBBLE_ID}",
            json.dumps({
                "text": "hello from the e2e test fixture",
                "type": "user",
                "createdAt": 1_715_000_400_000,
            }),
        ),
    )
    conn.commit()
    conn.close()


def _make_workspace_storage(parent: str, *, last_updated_ms: int = 1_715_000_500_000) -> str:
    ws_root = os.path.join(parent, "workspaceStorage")
    global_root = os.path.join(parent, "globalStorage")
    os.makedirs(ws_root, exist_ok=True)
    os.makedirs(global_root, exist_ok=True)

    ws_dir = os.path.join(ws_root, HAPPY_WORKSPACE_ID)
    os.makedirs(ws_dir, exist_ok=True)
    project_folder = os.path.join(parent, "happy-project")
    os.makedirs(project_folder, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as f:
        json.dump({"folder": project_folder}, f)
    local_db = os.path.join(ws_dir, "state.vscdb")
    conn = sqlite3.connect(local_db)
    conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
        ("composer.composerData", json.dumps({"allComposers": [{"composerId": HAPPY_COMPOSER_ID}]})),
    )
    conn.commit()
    conn.close()

    _make_global_state_db(os.path.join(global_root, "state.vscdb"), last_updated_ms=last_updated_ms)
    return ws_root


@contextmanager
def _run_export(argv: list[str], *, workspace_path: str, state_dir: str):
    """Invoke ``scripts.export.main()`` under controlled env + argv."""
    prior_argv = sys.argv
    prior_ws = os.environ.get("WORKSPACE_PATH")
    prior_cli = os.environ.get("CLI_CHATS_PATH")
    prior_state = os.environ.get("XDG_STATE_HOME")
    sys.argv = ["scripts/export.py", *argv]
    os.environ["WORKSPACE_PATH"] = workspace_path
    os.environ["CLI_CHATS_PATH"] = os.path.join(os.path.dirname(workspace_path), "cli_chats_empty")
    os.makedirs(os.environ["CLI_CHATS_PATH"], exist_ok=True)
    os.environ["XDG_STATE_HOME"] = state_dir
    captured = io.StringIO()
    prior_stdout = sys.stdout
    sys.stdout = captured
    try:
        try:
            export_script.main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
        yield captured.getvalue()
    finally:
        sys.stdout = prior_stdout
        sys.argv = prior_argv
        for key, prior in (("WORKSPACE_PATH", prior_ws), ("CLI_CHATS_PATH", prior_cli), ("XDG_STATE_HOME", prior_state)):
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


class TestCliExportEndToEnd(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name
        self.workspace_path = _make_workspace_storage(self.tmpdir)
        self.out_dir = os.path.join(self.tmpdir, "out")
        os.makedirs(self.out_dir, exist_ok=True)
        self.state_dir = os.path.join(self.tmpdir, "state")
        os.makedirs(self.state_dir, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    # ─── no-zip mode ────────────────────────────────────────────────────────

    def test_export_no_zip_writes_markdown_files(self):
        with _run_export(["--out", self.out_dir, "--no-zip"],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass

        md_files = list(Path(self.out_dir).rglob("*.md"))
        self.assertTrue(md_files, msg=f"expected markdown files under {self.out_dir}, found nothing")

        # The seeded composer surfaces as one of the .md files
        matched = [p for p in md_files if HAPPY_COMPOSER_ID[:8] in p.name]
        self.assertTrue(matched, msg=f"expected composer id in filename, got {[p.name for p in md_files]}")

    def test_markdown_frontmatter_has_required_fields(self):
        with _run_export(["--out", self.out_dir, "--no-zip"],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass

        md_files = list(Path(self.out_dir).rglob("*.md"))
        matched = [p for p in md_files if HAPPY_COMPOSER_ID[:8] in p.name]
        self.assertTrue(matched)
        content = matched[0].read_text(encoding="utf-8")

        # Frontmatter must contain the five spec-required fields
        fm_match = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
        self.assertIsNotNone(fm_match, msg="expected YAML frontmatter block at top of file")
        fm_text = fm_match.group(1)
        for required in ("log_id", "title", "workspace", "created_at", "updated_at"):
            self.assertIn(f"{required}:", fm_text,
                          msg=f"frontmatter missing required field '{required}'\n---\n{fm_text}")

    # ─── zip mode ──────────────────────────────────────────────────────────

    def test_export_zip_mode_writes_archive(self):
        with _run_export(["--out", self.out_dir],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass

        zips = list(Path(self.out_dir).rglob("*.zip"))
        self.assertTrue(zips, msg=f"expected a zip archive under {self.out_dir}, found {list(Path(self.out_dir).iterdir())}")
        with zipfile.ZipFile(zips[0], "r") as zf:
            names = zf.namelist()
            self.assertTrue(any(name.endswith(".md") for name in names),
                            msg=f"expected .md entries inside {zips[0].name}, got {names}")

    # ─── manifest.jsonl ────────────────────────────────────────────────────

    def test_manifest_jsonl_has_expected_shape(self):
        with _run_export(["--out", self.out_dir, "--no-zip"],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass

        manifest_path = os.path.join(self.out_dir, "manifest.jsonl")
        self.assertTrue(os.path.isfile(manifest_path),
                        msg=f"expected manifest.jsonl at {manifest_path}")
        entries = []
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        self.assertTrue(entries, msg="manifest.jsonl is empty")
        entry = next((e for e in entries if e.get("log_id") == HAPPY_COMPOSER_ID), None)
        self.assertIsNotNone(entry, msg=f"manifest missing seeded composer; got {entries}")
        for required in ("log_id", "path", "updated_at"):
            self.assertIn(required, entry, msg=f"manifest entry missing '{required}': {entry}")

    # ─── --since last incremental ─────────────────────────────────────────

    def test_since_last_skips_already_exported_records(self):
        # First export: writes everything.
        with _run_export(["--out", self.out_dir, "--no-zip"],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass
        md_after_first = list(Path(self.out_dir).rglob("*.md"))
        self.assertTrue(md_after_first)

        # Capture timestamps so we can detect re-writes.
        before_mtimes = {p.name: p.stat().st_mtime_ns for p in md_after_first}

        # Second export with --since last and no new data should not regenerate
        # the markdown for the unchanged composer.
        with _run_export(["--out", self.out_dir, "--no-zip", "--since", "last"],
                         workspace_path=self.workspace_path,
                         state_dir=self.state_dir):
            pass

        md_after_second = list(Path(self.out_dir).rglob("*.md"))
        # Same set of files (no new ones added because the composer wasn't touched)
        self.assertEqual(
            sorted(p.name for p in md_after_first),
            sorted(p.name for p in md_after_second),
        )
        # mtimes for the existing composer's markdown should NOT have advanced
        seeded_after_second = next(
            (p for p in md_after_second if HAPPY_COMPOSER_ID[:8] in p.name), None
        )
        self.assertIsNotNone(seeded_after_second)
        self.assertEqual(
            before_mtimes[seeded_after_second.name],
            seeded_after_second.stat().st_mtime_ns,
            msg="--since last should not rewrite the already-exported markdown",
        )


if __name__ == "__main__":
    unittest.main()
