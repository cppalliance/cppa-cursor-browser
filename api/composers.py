"""
API routes for composers — mirrors:
  src/app/api/composers/route.ts       GET /api/composers
  src/app/api/composers/[id]/route.ts  GET /api/composers/<id>
"""

import json
import logging
import os
import sqlite3
from contextlib import closing
from typing import Any

from flask import Blueprint, Response

from api.flask_config import api_error, json_response

from utils.workspace_path import resolve_workspace_path
from utils.path_helpers import to_epoch_ms
from models import Composer, SchemaError, Workspace, WorkspaceLocalComposer

bp = Blueprint("composers", __name__)
_logger = logging.getLogger(__name__)


def _read_json_file(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _parse_workspace_composer_rows(data: Any) -> list[Any]:
    """Validate ``composer.composerData`` envelope and return ``allComposers``."""
    if not isinstance(data, dict):
        raise SchemaError(
            "WorkspaceComposers",
            "composer.composerData",
            hint=f"expected object, got {type(data).__name__}",
        )
    if "allComposers" not in data:
        raise SchemaError("WorkspaceComposers", "allComposers")
    all_composers = data.get("allComposers")
    if not isinstance(all_composers, list):
        raise SchemaError(
            "WorkspaceComposers",
            "allComposers",
            hint=f"expected list, got {type(all_composers).__name__}",
        )
    return all_composers

@bp.route("/api/composers")
def list_composers() -> tuple[Response, int] | Response:
    """List all composers across workspace databases (GET /api/composers).

    Returns:
        JSON array of composer dicts sorted by ``lastUpdatedAt`` descending.
        500 on failure.
    """
    try:
        workspace_path = resolve_workspace_path()
        composers = []

        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if not os.path.isdir(full):
                continue

            db_path = os.path.join(full, "state.vscdb")
            wj_path = os.path.join(full, "workspace.json")
            if not os.path.isfile(db_path):
                continue

            workspace_folder = None
            try:
                workspace = Workspace.from_dict(_read_json_file(wj_path), workspace_id=name)
                workspace_folder = workspace.folder
            except (SchemaError, OSError, ValueError):
                # Missing / malformed workspace.json is non-fatal — the row still
                # contributes its composer data, just without a folder hint.
                pass

            try:
                # closing() guarantees .close() on scope exit (issue #17).
                with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                    ).fetchone()

                if row and row[0]:
                    all_composers = _parse_workspace_composer_rows(json.loads(row[0]))
                    for c in all_composers:
                        try:
                            local = WorkspaceLocalComposer.from_dict(c)
                        except SchemaError as e:
                            _logger.warning(
                                "Schema drift in %s: %s (%s)",
                                db_path,
                                e,
                                type(e).__name__,
                            )
                            continue
                        # Use the typed view downstream so the dataclass is
                        # load-bearing, not just a filter (Brad's review): the
                        # sort key and the JSON's composerId both read off the
                        # validated values, not the raw dict.
                        payload = local.cursor_storage_payload()
                        payload["composerId"] = local.composer_id
                        payload["lastUpdatedAt"] = local.last_updated_at
                        payload["conversation"] = payload.get("conversation") or []
                        payload["workspaceId"] = name
                        payload["workspaceFolder"] = workspace_folder
                        composers.append((local, payload))
            except SchemaError as e:
                _logger.warning(
                    "Schema drift in %s: %s (%s)",
                    db_path,
                    e,
                    type(e).__name__,
                )
            except Exception as e:
                _logger.error(
                    "Failed reading composers from %s: %s (%s)",
                    db_path,
                    e,
                    type(e).__name__,
                    exc_info=True,
                )

        composers.sort(key=lambda pair: to_epoch_ms(pair[0].last_updated_at), reverse=True)
        return json_response([c for _, c in composers])

    except Exception:
        _logger.exception("Failed to get composers")
        return api_error("Failed to get composers", "composers_list_failed", 500)


@bp.route("/api/composers/<composer_id>")
def get_composer(composer_id: str) -> tuple[Response, int] | Response:
    """Fetch one composer by ID (GET /api/composers/<composer_id>).

    Args:
        composer_id: Composer UUID.

    Returns:
        Composer JSON from per-workspace storage or global fallback. Per-workspace
        schema drift is logged and skipped before global fallback is attempted.
        404 when the composer is absent from both stores (``{"error": "Composer not found"}``)
        or when the global row fails validation (``{"error": "Composer schema drift"}``).
        500 on unexpected failure.
    """
    try:
        workspace_path = resolve_workspace_path()

        # Search per-workspace databases
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if not os.path.isdir(full):
                continue
            db_path = os.path.join(full, "state.vscdb")
            if not os.path.isfile(db_path):
                continue

            try:
                # closing() guarantees .close() on scope exit (issue #17).
                with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                    ).fetchone()

                if row and row[0]:
                    all_composers = _parse_workspace_composer_rows(json.loads(row[0]))
                    for c in all_composers:
                        if isinstance(c, dict) and c.get("composerId") == composer_id:
                            try:
                                local = WorkspaceLocalComposer.from_dict(c)
                            except SchemaError as e:
                                # Same drift list_composers() logs and skips at line ~78,
                                # so a single-composer fetch can't silently return malformed
                                # JSON the list endpoint hid.
                                _logger.warning(
                                    "Schema drift in workspace-local composer %s: %s (%s)",
                                    composer_id,
                                    e,
                                    type(e).__name__,
                                )
                                continue
                            # Match list_composers() at line 89 and the global
                            # fallback below: `conversation` is normalised to []
                            # whether it's absent or None, so the response shape
                            # is identical regardless of which branch resolved
                            # the composer (CodeRabbit on PR #30).
                            payload = local.cursor_storage_payload()
                            payload["conversation"] = payload.get("conversation") or []
                            return json_response(payload)
            except SchemaError as e:
                _logger.warning(
                    "Schema drift in %s: %s (%s)",
                    db_path,
                    e,
                    type(e).__name__,
                )
            except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError):
                pass

        # Fallback: global storage
        global_db_path = os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))
        if os.path.isfile(global_db_path):
            try:
                # closing() guarantees .close() on scope exit (issue #17).
                with closing(sqlite3.connect(f"file:{global_db_path}?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT value FROM cursorDiskKV WHERE key = ?",
                        (f"composerData:{composer_id}",),
                    ).fetchone()

                if row and row[0]:
                    raw = row[0] if isinstance(row[0], str) else row[0].decode("utf-8")
                    try:
                        composer = Composer.from_dict(json.loads(raw), composer_id=composer_id)
                    except SchemaError as e:
                        # Don't return malformed JSON to the client — surface the drift
                        # as a 404 + log, matching the silent-skip behaviour of the
                        # list endpoints for the same row.
                        _logger.warning(
                            "Schema drift in composer %s: %s (%s)",
                            composer_id,
                            e,
                            type(e).__name__,
                        )
                        return api_error("Composer schema drift", "composer_schema_drift", 404)
                    payload = composer.cursor_storage_payload()
                    payload["conversation"] = payload.get("conversation") or []
                    return json_response(payload)
            except (OSError, sqlite3.Error, json.JSONDecodeError, ValueError):
                pass

        return api_error("Composer not found", "composer_not_found", 404)
    except Exception:
        _logger.exception("Failed to get composer")
        return api_error("Failed to get composer", "composer_get_failed", 500)