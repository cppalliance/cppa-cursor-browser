"""Flask test-client coverage for GET /api/search (issue #101, #117)."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from tests._fixture_ids import HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID
from tests._helpers import client_with_rules


def _assert_search_result_shape(hit: dict) -> None:
    """Verify one /api/search hit matches the SearchResult contract."""
    assert isinstance(hit["workspaceId"], str)
    assert hit.get("workspaceFolder") is None or isinstance(hit["workspaceFolder"], str)
    assert isinstance(hit["chatId"], str)
    assert isinstance(hit["chatTitle"], str)
    assert isinstance(hit["timestamp"], (int, str))
    assert isinstance(hit["matchingText"], str)
    assert hit["type"] in ("composer", "chat", "cli_agent")
    if "source" in hit:
        assert hit["source"] == "cli"


def _assert_error_body(body: dict, *, code: str) -> None:
    assert body.get("code") == code
    assert isinstance(body.get("error"), str) and body["error"]


class TestSearchHappyPath:
    def test_finds_seeded_term_and_result_shape(self, client):
        response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert "results" in body and isinstance(body["results"], list)
        assert len(body["results"]) >= 1, f"expected sentinel match, got {body}"

        hit = body["results"][0]
        _assert_search_result_shape(hit)
        assert hit["chatId"] == HAPPY_COMPOSER_ID
        assert "sentinel-grep" in hit["matchingText"].lower()

    def test_type_composer_still_finds_global_match(self, client):
        response = client.get("/api/search?q=sentinel-grep&type=composer&all_history=1")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["results"], list)
        assert len(body["results"]) >= 1
        assert all(r["type"] == "composer" for r in body["results"])


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_code"),
    [
        ("/api/search", 400, "empty_query"),
        ("/api/search?q=", 400, "empty_query"),
        ("/api/search?q=%20%20%20", 400, "empty_query"),
        ("/api/search?q=x&type=invalid", 400, "invalid_type"),
        ("/api/search?q=x&since_days=0", 400, "invalid_since_days"),
        ("/api/search?q=x&since_days=not-a-number", 400, "invalid_since_days"),
        (
            "/api/search?q=no-such-workspace-scope&workspace=does-not-exist-xyzzy",
            404,
            "workspace_not_found",
        ),
        (
            "/api/search?q=x&workspace=..%2F..%2Fetc",
            404,
            "workspace_not_found",
        ),
    ],
)
def test_search_error_status_codes(client, path, expected_status, expected_code):
    response = client.get(path)
    assert response.status_code == expected_status
    body = response.get_json()
    assert isinstance(body, dict)
    _assert_error_body(body, code=expected_code)
    assert "results" not in body


class TestSearchErrorResponses:
    def test_query_too_long_returns_400(self, client):
        response = client.get("/api/search?q=" + ("x" * 501))
        assert response.status_code == 400
        _assert_error_body(response.get_json(), code="query_too_long")

    def test_internal_failure_returns_500(self, client):
        with patch(
            "api.search.search_global_storage",
            side_effect=RuntimeError("simulated DB failure"),
        ):
            response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 500
        body = response.get_json()
        _assert_error_body(body, code="internal_error")
        assert "results" not in body

    def test_index_lock_returns_503(self, client):
        with patch(
            "api.search.search_global_storage",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 503
        body = response.get_json()
        _assert_error_body(body, code="search_index_unavailable")
        assert "results" not in body

    def test_workspace_path_resolution_failure_returns_structured_500(self, client):
        with patch(
            "api.search.resolve_workspace_path",
            side_effect=OSError("simulated storage discovery failure"),
        ):
            response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 500
        _assert_error_body(response.get_json(), code="internal_error")


class TestSearchEdgeCases:
    def test_no_match_returns_empty_results(self, client):
        response = client.get("/api/search?q=does-not-match-any-content-xyzzy")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("results") == []

    def test_exclusion_rule_filters_matching_conversation(self, workspace_storage):
        excluded_client = client_with_rules(["Happy"])
        response = excluded_client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 200
        assert response.get_json().get("results") == []

    def test_workspace_scoped_hit_has_workspace_id(self, client):
        response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 200
        results = response.get_json()["results"]
        workspace_ids = {r["workspaceId"] for r in results}
        assert HAPPY_WORKSPACE_ID in workspace_ids or "global" in workspace_ids

    def test_workspace_filter_limits_results(self, client):
        response = client.get(
            f"/api/search?q=sentinel-grep&all_history=1&workspace={HAPPY_WORKSPACE_ID}",
        )
        assert response.status_code == 200
        results = response.get_json()["results"]
        assert results
        assert all(r["workspaceId"] == HAPPY_WORKSPACE_ID for r in results)


class TestSearchWindow:
    def test_default_window_excludes_old_seeded_composer(self, client):
        response = client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("allHistory") is False
        assert body.get("searchWindowDays") == 30
        assert body.get("results") == []

    def test_all_history_includes_old_seeded_composer(self, client):
        response = client.get("/api/search?q=sentinel-grep&all_history=1")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("allHistory") is True
        assert body.get("searchWindowDays") is None
        assert len(body.get("results", [])) >= 1

    def test_all_history_true_string_accepted(self, client):
        response = client.get("/api/search?q=sentinel-grep&all_history=true")
        assert response.status_code == 200
        assert response.get_json().get("allHistory") is True
