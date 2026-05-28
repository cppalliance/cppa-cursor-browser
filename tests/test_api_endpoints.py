from __future__ import annotations

from app import create_app
from tests._fixture_ids import HAPPY_BUBBLE_ID, HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID
from utils.exclusion_rules import tokenize_rule


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

class TestListWorkspaces:
    def test_happy_path_returns_workspace_list(self, client):
        response = client.get("/api/workspaces")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        projects = body["projects"]
        assert isinstance(projects, list)

        ids = [p["id"] for p in projects]
        assert HAPPY_WORKSPACE_ID in ids, f"expected {HAPPY_WORKSPACE_ID} in {ids}"

        ws = next(p for p in projects if p["id"] == HAPPY_WORKSPACE_ID)
        assert "name" in ws
        assert "conversationCount" in ws and isinstance(ws["conversationCount"], int)
        assert "lastModified" in ws and "T" in ws["lastModified"]

    def test_empty_storage_returns_empty_list(self, empty_workspace_client):
        response = empty_workspace_client.get("/api/workspaces")
        assert response.status_code == 200
        assert response.get_json() == {"projects": []}


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>
# ---------------------------------------------------------------------------

class TestGetWorkspace:
    def test_happy_path_returns_workspace_details(self, client):
        response = client.get(f"/api/workspaces/{HAPPY_WORKSPACE_ID}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == HAPPY_WORKSPACE_ID
        assert "name" in body
        assert "folder" in body
        assert "lastModified" in body and "T" in body["lastModified"]

    def test_unknown_id_returns_404(self, client):
        response = client.get("/api/workspaces/nonexistent-workspace-id")
        assert response.status_code == 404
        body = response.get_json()
        assert "error" in body

    def test_global_returns_other_chats(self, client):
        response = client.get("/api/workspaces/global")
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == "global"
        assert body["name"] == "Other chats"


# ---------------------------------------------------------------------------
# GET /api/workspaces/<id>/tabs
# ---------------------------------------------------------------------------

class TestGetWorkspaceTabs:
    def test_happy_path_returns_tabs(self, client):
        response = client.get(f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs")
        assert response.status_code == 200
        body = response.get_json()
        assert "tabs" in body and isinstance(body["tabs"], list)

        tab_ids = [t["id"] for t in body["tabs"]]
        assert HAPPY_COMPOSER_ID in tab_ids, f"expected {HAPPY_COMPOSER_ID} in {tab_ids}"

        tab = next(t for t in body["tabs"] if t["id"] == HAPPY_COMPOSER_ID)
        assert "title" in tab
        assert "timestamp" in tab and isinstance(tab["timestamp"], int)
        assert "bubbles" in tab and isinstance(tab["bubbles"], list)
        # The seeded user bubble must be present
        bubble_types = [b["type"] for b in tab["bubbles"]]
        assert "user" in bubble_types

    def test_global_returns_tabs(self, client):
        response = client.get("/api/workspaces/global/tabs")
        assert response.status_code == 200
        body = response.get_json()
        assert "tabs" in body and isinstance(body["tabs"], list)
        # Isolation: HAPPY_COMPOSER_ID is assigned to HAPPY_WORKSPACE_ID via the
        # local ItemTable allComposers row, so it must NOT also surface in the
        # /global bucket. If it does, workspace-assignment is leaking unassigned
        # composers into both buckets.
        global_tab_ids = [t["id"] for t in body["tabs"]]
        assert HAPPY_COMPOSER_ID not in global_tab_ids, (
            f"{HAPPY_COMPOSER_ID} leaked into /global tabs: {global_tab_ids}"
        )

    def test_missing_global_storage_returns_404(self, empty_workspace_client):
        response = empty_workspace_client.get("/api/workspaces/global/tabs")
        assert response.status_code == 404
        body = response.get_json()
        assert "error" in body


# ---------------------------------------------------------------------------
# GET /api/search?q=...
# ---------------------------------------------------------------------------

class TestSearch:
    def test_happy_path_finds_seeded_term(self, client):
        response = client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 200
        body = response.get_json()
        assert "results" in body and isinstance(body["results"], list)
        assert len(body["results"]) >= 1, f"expected sentinel match, got {body}"

    def test_no_match_returns_empty_results(self, client):
        response = client.get("/api/search?q=does-not-match-any-content-xyzzy")
        assert response.status_code == 200
        body = response.get_json()
        assert "results" in body and body["results"] == []

    def test_missing_q_returns_400(self, client):
        response = client.get("/api/search")
        assert response.status_code == 400
        body = response.get_json()
        assert "error" in body
        assert body["error"] == "No search query provided"

    def test_empty_q_returns_400(self, client):
        response = client.get("/api/search?q=")
        assert response.status_code == 400
        body = response.get_json()
        assert body.get("error") == "No search query provided"

    def test_whitespace_only_q_returns_400(self, client):
        # api/search.py strips q before the empty-check, so "   " is rejected.
        response = client.get("/api/search?q=%20%20%20")
        assert response.status_code == 400
        body = response.get_json()
        assert body.get("error") == "No search query provided"


# ---------------------------------------------------------------------------
# Exclusion rules — must be applied across endpoints
# ---------------------------------------------------------------------------

def _client_with_rules(rule_lines):
    """Build a Flask test client whose EXCLUSION_RULES match the given lines.

    The standard `client` fixture sets EXCLUSION_RULES = [] because no
    rules file exists under the temp workspace. This helper builds a fresh
    app on top of the same env (already pointed at workspace_storage) and
    overrides the config with parsed rules — exercising the same code path
    a real `exclusion-rules.txt` file would.
    """
    parsed = [tokenize_rule(line) for line in rule_lines]
    app = create_app()
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = [r for r in parsed if r]
    return app.test_client()


class TestExclusionRules:
    def test_workspace_matching_rule_is_filtered_out_of_list(self, workspace_storage):
        # The seeded workspace's display name resolves to "happy-project"
        # (the basename of the folder linked from workspace.json). A rule of
        # "happy-project" must drop it from /api/workspaces entirely.
        excluded_client = _client_with_rules(["happy-project"])
        response = excluded_client.get("/api/workspaces")
        assert response.status_code == 200
        body = response.get_json()
        ids = [w["id"] for w in body["projects"]]
        assert HAPPY_WORKSPACE_ID not in ids, (
            f"exclusion rule did not filter {HAPPY_WORKSPACE_ID}; got {ids}"
        )

    def test_workspace_not_matching_rule_still_listed(self, workspace_storage):
        # Negative control: a rule that doesn't match must leave the workspace
        # visible, so the test above can't pass for the wrong reason
        # (e.g. listing always returning []).
        kept_client = _client_with_rules(["unrelated-project-name-xyzzy"])
        response = kept_client.get("/api/workspaces")
        assert response.status_code == 200
        body = response.get_json()
        ids = [w["id"] for w in body["projects"]]
        assert HAPPY_WORKSPACE_ID in ids, (
            f"non-matching rule filtered the workspace; got {ids}"
        )

    def test_search_skips_conversations_matching_rule(self, workspace_storage):
        # The seeded conversation's name is "Happy conversation". Excluding by
        # "Happy" must drop the seeded match from /api/search even though the
        # bubble text still contains "sentinel-grep".
        excluded_client = _client_with_rules(["Happy"])
        response = excluded_client.get("/api/search?q=sentinel-grep")
        assert response.status_code == 200
        body = response.get_json()
        assert body.get("results") == [], (
            f"exclusion rule did not filter seeded chat from search: {body}"
        )
