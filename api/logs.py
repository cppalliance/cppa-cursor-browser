"""
API route for logs — mirrors src/app/api/logs/route.ts
GET /api/logs
"""

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

from flask import Blueprint, Response

from api.flask_config import json_response

from utils.workspace_path import resolve_workspace_path
from utils.path_helpers import to_epoch_ms, warn_workspace_json_read

bp = Blueprint("logs", __name__)
_logger = logging.getLogger(__name__)


def _extract_chat_id_from_bubble_key(key: str) -> str | None:
    m = re.match(r"^bubbleId:([^:]+):", key)
    return m.group(1) if m else None


@bp.route("/api/logs")
def get_logs() -> tuple[Response, int] | Response:
    try:
        workspace_path = resolve_workspace_path()
        logs = []

        # Global storage (new Cursor format)
        global_db_path = os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))
        if os.path.isfile(global_db_path):
            try:
                # closing() guarantees .close() on scope exit (issue #17).
                with closing(sqlite3.connect(f"file:{global_db_path}?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'").fetchall()

                chat_map: dict[str, list[Any]] = {}
                for row in rows:
                    chat_id = _extract_chat_id_from_bubble_key(row["key"])
                    if not chat_id:
                        continue
                    try:
                        bubble = json.loads(row["value"])
                        chat_map.setdefault(chat_id, []).append(bubble)
                    except Exception as e:
                        _logger.warning(
                            "Failed to decode bubble row %s: %s",
                            row["key"],
                            e,
                        )

                for chat_id, bubbles in chat_map.items():
                    bubbles = [b for b in bubbles if isinstance(b, dict)]
                    if not bubbles:
                        continue
                    bubbles.sort(key=lambda b: to_epoch_ms(b.get("createdAt") or b.get("timestamp")))
                    first = bubbles[0]
                    last = bubbles[-1]
                    if not first or not last:
                        continue
                    title_text = first.get("text", "") or ""
                    title = title_text.split("\n")[0] if title_text else f"Chat {chat_id[:8]}"
                    logs.append({
                        "id": chat_id,
                        "workspaceId": "global",
                        "workspaceFolder": None,
                        "title": title,
                        "timestamp": to_epoch_ms(last.get("createdAt") or last.get("timestamp")) or int(datetime.now().timestamp() * 1000),
                        "type": "chat",
                        "messageCount": len(bubbles),
                    })
            except Exception:
                _logger.exception("Error reading global storage")

        # Per-workspace (legacy)
        try:
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
                    with open(wj_path, "r", encoding="utf-8") as f:
                        wd = json.load(f)
                    workspace_folder = wd.get("folder")
                except Exception as e:
                    warn_workspace_json_read(_logger, name, e)

                try:
                    # closing() guarantees .close() on scope exit (issue #17).
                    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
                        # Chat logs
                        chat_row = conn.execute(
                            "SELECT value FROM ItemTable WHERE [key] = 'workbench.panel.aichat.view.aichat.chatdata'"
                        ).fetchone()
                        if chat_row and chat_row[0]:
                            data = json.loads(chat_row[0])
                            tabs = data.get("tabs") or []
                            for tab in tabs:
                                logs.append({
                                    "id": tab.get("id", ""),
                                    "workspaceId": name,
                                    "workspaceFolder": workspace_folder,
                                    "title": tab.get("title") or f"Chat {(tab.get('id') or '')[:8]}",
                                    "timestamp": tab.get("timestamp", 0),
                                    "type": "chat",
                                    "messageCount": len(tab.get("bubbles") or []),
                                })

                        # Composer logs
                        comp_row = conn.execute(
                            "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                        ).fetchone()
                        if comp_row and comp_row[0]:
                            data = json.loads(comp_row[0])
                            for c in (data.get("allComposers") or []):
                                logs.append({
                                    "id": c.get("composerId", ""),
                                    "workspaceId": name,
                                    "workspaceFolder": workspace_folder,
                                    "title": c.get("text") or f"Composer {(c.get('composerId') or '')[:8]}",
                                    "timestamp": to_epoch_ms(c.get("lastUpdatedAt")) or to_epoch_ms(c.get("createdAt")) or 0,
                                    "type": "composer",
                                    "messageCount": len(c.get("conversation") or []),
                                })
                except Exception as e:
                    _logger.warning(
                        "Failed to read logs from workspace %s: %s",
                        name,
                        e,
                    )
        except Exception as e:
            _logger.warning(
                "Failed to iterate workspaces under %s: %s",
                workspace_path,
                e,
            )

        logs.sort(key=lambda log: log.get("timestamp") or 0, reverse=True)
        return json_response({"logs": logs})

    except Exception:
        _logger.exception("Failed to get logs")
        return json_response({"error": "Failed to get logs", "logs": []}, 500)