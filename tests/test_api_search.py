"""Flask test-client coverage for GET /api/search (issue #101)."""

from __future__ import annotations

from unittest.mock import patch

from tests._fixture_ids import HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID
from tests.conftest import client_with_rules


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


class TestSearchHappyPath:
    def test_finds_seeded_term_and_result_shape(self, client):
        response = client.get("/api/search?q=sentinel-grep")
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
        response = client.get("/api/search?q=sentinel-grep&type=composer")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["results"], list)
        assert len(body["results"]) >= 1
        assert all(r["type"] == "composer" for r in body["results"])


class TestSearchErrorResponses:
    def test_missing_q_returns_400(self, client):
        response = client.get("/api/search")
        assert response.status_code == 400
        body = response.get_json()
        assert body.get("error") == "No search query provided"
        assert "results" not in body

    def test_empty_q_returns_400(self, client):
        response = client.get("/api/search?q=")
        assert response.status_code == 400
        assert response.get_json().get("error") == "No search query provided"

    def test_whitespace_only_q_returns_400(self, client):
        response = client.get("/api/search?q=%20%20%20")
        assert response.status_code == 400
        assert response.get_json().get("error") == "No search query provided"

    def test_internal_failure_returns_500_with_empty_results(self, client):
        with patch(
            "api.search.search_global_storage",
            side_effect=RuntimeError("simulated DB failure"),
        ):
            response = client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 500
        body = response.get_json()
        assert body.get("error") == "Search failed"
        assert body.get("results") == []


class TestSearchEdgeCases:
    def test_no_match_returns_empty_results(self, client):
        response = client.get("/api/search?q=does-not-match-any-content-xyzzy")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("results") == []

    def test_exclusion_rule_filters_matching_conversation(self, workspace_storage):
        excluded_client = client_with_rules(["Happy"])
        response = excluded_client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 200
        assert response.get_json().get("results") == []

    def test_workspace_scoped_hit_has_workspace_id(self, client):
        response = client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 200
        results = response.get_json()["results"]
        workspace_ids = {r["workspaceId"] for r in results}
        assert HAPPY_WORKSPACE_ID in workspace_ids or "global" in workspace_ids
