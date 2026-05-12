"""
API routes for composers — mirrors:
  src/app/api/composers/route.ts       GET /api/composers
  src/app/api/composers/[id]/route.ts  GET /api/composers/<id>
"""

import json
import os
import sqlite3
from contextlib import closing

from flask import Blueprint, jsonify

from utils.workspace_path import resolve_workspace_path
from utils.path_helpers import to_epoch_ms
from utils.workspace_descriptor import _read_json_file

bp = Blueprint("composers", __name__)


@bp.route("/api/composers")
def list_composers():
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
                wd = _read_json_file(wj_path)
                workspace_folder = wd.get("folder")
            except Exception:
                pass

            try:
                # closing() guarantees .close() on scope exit (issue #17).
                with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                    row = conn.execute(
                        "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                    ).fetchone()

                if row and row[0]:
                    data = json.loads(row[0])
                    all_composers = data.get("allComposers")
                    if isinstance(all_composers, list):
                        for c in all_composers:
                            c["conversation"] = c.get("conversation") or []
                            c["workspaceId"] = name
                            c["workspaceFolder"] = workspace_folder
                            composers.append(c)
            except Exception:
                pass

        composers.sort(key=lambda c: to_epoch_ms(c.get("lastUpdatedAt")), reverse=True)
        return jsonify(composers)

    except Exception as e:
        print(f"Failed to get composers: {e}")
        return jsonify({"error": "Failed to get composers"}), 500


@bp.route("/api/composers/<composer_id>")
def get_composer(composer_id):
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
                    data = json.loads(row[0])
                    for c in (data.get("allComposers") or []):
                        if c.get("composerId") == composer_id:
                            return jsonify(c)
            except Exception:
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
                    composer = json.loads(raw)
                    composer.setdefault("conversation", [])
                    return jsonify(composer)
            except Exception:
                pass

        return jsonify({"error": "Composer not found"}), 404

    except Exception as e:
        print(f"Failed to get composer: {e}")
        return jsonify({"error": "Failed to get composer"}), 500
