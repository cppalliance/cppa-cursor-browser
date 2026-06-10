"""
Unit tests for services/search.py — the three decomposed search functions
and shared helpers extracted from the monolithic api/search.py handler.

Each test class targets a single extracted function so failures pinpoint
the exact data-source reader that broke, independently of the Flask layer.

Run:
  pytest tests/test_search_helpers.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from models import ParseWarningCollector
from services.search import (
    _build_exclusion_searchable,
    _extract_snippet,
    _find_match,
    rank_results,
    search_cli_sessions,
    search_global_storage,
    search_legacy_workspaces,
)


# ---------------------------------------------------------------------------
# _extract_snippet
# ---------------------------------------------------------------------------


class TestExtractSnippet:
    def test_match_at_start_no_leading_ellipsis(self):
        text = "hello world foo"
        snippet = _extract_snippet(text, "hello", "hello")
        assert snippet.startswith("hello")
        assert not snippet.startswith("...")

    def test_match_in_middle_adds_ellipsis(self):
        padding = "x" * 200
        text = padding + "needle" + padding
        snippet = _extract_snippet(text, "needle", "needle")
        assert "needle" in snippet
        assert snippet.startswith("...")
        assert snippet.endswith("...")

    def test_no_match_returns_empty_string(self):
        assert _extract_snippet("no match here", "xyz", "xyz") == ""

    def test_case_insensitive_query_lower(self):
        text = "The Query appears here"
        snippet = _extract_snippet(text, "Query", "query")
        assert "Query" in snippet

    def test_snippet_length_is_bounded(self):
        text = "a" * 1000 + "target" + "b" * 1000
        snippet = _extract_snippet(text, "target", "target")
        # Context window: 80 before + len("target") + 120 after = ~206 chars + ellipses
        assert len(snippet) < 300


# ---------------------------------------------------------------------------
# _find_match
# ---------------------------------------------------------------------------


class TestFindMatch:
    def test_title_match_returns_full_title(self):
        has_match, text = _find_match("hello query world", [], "query", "query")
        assert has_match
        assert text == "hello query world"

    def test_bubble_match_returns_snippet(self):
        has_match, text = _find_match(
            "",
            ["padding " * 20 + "needle" + " padding" * 20],
            "needle",
            "needle",
        )
        assert has_match
        assert "needle" in text

    def test_no_match_returns_false_and_empty(self):
        has_match, text = _find_match("nothing here", ["also nothing"], "xyz", "xyz")
        assert not has_match
        assert text == ""

    def test_title_checked_before_bubbles(self):
        # Both title and bubble contain the term; title should win.
        has_match, text = _find_match(
            "The query is in the title",
            ["The query is also in bubbles"],
            "query",
            "query",
        )
        assert has_match
        assert text == "The query is in the title"

    def test_case_insensitive_title_match(self):
        has_match, _ = _find_match("HELLO WORLD", [], "hello", "hello")
        assert has_match

    def test_empty_title_and_empty_bubbles_no_match(self):
        has_match, text = _find_match("", [], "q", "q")
        assert not has_match
        assert text == ""


# ---------------------------------------------------------------------------
# rank_results
# ---------------------------------------------------------------------------


class TestRankResults:
    def test_sorted_by_timestamp_descending(self):
        results = [
            {"timestamp": 1000},
            {"timestamp": 3000},
            {"timestamp": 2000},
        ]
        ranked = rank_results(results)
        assert [r["timestamp"] for r in ranked] == [3000, 2000, 1000]

    def test_iso_string_timestamps_sort_correctly(self):
        results = [
            {"timestamp": "2024-01-01T00:00:00Z"},
            {"timestamp": "2025-01-01T00:00:00Z"},
            {"timestamp": "2023-01-01T00:00:00Z"},
        ]
        ranked = rank_results(results)
        assert ranked[0]["timestamp"] == "2025-01-01T00:00:00Z"
        assert ranked[-1]["timestamp"] == "2023-01-01T00:00:00Z"

    def test_empty_list_returns_empty(self):
        assert rank_results([]) == []

    def test_missing_timestamp_treated_as_zero(self):
        results = [{"timestamp": 500}, {}, {"timestamp": 100}]
        ranked = rank_results(results)
        assert ranked[0]["timestamp"] == 500
        # Missing timestamp entry sorts last
        assert "timestamp" not in ranked[-1]


# ---------------------------------------------------------------------------
# Fixtures — minimal SQLite databases for integration-style unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_workspace_root():
    """Temporary workspaceStorage + globalStorage directory pair."""
    with tempfile.TemporaryDirectory() as tmp:
        ws_root = os.path.join(tmp, "workspaceStorage")
        global_root = os.path.join(tmp, "globalStorage")
        cli_root = os.path.join(tmp, "cli_chats")
        os.makedirs(ws_root, exist_ok=True)
        os.makedirs(global_root, exist_ok=True)
        os.makedirs(cli_root, exist_ok=True)
        yield {
            "ws_root": ws_root,
            "global_root": global_root,
            "cli_root": cli_root,
            "tmp": tmp,
        }


def _make_global_db(global_root: str, composer_id: str, bubble_text: str) -> None:
    """Seed globalStorage/state.vscdb with one composer + one bubble."""
    db_path = os.path.join(global_root, "state.vscdb")
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:bub-1",
                json.dumps({"type": "user", "text": bubble_text}),
            ),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"composerData:{composer_id}",
                json.dumps({
                    "name": "Test conversation",
                    "createdAt": 1_715_000_000_000,
                    "lastUpdatedAt": 1_715_001_000_000,
                    "fullConversationHeadersOnly": [{"bubbleId": "bub-1"}],
                    "modelConfig": {"modelName": "gpt-4o"},
                }),
            ),
        )
        conn.commit()


def _make_workspace_db(
    ws_root: str,
    workspace_id: str,
    composer_id: str,
    folder: str,
    legacy_chat_text: str | None = None,
) -> None:
    """Seed a per-workspace state.vscdb + workspace.json."""
    ws_dir = os.path.join(ws_root, workspace_id)
    os.makedirs(ws_dir, exist_ok=True)
    with open(os.path.join(ws_dir, "workspace.json"), "w", encoding="utf-8") as fh:
        json.dump({"folder": folder}, fh)
    db_path = os.path.join(ws_dir, "state.vscdb")
    with contextlib.closing(sqlite3.connect(db_path)) as conn:
        conn.execute("CREATE TABLE ItemTable ([key] TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
            (
                "composer.composerData",
                json.dumps({"allComposers": [{"composerId": composer_id}]}),
            ),
        )
        if legacy_chat_text is not None:
            legacy_data = {
                "tabs": [{
                    "tabId": "tab-legacy-1",
                    "chatTitle": "Legacy chat",
                    "lastSendTime": "2026-01-01T00:00:00Z",
                    "bubbles": [{"type": "user", "text": legacy_chat_text}],
                }]
            }
            conn.execute(
                "INSERT INTO ItemTable ([key], value) VALUES (?, ?)",
                (
                    "workbench.panel.aichat.view.aichat.chatdata",
                    json.dumps(legacy_data),
                ),
            )
        conn.commit()


# ---------------------------------------------------------------------------
# search_global_storage
# ---------------------------------------------------------------------------


class TestSearchGlobalStorage:
    def test_returns_matching_composer(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_global_db(dirs["global_root"], "cmp-gs-1", "unique-search-term-gs")
        _make_workspace_db(dirs["ws_root"], "ws-gs-1", "cmp-gs-1", "/projects/myapp")

        results = search_global_storage(
            workspace_path=dirs["ws_root"],
            query="unique-search-term-gs",
            query_lower="unique-search-term-gs",
            rules=[],
            parse_warnings=ParseWarningCollector(),
        )

        assert len(results) >= 1
        assert any(r["chatId"] == "cmp-gs-1" for r in results)

    def test_no_match_returns_empty_list(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_global_db(dirs["global_root"], "cmp-gs-2", "some other content")
        _make_workspace_db(dirs["ws_root"], "ws-gs-2", "cmp-gs-2", "/projects/other")

        results = search_global_storage(
            workspace_path=dirs["ws_root"],
            query="xyzzy-no-match-ever",
            query_lower="xyzzy-no-match-ever",
            rules=[],
            parse_warnings=ParseWarningCollector(),
        )

        assert results == []

    def test_result_has_required_keys(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_global_db(dirs["global_root"], "cmp-gs-3", "search-key-check")
        _make_workspace_db(dirs["ws_root"], "ws-gs-3", "cmp-gs-3", "/projects/keys")

        results = search_global_storage(
            workspace_path=dirs["ws_root"],
            query="search-key-check",
            query_lower="search-key-check",
            rules=[],
            parse_warnings=ParseWarningCollector(),
        )

        assert results
        r = results[0]
        for key in ("workspaceId", "chatId", "chatTitle", "timestamp", "matchingText", "type"):
            assert key in r, f"missing key: {key}"
        assert r["type"] == "composer"
        assert isinstance(r["timestamp"], int)

    def test_missing_global_db_returns_empty(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        # No global DB created — directory exists but state.vscdb absent.
        results = search_global_storage(
            workspace_path=dirs["ws_root"],
            query="anything",
            query_lower="anything",
            rules=[],
            parse_warnings=ParseWarningCollector(),
        )
        assert results == []

    def test_workspace_display_name_resolved(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_global_db(dirs["global_root"], "cmp-gs-4", "name-check-term")
        _make_workspace_db(
            dirs["ws_root"], "ws-gs-4", "cmp-gs-4", "file:///home/user/projects/myrepo"
        )

        results = search_global_storage(
            workspace_path=dirs["ws_root"],
            query="name-check-term",
            query_lower="name-check-term",
            rules=[],
            parse_warnings=ParseWarningCollector(),
        )

        assert results
        # Workspace folder name is resolved to the basename of the folder path.
        assert results[0]["workspaceFolder"] == "myrepo"


# ---------------------------------------------------------------------------
# search_legacy_workspaces
# ---------------------------------------------------------------------------


class TestSearchLegacyWorkspaces:
    def test_returns_matching_legacy_tab(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_workspace_db(
            dirs["ws_root"],
            "ws-leg-1",
            "cmp-leg-1",
            "/projects/legacyapp",
            legacy_chat_text="legacy-unique-search-text",
        )

        results = search_legacy_workspaces(
            workspace_path=dirs["ws_root"],
            query="legacy-unique-search-text",
            query_lower="legacy-unique-search-text",
            search_type="all",
            rules=[],
        )

        assert len(results) >= 1
        assert any(r.get("type") == "chat" for r in results)

    def test_no_match_returns_empty(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_workspace_db(
            dirs["ws_root"],
            "ws-leg-2",
            "cmp-leg-2",
            "/projects/other",
            legacy_chat_text="something else entirely",
        )

        results = search_legacy_workspaces(
            workspace_path=dirs["ws_root"],
            query="xyzzy-absolutely-no-match",
            query_lower="xyzzy-absolutely-no-match",
            search_type="all",
            rules=[],
        )

        assert results == []

    def test_search_type_composer_returns_empty(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_workspace_db(
            dirs["ws_root"],
            "ws-leg-3",
            "cmp-leg-3",
            "/projects/skip",
            legacy_chat_text="type-guard-term",
        )

        results = search_legacy_workspaces(
            workspace_path=dirs["ws_root"],
            query="type-guard-term",
            query_lower="type-guard-term",
            search_type="composer",
            rules=[],
        )

        # Legacy workspaces only hold chat (type="chat"); composer search skips them.
        assert results == []

    def test_result_has_required_keys(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        _make_workspace_db(
            dirs["ws_root"],
            "ws-leg-4",
            "cmp-leg-4",
            "/projects/keycheck",
            legacy_chat_text="key-check-legacy",
        )

        results = search_legacy_workspaces(
            workspace_path=dirs["ws_root"],
            query="key-check-legacy",
            query_lower="key-check-legacy",
            search_type="chat",
            rules=[],
        )

        assert results
        r = results[0]
        for key in ("workspaceId", "chatId", "chatTitle", "timestamp", "matchingText", "type"):
            assert key in r, f"missing key: {key}"
        assert r["type"] == "chat"

    def test_workspace_without_legacy_data_skipped(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        # Workspace DB exists but has no chatdata key (modern workspaces).
        _make_workspace_db(
            dirs["ws_root"],
            "ws-leg-5",
            "cmp-leg-5",
            "/projects/modern",
            legacy_chat_text=None,  # no legacy chatdata row
        )

        results = search_legacy_workspaces(
            workspace_path=dirs["ws_root"],
            query="anything",
            query_lower="anything",
            search_type="all",
            rules=[],
        )

        assert results == []


# ---------------------------------------------------------------------------
# search_cli_sessions
# ---------------------------------------------------------------------------


class TestSearchCliSessions:
    def test_empty_cli_dir_returns_empty(self, tmp_workspace_root):
        dirs = tmp_workspace_root
        # cli_root is empty — no projects, no sessions.
        results = search_cli_sessions(
            cli_chats_path=dirs["cli_root"],
            query="anything",
            query_lower="anything",
            rules=[],
        )
        assert results == []

    def test_nonexistent_cli_dir_returns_empty(self):
        results = search_cli_sessions(
            cli_chats_path="/nonexistent/path/that/does/not/exist",
            query="anything",
            query_lower="anything",
            rules=[],
        )
        assert results == []
