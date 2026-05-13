from __future__ import annotations

from tests.conftest import HAPPY_BUBBLE_ID, HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID


# ---------------------------------------------------------------------------
# GET /api/workspaces
# ---------------------------------------------------------------------------

class TestListWorkspaces:
    def test_happy_path_returns_workspace_list(self, client):
        response = client.get("/api/workspaces")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, list)

        ids = [p["id"] for p in body]
        assert HAPPY_WORKSPACE_ID in ids, f"expected {HAPPY_WORKSPACE_ID} in {ids}"

        ws = next(p for p in body if p["id"] == HAPPY_WORKSPACE_ID)
        assert "name" in ws
        assert "conversationCount" in ws and isinstance(ws["conversationCount"], int)
        assert "lastModified" in ws and "T" in ws["lastModified"]

    def test_empty_storage_returns_empty_list(self, empty_workspace_client):
        response = empty_workspace_client.get("/api/workspaces")
        assert response.status_code == 200
        assert response.get_json() == []


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
