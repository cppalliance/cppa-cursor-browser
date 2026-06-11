"""Flask test-client coverage for /api/workspaces* routes (issue #101)."""

from __future__ import annotations

from unittest.mock import patch

from tests._fixture_ids import HAPPY_COMPOSER_ID, HAPPY_WORKSPACE_ID
from tests.conftest import client_with_rules


def _assert_project_shape(project: dict) -> None:
    assert isinstance(project["id"], str)
    assert isinstance(project["name"], str)
    assert isinstance(project["conversationCount"], int)
    assert isinstance(project["lastModified"], str)
    assert "T" in project["lastModified"]


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
        _assert_project_shape(ws)

    def test_empty_storage_returns_empty_list(self, empty_workspace_client):
        response = empty_workspace_client.get("/api/workspaces")
        assert response.status_code == 200
        assert response.get_json() == {"projects": []}

    def test_internal_failure_returns_500(self, client):
        with patch(
            "api.workspaces.list_workspace_projects",
            side_effect=RuntimeError("simulated listing failure"),
        ):
            response = client.get("/api/workspaces")
        assert response.status_code == 500
        assert response.get_json().get("error") == "Failed to get workspaces"


class TestGetWorkspace:
    def test_happy_path_returns_workspace_details(self, client):
        response = client.get(f"/api/workspaces/{HAPPY_WORKSPACE_ID}")
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == HAPPY_WORKSPACE_ID
        assert isinstance(body["name"], str)
        assert "folder" in body
        assert isinstance(body["lastModified"], str) and "T" in body["lastModified"]

    def test_unknown_id_returns_404(self, client):
        response = client.get("/api/workspaces/nonexistent-workspace-id")
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_global_returns_other_chats(self, client):
        response = client.get("/api/workspaces/global")
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == "global"
        assert body["name"] == "Other chats"


class TestGetWorkspaceTabs:
    def test_happy_path_returns_tabs(self, client):
        response = client.get(f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body["tabs"], list)

        tab_ids = [t["id"] for t in body["tabs"]]
        assert HAPPY_COMPOSER_ID in tab_ids

        tab = next(t for t in body["tabs"] if t["id"] == HAPPY_COMPOSER_ID)
        assert isinstance(tab["title"], str)
        assert isinstance(tab["timestamp"], int)
        assert isinstance(tab["bubbles"], list)
        assert "user" in [b["type"] for b in tab["bubbles"]]

    def test_global_returns_tabs_without_leaking_assigned_composer(self, client):
        response = client.get("/api/workspaces/global/tabs")
        assert response.status_code == 200
        global_tab_ids = [t["id"] for t in response.get_json()["tabs"]]
        assert HAPPY_COMPOSER_ID not in global_tab_ids

    def test_missing_global_storage_returns_404(self, empty_workspace_client):
        response = empty_workspace_client.get("/api/workspaces/global/tabs")
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_summary_query_returns_tab_list_without_bubbles(self, client):
        response = client.get(f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs?summary=1")
        assert response.status_code == 200
        body = response.get_json()
        tab = next(t for t in body["tabs"] if t["id"] == HAPPY_COMPOSER_ID)
        assert isinstance(tab["messageCount"], int)
        assert "bubbles" not in tab


class TestGetWorkspaceTab:
    def test_happy_path_returns_single_tab(self, client):
        response = client.get(
            f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs/{HAPPY_COMPOSER_ID}"
        )
        assert response.status_code == 200
        tab = response.get_json()["tab"]
        assert tab["id"] == HAPPY_COMPOSER_ID
        assert isinstance(tab["bubbles"], list)
        assert "codeBlockDiffs" in tab

    def test_unknown_composer_returns_404(self, client):
        response = client.get(
            f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs/no-such-composer"
        )
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_cli_workspace_returns_400(self, client):
        response = client.get("/api/workspaces/cli:proj-1/tabs/cmp-happy")
        assert response.status_code == 400
        assert "error" in response.get_json()


class TestWorkspaceExclusionRules:
    def test_matching_rule_filters_workspace_from_list(self, workspace_storage):
        excluded_client = client_with_rules(["happy-project"])
        response = excluded_client.get("/api/workspaces")
        assert response.status_code == 200
        ids = [w["id"] for w in response.get_json()["projects"]]
        assert HAPPY_WORKSPACE_ID not in ids

    def test_non_matching_rule_leaves_workspace_visible(self, workspace_storage):
        kept_client = client_with_rules(["unrelated-project-name-xyzzy"])
        response = kept_client.get("/api/workspaces")
        assert response.status_code == 200
        ids = [w["id"] for w in response.get_json()["projects"]]
        assert HAPPY_WORKSPACE_ID in ids
