"""Unit tests for utils/cursor_md_exporter.py.

Run:
  python -m unittest tests.test_cursor_md_exporter -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from utils.cursor_md_exporter import (
    _build_ide_session_summary,
    cursor_cli_session_to_markdown,
)


def _make_meta_hex(meta: dict) -> str:
    return json.dumps(meta).encode("utf-8").hex()


def _build_store_db(path: str, meta: dict, json_blobs: dict[str, dict]) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO meta VALUES ('0', ?)", (_make_meta_hex(meta),))
    for blob_id, msg in json_blobs.items():
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, json.dumps(msg).encode("utf-8")))
    conn.commit()
    conn.close()


class TestCursorCliSessionToMarkdown(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _db_path(self, name: str = "store.db") -> str:
        return os.path.join(self.tmpdir, name)

    def _simple_session(self, name: str = "My Session", mode: str = "agent") -> tuple[str, dict]:
        """Build a store.db with one user message and one assistant reply.

        Models the real CLI linked-list layout: root (latest chain node) references
        the newest message (assistant) and a pointer to an older chain node which
        holds the older message (user).  After traverse_blobs() reverses, callers
        receive [user, assistant] in chronological order.
        """
        root_id = "0" * 64   # latest chain node
        prev_id = "f" * 64   # older chain node
        blob_user = "a" * 64
        blob_asst = "b" * 64
        meta = {
            "agentId": "test-agent-id",
            "latestRootBlobId": root_id,
            "name": name,
            "mode": mode,
            "createdAt": 1_700_000_000_000,
        }
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (json.dumps(meta).encode("utf-8").hex(),))
        # root -> [newest msg (asst), prev chain node]
        root_chain = b"\x0a\x20" + bytes.fromhex(blob_asst) + b"\x0a\x20" + bytes.fromhex(prev_id)
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (root_id, root_chain))
        # prev chain node -> [oldest msg (user)]
        prev_chain = b"\x0a\x20" + bytes.fromhex(blob_user)
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (prev_id, prev_chain))
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (
            blob_user, json.dumps({"role": "user", "content": "<user_query>Write a sort function</user_query>"}).encode("utf-8")
        ))
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (
            blob_asst, json.dumps({"role": "assistant", "content": "Here is a sort function."}).encode("utf-8")
        ))
        conn.commit()
        conn.close()
        return db_path, meta

    def test_returns_string(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIsInstance(result, str)

    def test_contains_yaml_frontmatter(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertTrue(result.startswith("---\n"))
        self.assertIn("\n---\n", result)

    def test_frontmatter_includes_session_id(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn('log_id: "test-agent-id"', result)

    def test_frontmatter_includes_log_type(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("log_type: cli_agent", result)

    def test_frontmatter_includes_mode(self):
        db_path, _ = self._simple_session(mode="agent")
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn('mode: "agent"', result)

    def test_frontmatter_title_from_session_name(self):
        db_path, _ = self._simple_session(name="My Custom Title")
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("My Custom Title", result)

    def test_title_derived_from_first_user_message_when_generic(self):
        blob_user = "a" * 64
        meta = {
            "agentId": "agent-x",
            "latestRootBlobId": blob_user,
            "name": "New Agent Session",
            "createdAt": 1_700_000_000_000,
        }
        json_blobs = {
            blob_user: {"role": "user", "content": "<user_query>Refactor the parser module</user_query>"},
        }
        db_path = self._db_path()
        _build_store_db(db_path, meta, json_blobs)
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("Refactor the parser module", result)

    def test_body_contains_user_and_assistant_headings(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("### User", result)
        self.assertIn("### Assistant", result)

    def test_body_contains_message_text(self):
        db_path, _ = self._simple_session()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("Write a sort function", result)
        self.assertIn("Here is a sort function.", result)

    def test_tool_calls_rendered_in_body(self):
        blob_asst = "a" * 64
        meta = {
            "agentId": "ag1",
            "latestRootBlobId": blob_asst,
            "name": "Tool session",
            "createdAt": 1_700_000_000_000,
        }
        json_blobs = {
            blob_asst: {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "Shell", "args": {"command": "git status"}, "toolCallId": "tc1"}
                ],
            }
        }
        db_path = self._db_path()
        _build_store_db(db_path, meta, json_blobs)
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("**Tool:", result)
        self.assertIn("git status", result)

    def test_tool_call_count_in_frontmatter(self):
        blob_asst = "a" * 64
        meta = {
            "agentId": "ag2",
            "latestRootBlobId": blob_asst,
            "name": "Tool count session",
            "createdAt": 1_700_000_000_000,
        }
        json_blobs = {
            blob_asst: {
                "role": "assistant",
                "content": [
                    {"type": "tool-call", "toolName": "Read", "args": {"path": "/foo"}, "toolCallId": "tc1"},
                    {"type": "tool-call", "toolName": "Read", "args": {"path": "/bar"}, "toolCallId": "tc2"},
                ],
            }
        }
        db_path = self._db_path()
        _build_store_db(db_path, meta, json_blobs)
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn("total_tool_calls: 2", result)
        self.assertIn("tool_call_breakdown:", result)
        self.assertIn('"Read": 2', result)

    def test_with_session_meta_kwarg(self):
        """session_meta kwarg skips reading from the database."""
        blob_user = "a" * 64
        meta = {
            "agentId": "override-id",
            "latestRootBlobId": blob_user,
            "name": "Provided Meta Session",
            "createdAt": 1_700_000_000_000,
        }
        json_blobs = {blob_user: {"role": "user", "content": "<user_query>Hello</user_query>"}}
        db_path = self._db_path()
        _build_store_db(db_path, meta, json_blobs)

        provided_meta = {
            "agentId": "override-id",
            "name": "Provided Meta Session",
            "mode": "custom",
            "createdAt": 1_700_000_000_000,
        }
        result = cursor_cli_session_to_markdown(db_path, session_meta=provided_meta)
        self.assertIn('mode: "custom"', result)
        self.assertIn("Provided Meta Session", result)

    def test_empty_session_produces_valid_markdown(self):
        db_path = self._db_path()
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.commit()
        conn.close()
        result = cursor_cli_session_to_markdown(db_path)
        self.assertIsInstance(result, str)
        self.assertIn("---", result)

    def test_title_quoting_with_special_chars(self):
        blob_user = "a" * 64
        meta = {
            "agentId": "agent-q",
            "latestRootBlobId": blob_user,
            'name': 'He said "hello"',
            "createdAt": 1_700_000_000_000,
        }
        json_blobs = {blob_user: {"role": "user", "content": "<user_query>test</user_query>"}}
        db_path = self._db_path()
        _build_store_db(db_path, meta, json_blobs)
        result = cursor_cli_session_to_markdown(db_path)
        # Frontmatter title must have embedded quotes escaped as \"
        self.assertIn('title: "He said \\"hello\\""', result)

    def test_non_dict_meta_json_falls_back_to_empty_mapping(self):
        """List/scalar meta JSON must not crash; agentId falls back to parent dir name."""
        db_path = self._db_path("scalar-meta")
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.execute(
            "INSERT INTO meta VALUES ('0', ?)",
            (json.dumps(["not", "a", "dict"]).encode("utf-8").hex(),),
        )
        conn.commit()
        conn.close()

        result = cursor_cli_session_to_markdown(db_path)
        self.assertIn(f'log_id: "{Path(db_path).parent.name}"', result)
        self.assertIn("log_type: cli_agent", result)

    def test_web_only_tool_stats_produce_session_summary(self):
        """Search/web-only sessions still get a Tool Results summary block."""
        summary = _build_ide_session_summary(
            [],
            [],
            [],
            {
                "terminal_success": 0,
                "terminal_error": 0,
                "file_reads": 0,
                "file_edits": 0,
                "searches": 0,
                "web": 2,
            },
        )
        self.assertIn("## Session Summary", summary)
        self.assertIn("### Tool Results", summary)
        self.assertIn("Web Fetches: 2", summary)


if __name__ == "__main__":
    unittest.main()
