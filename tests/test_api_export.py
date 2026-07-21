"""Flask test-client coverage for /api/export routes (issue #101)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import zipfile
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from app import create_app
from tests._fixture_ids import HAPPY_COMPOSER_ID


@pytest.fixture
def export_state_dir(tmp_path, monkeypatch):
    """Redirect export state reads/writes to a temp directory."""
    state_dir = tmp_path / ".cursor-chat-browser"
    state_dir.mkdir()
    monkeypatch.setattr("api.export_api._get_state_dir", lambda: str(state_dir))
    return state_dir


def _post_export(client: FlaskClient, body: dict | None = None):
    return client.post(
        "/api/export",
        json=body if body is not None else {},
        content_type="application/json",
    )


def _read_zip_entries(data: bytes) -> list[str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return zf.namelist()


class TestExportState:
    def test_get_state_returns_json_object(self, client, export_state_dir):
        response = client.get("/api/export/state")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)

    def test_get_state_reflects_saved_export(self, client, export_state_dir):
        state_path = export_state_dir / "export_state.json"
        state_path.write_text(
            json.dumps({"lastExportTime": "2026-01-01T12:00:00", "exportedCount": 3}),
            encoding="utf-8",
        )
        response = client.get("/api/export/state")
        assert response.status_code == 200
        body = response.get_json()
        assert body["exportedCount"] == 3
        assert body["lastExportTime"] == "2026-01-01T12:00:00"

    def test_get_state_handles_non_dict_json(self, client, export_state_dir):
        state_path = export_state_dir / "export_state.json"
        state_path.write_text('["not","a","dict"]', encoding="utf-8")
        response = client.get("/api/export/state")
        assert response.status_code == 200
        body = response.get_json()
        assert isinstance(body, dict)
        assert body == {}


class TestExportHappyPath:
    def test_post_returns_zip_with_markdown_entry(self, client, export_state_dir):
        response = _post_export(client)
        assert response.status_code == 200
        assert response.content_type.startswith("application/zip")
        assert (
            'attachment; filename="cursor-export.zip"'
            in response.headers.get("Content-Disposition", "")
        )
        assert int(response.headers.get("X-Export-Count", "0")) >= 1

        names = _read_zip_entries(response.data)
        assert any(name.endswith(".md") for name in names)
        assert any(HAPPY_COMPOSER_ID[:8] in name for name in names)

        state_path = export_state_dir / "export_state.json"
        assert state_path.is_file()
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["exportedCount"] >= 1
        assert isinstance(saved["lastExportTime"], str)


class TestExportErrorResponses:
    def test_non_dict_json_body_returns_400(self, client, export_state_dir):
        response = client.post(
            "/api/export",
            json=["not", "an", "object"],
            content_type="application/json",
        )
        assert response.status_code == 400
        body = response.get_json()
        assert body.get("error") == "request body must be a JSON object"
        assert body.get("code") == "invalid_json_body"

    def test_missing_global_storage_returns_404(self, empty_workspace_client):
        response = _post_export(empty_workspace_client)
        assert response.status_code == 404
        body = response.get_json()
        assert body.get("error") == "Cursor global storage not found"
        assert body.get("code") == "global_storage_not_found"

    def test_no_conversations_returns_404(self, workspace_storage, export_state_dir):
        """Global DB exists but has no exportable composer rows."""
        ws_root = workspace_storage
        parent = os.path.dirname(ws_root)
        global_db = os.path.join(parent, "globalStorage", "state.vscdb")
        with contextlib.closing(sqlite3.connect(global_db)) as conn:
            conn.execute("DELETE FROM cursorDiskKV")
            conn.commit()

        app = create_app()
        app.config["TESTING"] = True
        app.config["EXCLUSION_RULES"] = []
        response = _post_export(app.test_client())
        assert response.status_code == 404
        body = response.get_json()
        assert body.get("error") == "No conversations to export"
        assert body.get("code") == "no_conversations_to_export"

    def test_internal_failure_returns_500(self, client, export_state_dir):
        with patch(
            "api.export_api.collect_export_entries",
            side_effect=RuntimeError("simulated export failure"),
        ):
            response = _post_export(client)
        assert response.status_code == 500
        body = response.get_json()
        assert body.get("error") == "Export failed"
        assert body.get("code") == "export_failed"


class TestExportEdgeCases:
    def test_since_last_with_no_prior_state_exports_all(
        self, client, export_state_dir
    ):
        response = _post_export(client, {"since": "last"})
        assert response.status_code == 200
        assert int(response.headers.get("X-Export-Count", "0")) >= 1

    def test_since_last_after_export_returns_404_when_nothing_new(
        self, client, export_state_dir
    ):
        # Relies on the seeded composer's lastUpdatedAt (May 2024 in conftest)
        # being older than the export state's lastExportTime set by the first call.
        first = _post_export(client, {"since": "all"})
        assert first.status_code == 200

        second = _post_export(client, {"since": "last"})
        assert second.status_code == 404
        body = second.get_json()
        assert body.get("error") == "No conversations to export since last export"
        assert body.get("code") == "no_conversations_since_last_export"

    def test_empty_json_body_defaults_to_export_all(
        self, client, export_state_dir
    ):
        response = _post_export(client, {})
        assert response.status_code == 200
        assert response.data.startswith(b"PK")  # ZIP magic
