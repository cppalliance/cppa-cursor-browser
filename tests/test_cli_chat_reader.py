"""Unit tests for utils/cli_chat_reader.py.

Run:
  python -m unittest tests.test_cli_chat_reader -v
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

from utils.cli_chat_reader import (
    content_to_text,
    extract_blob_refs,
    extract_tool_calls,
    strip_user_info,
    aggregate_session_stats,
    extract_workspace_path,
    iter_sessions,
    list_cli_projects,
    messages_to_bubbles,
    traverse_blobs,
)


# ---------------------------------------------------------------------------
# Helpers to build minimal store.db fixtures in a temp directory
# ---------------------------------------------------------------------------

def _make_meta_value(meta: dict) -> str:
    """Encode a metadata dict as the hex-encoded JSON string that store.db stores."""
    return json.dumps(meta).encode("utf-8").hex()


def _build_store_db(path: str, meta: dict, json_blobs: dict[str, dict], chain: dict[str, list[str]]) -> None:
    """Create a store.db fixture at *path*.

    Parameters
    ----------
    meta:
        Session metadata dict (will be hex-encoded as JSON).
    json_blobs:
        Mapping of blob_id -> message dict.
    chain:
        Mapping of blob_id -> list[ref_blob_id] (chain/pointer blobs).
    """
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")

    conn.execute("INSERT INTO meta VALUES ('0', ?)", (_make_meta_value(meta),))

    for blob_id, msg in json_blobs.items():
        data = json.dumps(msg).encode("utf-8")
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, data))

    for blob_id, refs in chain.items():
        # Build binary chain node: for each ref, emit 0x0a 0x20 <32-byte hash>
        raw = b""
        for ref in refs:
            raw += b"\x0a\x20" + bytes.fromhex(ref)
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, raw))

    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# extract_blob_refs
# ---------------------------------------------------------------------------

class TestExtractBlobRefs(unittest.TestCase):
    def test_empty_bytes_returns_empty(self):
        self.assertEqual(extract_blob_refs(b""), [])

    def test_single_ref(self):
        ref = "a" * 64  # 32 bytes as hex
        raw = b"\x0a\x20" + bytes.fromhex(ref)
        self.assertEqual(extract_blob_refs(raw), [ref])

    def test_two_refs(self):
        ref1 = "a" * 64
        ref2 = "b" * 64
        raw = b"\x0a\x20" + bytes.fromhex(ref1) + b"\x0a\x20" + bytes.fromhex(ref2)
        self.assertEqual(extract_blob_refs(raw), [ref1, ref2])

    def test_noise_bytes_ignored(self):
        ref = "c" * 64
        noise = b"\x00\xff\x01\x02\x03\x04"
        raw = noise + b"\x0a\x20" + bytes.fromhex(ref) + b"\xde\xad"
        self.assertIn(ref, extract_blob_refs(raw))

    def test_partial_tag_at_end_ignored(self):
        # Only 0x0a without 0x20 immediately following should not produce a ref.
        raw = b"\x0a" + b"\x00" * 32
        self.assertEqual(extract_blob_refs(raw), [])


# ---------------------------------------------------------------------------
# content_to_text
# ---------------------------------------------------------------------------

class TestContentToText(unittest.TestCase):
    def test_string_passthrough(self):
        self.assertEqual(content_to_text("hello"), "hello")

    def test_list_with_text_parts(self):
        content = [{"type": "text", "text": "foo"}, {"type": "text", "text": "bar"}]
        result = content_to_text(content)
        self.assertIn("foo", result)
        self.assertIn("bar", result)

    def test_list_with_tool_result(self):
        content = [{"type": "tool-result", "result": "output here"}]
        self.assertIn("output here", content_to_text(content))

    def test_empty_list(self):
        self.assertEqual(content_to_text([]), "")

    def test_unknown_type_ignored(self):
        content = [{"type": "image", "url": "http://example.com/img.png"}]
        self.assertEqual(content_to_text(content), "")

    def test_non_string_non_list(self):
        self.assertEqual(content_to_text(None), "")
        self.assertEqual(content_to_text(42), "")


# ---------------------------------------------------------------------------
# extract_tool_calls
# ---------------------------------------------------------------------------

class TestExtractToolCalls(unittest.TestCase):
    def test_non_list_returns_empty(self):
        self.assertEqual(extract_tool_calls("text"), [])
        self.assertEqual(extract_tool_calls(None), [])

    def test_list_without_tool_call_type(self):
        content = [{"type": "text", "text": "hello"}]
        self.assertEqual(extract_tool_calls(content), [])

    def test_single_tool_call(self):
        content = [
            {"type": "tool-call", "toolName": "Shell", "args": {"command": "ls"}, "toolCallId": "tc-1"}
        ]
        calls = extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "Shell")
        self.assertEqual(calls[0]["args"], {"command": "ls"})
        self.assertEqual(calls[0]["toolCallId"], "tc-1")

    def test_mixed_content(self):
        content = [
            {"type": "text", "text": "I will run a command."},
            {"type": "tool-call", "toolName": "Grep", "args": {"pattern": "foo"}, "toolCallId": "tc-2"},
        ]
        calls = extract_tool_calls(content)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "Grep")


# ---------------------------------------------------------------------------
# extract_workspace_path
# ---------------------------------------------------------------------------

class TestExtractWorkspacePath(unittest.TestCase):
    def test_extracts_from_user_info_preamble(self):
        messages = [
            {
                "role": "user",
                "content": "<user_info>\nOS Version: linux\nWorkspace Path: /home/user/myproject\n</user_info>\n<user_query>hello</user_query>",
            }
        ]
        self.assertEqual(extract_workspace_path(messages), "/home/user/myproject")

    def test_returns_none_when_absent(self):
        messages = [{"role": "user", "content": "No preamble here."}]
        self.assertIsNone(extract_workspace_path(messages))

    def test_skips_non_user_messages(self):
        messages = [
            {"role": "system", "content": "Workspace Path: /should/be/ignored"},
            {"role": "assistant", "content": "Workspace Path: /also/ignored"},
            {"role": "user", "content": "Workspace Path: /home/user/real"},
        ]
        self.assertEqual(extract_workspace_path(messages), "/home/user/real")

    def test_returns_first_match(self):
        messages = [
            {"role": "user", "content": "Workspace Path: /first"},
            {"role": "user", "content": "Workspace Path: /second"},
        ]
        self.assertEqual(extract_workspace_path(messages), "/first")


# ---------------------------------------------------------------------------
# strip_user_info
# ---------------------------------------------------------------------------

class TestStripUserInfo(unittest.TestCase):
    def test_extracts_user_query_tag(self):
        text = "<user_info>some preamble</user_info>\n<user_query>my actual question</user_query>"
        self.assertEqual(strip_user_info(text), "my actual question")

    def test_strips_user_info_block_when_no_query_tag(self):
        text = "<user_info>preamble stuff</user_info>\nActual message here."
        result = strip_user_info(text)
        self.assertNotIn("<user_info>", result)
        self.assertIn("Actual message here.", result)

    def test_passthrough_when_no_user_info(self):
        self.assertEqual(strip_user_info("plain text"), "plain text")


# ---------------------------------------------------------------------------
# messages_to_bubbles
# ---------------------------------------------------------------------------

class TestMessagesToBubbles(unittest.TestCase):
    BASE_MS = 1_700_000_000_000

    def test_system_messages_skipped(self):
        messages = [{"role": "system", "content": "You are an agent."}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(bubbles, [])

    def test_tool_messages_skipped(self):
        messages = [{"role": "tool", "content": "tool result"}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(bubbles, [])

    def test_pure_preamble_user_message_skipped(self):
        messages = [{"role": "user", "content": "<user_info>OS: linux</user_info>"}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(bubbles, [])

    def test_user_message_with_query_tag(self):
        content = "<user_info>OS: linux</user_info>\n<user_query>Fix the bug</user_query>"
        messages = [{"role": "user", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(len(bubbles), 1)
        self.assertEqual(bubbles[0]["type"], "user")
        self.assertEqual(bubbles[0]["text"], "Fix the bug")

    def test_assistant_text_bubble(self):
        messages = [{"role": "assistant", "content": "Here is the fix."}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(len(bubbles), 1)
        self.assertEqual(bubbles[0]["type"], "ai")
        self.assertEqual(bubbles[0]["text"], "Here is the fix.")

    def test_assistant_with_tool_calls(self):
        content = [
            {"type": "tool-call", "toolName": "Shell", "args": {"command": "ls -la"}, "toolCallId": "tc-1"}
        ]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(len(bubbles), 1)
        self.assertIn("toolCalls", bubbles[0].get("metadata", {}))
        calls = bubbles[0]["metadata"]["toolCalls"]
        self.assertEqual(calls[0]["name"], "Shell")

    def test_tool_call_summary_shell(self):
        content = [{"type": "tool-call", "toolName": "Shell", "args": {"command": "echo hi"}, "toolCallId": "tc"}]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        summary = bubbles[0]["metadata"]["toolCalls"][0]["summary"]
        self.assertIn("echo hi", summary)

    def test_tool_call_summary_read(self):
        content = [{"type": "tool-call", "toolName": "Read", "args": {"path": "/foo/bar.py"}, "toolCallId": "tc"}]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        summary = bubbles[0]["metadata"]["toolCalls"][0]["summary"]
        self.assertIn("bar.py", summary)

    def test_tool_call_summary_grep(self):
        content = [{"type": "tool-call", "toolName": "Grep", "args": {"pattern": "TODO"}, "toolCallId": "tc"}]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        summary = bubbles[0]["metadata"]["toolCalls"][0]["summary"]
        self.assertIn("TODO", summary)

    def test_tool_call_summary_glob(self):
        content = [{"type": "tool-call", "toolName": "Glob", "args": {"glob_pattern": "**/*.py"}, "toolCallId": "tc"}]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        summary = bubbles[0]["metadata"]["toolCalls"][0]["summary"]
        self.assertIn("*.py", summary)

    def test_tool_call_summary_web_search(self):
        content = [{"type": "tool-call", "toolName": "WebSearch", "args": {"search_term": "Python async"}, "toolCallId": "tc"}]
        messages = [{"role": "assistant", "content": content}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        summary = bubbles[0]["metadata"]["toolCalls"][0]["summary"]
        self.assertIn("Python async", summary)

    def test_timestamps_increment(self):
        messages = [
            {"role": "user", "content": "<user_query>Q1</user_query>"},
            {"role": "assistant", "content": "A1"},
        ]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(len(bubbles), 2)
        self.assertLess(bubbles[0]["timestamp"], bubbles[1]["timestamp"])

    def test_empty_assistant_message_skipped(self):
        messages = [{"role": "assistant", "content": "   "}]
        bubbles = messages_to_bubbles(messages, self.BASE_MS)
        self.assertEqual(bubbles, [])

    def test_empty_messages_returns_empty(self):
        self.assertEqual(messages_to_bubbles([], self.BASE_MS), [])


# ---------------------------------------------------------------------------
# traverse_blobs (requires temporary store.db files)
# ---------------------------------------------------------------------------

class TestTraverseBlobs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _db(self, name: str) -> str:
        return os.path.join(self.tmpdir, name)

    def test_empty_meta_returns_empty(self):
        path = self._db("empty.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.commit()
        conn.close()
        self.assertEqual(traverse_blobs(path), [])

    def test_missing_root_returns_empty(self):
        path = self._db("no_root.db")
        _build_store_db(path, {"latestRootBlobId": ""}, {}, {})
        self.assertEqual(traverse_blobs(path), [])

    def test_single_json_message(self):
        msg = {"role": "user", "content": "Hello"}
        blob_id = "a" * 64
        path = self._db("single.db")
        _build_store_db(path, {"latestRootBlobId": blob_id}, {blob_id: msg}, {})
        result = traverse_blobs(path)
        self.assertEqual(result, [msg])

    def test_chain_preserves_chronological_order(self):
        """Linked-list chain (newest root -> prev node -> oldest msg) must produce oldest-first output.

        Mirrors the real CLI storage layout: latestRootBlobId is newest, and
        each chain node points to an older node/message.
        """
        root_id = "0" * 64    # latest chain node (newest end)
        prev_id = "f" * 64    # older chain node
        msg1_id = "1" * 64    # oldest message (user)
        msg2_id = "2" * 64    # newest message (assistant)
        msg1 = {"role": "user", "content": "first"}
        msg2 = {"role": "assistant", "content": "second"}
        path = self._db("chain.db")
        _build_store_db(
            path,
            {"latestRootBlobId": root_id},
            {msg1_id: msg1, msg2_id: msg2},
            # root (latest) references the newest message and a pointer to the older node;
            # prev node references the oldest message.
            {root_id: [msg2_id, prev_id], prev_id: [msg1_id]},
        )
        result = traverse_blobs(path)
        self.assertEqual(result, [msg1, msg2])

    def test_no_cycle_in_traversal(self):
        """Cyclic references must not loop forever."""
        a = "a" * 64
        b = "b" * 64
        path = self._db("cycle.db")
        _build_store_db(path, {"latestRootBlobId": a}, {}, {a: [b], b: [a]})
        result = traverse_blobs(path)
        self.assertEqual(result, [])

    def _write_raw_meta(self, name: str, raw_meta_value: str) -> str:
        """Build a minimal store.db with a hand-rolled meta row 0 value."""
        path = self._db(name)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
        conn.execute("INSERT INTO meta VALUES ('0', ?)", (raw_meta_value,))
        conn.commit()
        conn.close()
        return path

    def test_malformed_hex_meta_returns_empty(self):
        path = self._write_raw_meta("bad_hex.db", "zzzz")
        self.assertEqual(traverse_blobs(path), [])

    def test_non_utf8_meta_returns_empty(self):
        path = self._write_raw_meta("bad_utf8.db", "fffe")
        self.assertEqual(traverse_blobs(path), [])

    def test_invalid_json_meta_returns_empty(self):
        garbage = "not json".encode("utf-8").hex()
        path = self._write_raw_meta("bad_json.db", garbage)
        self.assertEqual(traverse_blobs(path), [])

    def test_non_dict_meta_returns_empty(self):
        list_payload = json.dumps(["not", "a", "dict"]).encode("utf-8").hex()
        path = self._write_raw_meta("non_dict.db", list_payload)
        self.assertEqual(traverse_blobs(path), [])


# ---------------------------------------------------------------------------
# iter_sessions and list_cli_projects
# ---------------------------------------------------------------------------

class TestIterSessions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.chats_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_session(self, project_id: str, session_id: str, meta: dict) -> str:
        """Create a minimal session directory under chats_dir and return db_path."""
        session_dir = os.path.join(self.chats_dir, project_id, session_id)
        os.makedirs(session_dir, exist_ok=True)
        db_path = os.path.join(session_dir, "store.db")
        _build_store_db(db_path, meta, {}, {})
        return db_path

    def test_empty_dir_yields_nothing(self):
        self.assertEqual(list(iter_sessions(self.chats_dir)), [])

    def test_nonexistent_dir_yields_nothing(self):
        self.assertEqual(list(iter_sessions("/nonexistent/path")), [])

    def test_session_without_store_db_skipped(self):
        project_dir = os.path.join(self.chats_dir, "proj1")
        os.makedirs(os.path.join(project_dir, "sess1"), exist_ok=True)
        self.assertEqual(list(iter_sessions(self.chats_dir)), [])

    def test_yields_single_session(self):
        self._make_session("proj1", "sess1", {"name": "My session", "createdAt": 1000})
        sessions = list(iter_sessions(self.chats_dir))
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["project_id"], "proj1")
        self.assertEqual(sessions[0]["session_id"], "sess1")
        self.assertEqual(sessions[0]["meta"].get("name"), "My session")

    def test_yields_multiple_sessions_across_projects(self):
        self._make_session("proj1", "sess1", {"name": "A"})
        self._make_session("proj1", "sess2", {"name": "B"})
        self._make_session("proj2", "sess3", {"name": "C"})
        sessions = list(iter_sessions(self.chats_dir))
        self.assertEqual(len(sessions), 3)


class TestListCliProjects(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.chats_dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _make_session(self, project_id: str, session_id: str, meta: dict, msg_content: str | None = None) -> str:
        session_dir = os.path.join(self.chats_dir, project_id, session_id)
        os.makedirs(session_dir, exist_ok=True)
        db_path = os.path.join(session_dir, "store.db")
        json_blobs: dict = {}
        chain: dict = {}
        if msg_content:
            blob_id = session_id[:64].ljust(64, "0")
            json_blobs[blob_id] = {"role": "user", "content": msg_content}
            meta = dict(meta, latestRootBlobId=blob_id)
        _build_store_db(db_path, meta, json_blobs, chain)
        return db_path

    def test_empty_dir_returns_empty_list(self):
        self.assertEqual(list_cli_projects(self.chats_dir), [])

    def test_sessions_grouped_by_project(self):
        self._make_session("proj1", "sess1", {"createdAt": 1000})
        self._make_session("proj1", "sess2", {"createdAt": 2000})
        self._make_session("proj2", "sess3", {"createdAt": 3000})
        projects = list_cli_projects(self.chats_dir)
        self.assertEqual(len(projects), 2)
        ids = {p["project_id"] for p in projects}
        self.assertIn("proj1", ids)
        self.assertIn("proj2", ids)

    def test_last_updated_ms_is_at_least_max_created_at(self):
        # last_updated_ms = max(createdAt, store.db mtime) across all sessions.
        # Since test files are freshly created, mtime >= createdAt always; we only
        # assert the floor: the value must be >= max(createdAt) and > 0.
        self._make_session("proj1", "sess1", {"createdAt": 1000})
        self._make_session("proj1", "sess2", {"createdAt": 5000})
        projects = list_cli_projects(self.chats_dir)
        proj = next(p for p in projects if p["project_id"] == "proj1")
        self.assertGreaterEqual(proj["last_updated_ms"], 5000)

    def test_workspace_name_extracted_from_user_info(self):
        ws_path = "/home/user/my-project"
        preamble = (
            f"<user_info>\nOS Version: linux\nWorkspace Path: {ws_path}\n</user_info>\n"
            "<user_query>hello</user_query>"
        )
        self._make_session("proj1", "sess1", {"createdAt": 1000, "latestRootBlobId": "s" * 64}, msg_content=preamble)
        projects = list_cli_projects(self.chats_dir)
        proj = projects[0]
        self.assertEqual(proj["workspace_path"], ws_path)
        self.assertEqual(proj["workspace_name"], "my-project")

    def test_workspace_name_falls_back_to_project_id_prefix(self):
        self._make_session("proj1abcdef123456", "sess1", {"createdAt": 1000})
        projects = list_cli_projects(self.chats_dir)
        proj = projects[0]
        # list_cli_projects uses pid[:12] as fallback
        self.assertEqual(proj["workspace_name"], "proj1abcdef1")


# ---------------------------------------------------------------------------
# aggregate_session_stats
# ---------------------------------------------------------------------------

class TestAggregateSessionStats(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmpdir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_created_ms_and_empty_messages_on_bad_db(self):
        session = {"db_path": "/nonexistent/store.db", "meta": {"createdAt": 42000}}
        stats = aggregate_session_stats(session)
        self.assertEqual(stats["created_ms"], 42000)
        self.assertEqual(stats["messages"], [])

    def test_counts_tool_calls(self):
        blob_id = "a" * 64
        msg = {
            "role": "assistant",
            "content": [
                {"type": "tool-call", "toolName": "Shell", "args": {"command": "ls"}, "toolCallId": "tc1"},
                {"type": "tool-call", "toolName": "Read", "args": {"path": "/foo"}, "toolCallId": "tc2"},
            ],
        }
        db_path = os.path.join(self.tmpdir, "sess.db")
        _build_store_db(db_path, {"latestRootBlobId": blob_id, "createdAt": 9000}, {blob_id: msg}, {})
        session = {"db_path": db_path, "meta": {"createdAt": 9000}}
        stats = aggregate_session_stats(session)
        self.assertEqual(stats["total_tool_calls"], 2)
        self.assertEqual(stats["tool_breakdown"].get("Shell"), 1)
        self.assertEqual(stats["tool_breakdown"].get("Read"), 1)

    def test_mode_and_session_name_from_meta(self):
        db_path = os.path.join(self.tmpdir, "sess2.db")
        _build_store_db(db_path, {}, {}, {})
        session = {"db_path": db_path, "meta": {"mode": "agent", "name": "My session", "createdAt": 1000}}
        stats = aggregate_session_stats(session)
        self.assertEqual(stats["mode"], "agent")
        self.assertEqual(stats["session_name"], "My session")


if __name__ == "__main__":
    unittest.main()
