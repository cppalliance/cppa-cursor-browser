"""Structured {error, code} bodies across API blueprints (Week 30 #4)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests._fixture_ids import HAPPY_WORKSPACE_ID


def _assert_api_error(body: dict, *, code: str, message: str | None = None) -> None:
    assert body.get("code") == code
    assert isinstance(body.get("error"), str) and body["error"]
    if message is not None:
        assert body["error"] == message


@pytest.mark.parametrize(
    ("method", "path", "kwargs", "expected_status", "expected_code", "expected_message"),
    [
        (
            "get",
            "/api/workspaces/nonexistent-workspace-id",
            {},
            404,
            "workspace_not_found",
            "Workspace not found",
        ),
        (
            "get",
            "/api/workspaces/cli:proj-1/tabs/cmp-happy",
            {},
            400,
            "cli_tab_lazy_load_unsupported",
            None,
        ),
        (
            "get",
            f"/api/workspaces/{HAPPY_WORKSPACE_ID}/tabs/no-such-composer",
            {},
            404,
            "conversation_not_found",
            "Conversation not found",
        ),
        (
            "post",
            "/api/export",
            {"json": ["not", "an", "object"]},
            400,
            "invalid_json_body",
            "request body must be a JSON object",
        ),
        (
            "get",
            "/api/composers/no-such-composer-id",
            {},
            404,
            "composer_not_found",
            "Composer not found",
        ),
    ],
)
def test_representative_endpoints_return_stable_error_codes(
    client,
    method: str,
    path: str,
    kwargs: dict,
    expected_status: int,
    expected_code: str,
    expected_message: str | None,
) -> None:
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == expected_status
    _assert_api_error(response.get_json(), code=expected_code, message=expected_message)


def test_validate_path_invalid_json_includes_code(client) -> None:
    response = client.post(
        "/api/validate-path",
        data='"not an object"',
        content_type="application/json",
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["valid"] is False
    assert body["workspaceCount"] == 0
    assert body["error"] == "invalid JSON body"
    assert body["code"] == "invalid_json_body"


def test_missing_global_storage_tabs_returns_global_storage_code(
    empty_workspace_client,
) -> None:
    response = empty_workspace_client.get("/api/workspaces/global/tabs")
    assert response.status_code == 404
    _assert_api_error(
        response.get_json(),
        code="global_storage_not_found",
        message="Global storage not found",
    )


def test_workspaces_list_failure_returns_code(client) -> None:
    with patch(
        "api.workspaces.list_workspace_projects",
        side_effect=RuntimeError("simulated listing failure"),
    ):
        response = client.get("/api/workspaces")
    assert response.status_code == 500
    _assert_api_error(
        response.get_json(),
        code="workspaces_list_failed",
        message="Failed to get workspaces",
    )


def test_cli_workspace_tabs_missing_project_returns_code(client) -> None:
    with patch("services.cli_tabs.list_cli_projects", return_value=[]):
        response = client.get("/api/workspaces/cli:no-such-cli-project/tabs")
    assert response.status_code == 404
    _assert_api_error(
        response.get_json(),
        code="cli_project_not_found",
        message="CLI project not found",
    )


def test_cli_workspace_tabs_internal_failure_returns_code(client) -> None:
    with patch(
        "services.cli_tabs.list_cli_projects",
        side_effect=RuntimeError("simulated CLI tabs failure"),
    ):
        response = client.get("/api/workspaces/cli:proj-1/tabs")
    assert response.status_code == 500
    _assert_api_error(
        response.get_json(),
        code="cli_workspace_tabs_failed",
        message="Failed to get CLI workspace tabs",
    )
