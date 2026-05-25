"""
API route for search — mirrors src/app/api/search/route.ts
GET /api/search?q=...&type=all|chat|composer
"""

import json
import logging
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from urllib.parse import unquote as _url_unquote

from flask import Blueprint, current_app, jsonify, request

from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.workspace_path import resolve_workspace_path, get_cli_chats_path
from utils.path_helpers import to_epoch_ms, warn_workspace_json_read
from utils.text_extract import extract_text_from_bubble
from utils.cli_chat_reader import list_cli_projects, traverse_blobs, messages_to_bubbles
from models import Bubble, Composer, ParseWarningCollector, SchemaError

bp = Blueprint("search", __name__)
_logger = logging.getLogger(__name__)


def _json_dump_safe(value) -> str:
    """Best-effort JSON string conversion for exclusion matching."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value) if value is not None else ""


def _workspace_display_name_from_folder(folder: str | None, fallback: str | None = None) -> str:
    """Extract a human-readable workspace name from workspace folder path."""
    if folder:
        raw = str(folder).strip()
        cleaned = re.sub(r"^file://", "", raw).replace("\\", "/")
        parts = cleaned.split("/")
        leaf = parts[-1] if parts else ""
        if leaf:
            return _url_unquote(leaf)
    return fallback or "Other chats"


def _build_exclusion_searchable(
    *,
    project_name: str | None,
    chat_title: str | None,
    model_names: list[str] | None = None,
    content_parts: list[str] | None = None,
    metadata_parts: list[str] | None = None,
) -> str:
    """Build broad searchable text so exclusion rules cover visible output."""
    combined: list[str] = []
    if content_parts:
        combined.extend(p for p in content_parts if p)
    if metadata_parts:
        combined.extend(p for p in metadata_parts if p)
    return build_searchable_text(
        project_name=project_name,
        chat_title=chat_title,
        model_names=model_names,
        chat_content_snippet="\n\n".join(combined) if combined else None,
    )


@bp.route("/api/search")
def search():
    try:
        query = request.args.get("q", "").strip()
        search_type = request.args.get("type", "all")
        rules = current_app.config.get("EXCLUSION_RULES") or []

        if not query:
            return jsonify({"error": "No search query provided"}), 400

        workspace_path = resolve_workspace_path()
        results = []
        parse_warnings = ParseWarningCollector()
        query_lower = query.lower()

        global_db_path = os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))

        # ---------------------------------------------------------------
        # Search global cursorDiskKV (new Cursor format — primary source)
        # ---------------------------------------------------------------
        if os.path.isfile(global_db_path):
            # try/finally guarantees .close() on every exit path including
            # exception (issue #17). Equivalent to wrapping the body in
            # `with closing(sqlite3.connect(...))`, without the 160-line
            # indent shift over the search logic that follows.
            conn = None
            try:
                conn = sqlite3.connect(f"file:{global_db_path}?mode=ro", uri=True)
                conn.row_factory = sqlite3.Row

                # Build workspace name map for display
                workspace_entries = []
                ws_id_to_name = {}
                try:
                    for name in os.listdir(workspace_path):
                        full = os.path.join(workspace_path, name)
                        wj = os.path.join(full, "workspace.json")
                        if os.path.isdir(full) and os.path.isfile(wj):
                            workspace_entries.append({"name": name, "workspaceJsonPath": wj})
                            try:
                                with open(wj, "r", encoding="utf-8") as f:
                                    wd = json.load(f)
                                first_folder = wd.get("folder") or (wd.get("folders", [{}])[0] or {}).get("path")
                                if first_folder:
                                    parts = first_folder.replace("\\", "/").split("/")
                                    fn = parts[-1] if parts else None
                                    if fn:
                                        ws_id_to_name[name] = _url_unquote(fn)
                            except Exception as e:
                                warn_workspace_json_read(_logger, name, e)
                except Exception as e:
                    _logger.warning(
                        "Failed to list workspace entries under %s: %s",
                        workspace_path,
                        e,
                    )

                # Build composer → workspace mapping
                composer_id_to_ws = {}
                for entry in workspace_entries:
                    db_path = os.path.join(workspace_path, entry["name"], "state.vscdb")
                    if not os.path.isfile(db_path):
                        continue
                    try:
                        # closing() guarantees .close() on scope exit (issue #17).
                        with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as wconn:
                            row = wconn.execute(
                                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                            ).fetchone()
                        if row and row[0]:
                            data = json.loads(row[0])
                            all_composers = data.get("allComposers")
                            if isinstance(all_composers, list):
                                for c in all_composers:
                                    cid = c.get("composerId") if isinstance(c, dict) else None
                                    if cid:
                                        composer_id_to_ws[cid] = entry["name"]
                    except Exception as e:
                        _logger.warning(
                            "Failed to load composer mapping from workspace %s: %s",
                            entry["name"],
                            e,
                        )

                # Load bubble text for searching
                bubble_map = {}
                for row in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
                    parts = row["key"].split(":")
                    if len(parts) >= 3:
                        bid = parts[2]
                        try:
                            bubble = Bubble.from_dict(json.loads(row["value"]), bubble_id=bid)
                            text = extract_text_from_bubble(bubble.raw)
                            bubble_map[bid] = {"text": text, "raw": bubble.raw}
                        except SchemaError as e:
                            # Drift logged so the operator can see why a chat dropped
                            # out of search results; bad row still skipped so search
                            # keeps returning results from the well-formed ones.
                            _logger.warning(
                                "Schema drift in bubble %s: %s (%s)",
                                bid,
                                e,
                                type(e).__name__,
                            )
                            parse_warnings.record_bubble_skipped()
                        except (json.JSONDecodeError, ValueError):
                            parse_warnings.record_bubble_skipped()

                # Search through composerData
                composer_rows = conn.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND LENGTH(value) > 10"
                ).fetchall()

                for row in composer_rows:
                    composer_id = row["key"].split(":")[1]
                    try:
                        composer = Composer.from_dict(json.loads(row["value"]), composer_id=composer_id)
                    except SchemaError as e:
                        _logger.warning(
                            "Schema drift in composer %s: %s (%s)",
                            composer_id,
                            e,
                            type(e).__name__,
                        )
                        parse_warnings.record_composer_skipped()
                        continue
                    except (json.JSONDecodeError, TypeError, ValueError):
                        parse_warnings.record_composer_skipped()
                        continue
                    try:
                        cd = composer.raw
                        headers = composer.full_conversation_headers_only
                        if not headers:
                            continue

                        title = composer.name or ""
                        ws_id = composer_id_to_ws.get(composer_id, "global")
                        ws_name = ws_id_to_name.get(ws_id)
                        project_name = ws_name or ("Other chats" if ws_id == "global" else ws_id)

                        model_config = composer.model_config
                        model_name = model_config.get("modelName")
                        model_names = [model_name] if model_name and model_name != "default" else None

                        bubble_texts = []
                        bubble_meta = []
                        for header in headers:
                            bid = header.get("bubbleId")
                            bubble_entry = bubble_map.get(bid)
                            if not bubble_entry:
                                continue
                            text = bubble_entry.get("text") or ""
                            if text:
                                bubble_texts.append(text)
                            raw_bubble = bubble_entry.get("raw")
                            if raw_bubble:
                                bubble_meta.append(_json_dump_safe(raw_bubble))

                        exclusion_text = _build_exclusion_searchable(
                            project_name=project_name,
                            chat_title=title,
                            model_names=model_names,
                            content_parts=bubble_texts,
                            metadata_parts=[
                                _json_dump_safe(model_config),
                                _json_dump_safe(cd.get("conversationSummary")),
                                _json_dump_safe(cd.get("usage")),
                                _json_dump_safe(cd.get("requestMetadata")),
                                _json_dump_safe(cd),
                                "\n".join(bubble_meta),
                            ],
                        )
                        if is_excluded_by_rules(rules, exclusion_text):
                            continue

                        # Check if any bubble text matches
                        has_match = False
                        matching_text = ""
                        # Check title
                        if title and query_lower in title.lower():
                            has_match = True
                            matching_text = title

                        # Check bubble texts
                        if not has_match:
                            for text in bubble_texts:
                                if text and query_lower in text.lower():
                                    has_match = True
                                    # Extract a snippet around the match
                                    idx = text.lower().find(query_lower)
                                    start = max(0, idx - 80)
                                    end = min(len(text), idx + len(query) + 120)
                                    matching_text = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                                    break

                        if has_match:
                            if not title:
                                # Derive title from first bubble
                                for text in bubble_texts:
                                    if text:
                                        first_lines = [ln for ln in text.split("\n") if ln.strip()]
                                        if first_lines:
                                            title = first_lines[0][:100]
                                        break
                                if not title:
                                    title = f"Conversation {composer_id[:8]}"

                            results.append({
                                "workspaceId": ws_id,
                                "workspaceFolder": ws_name,
                                "chatId": composer_id,
                                "chatTitle": title,
                                "timestamp": to_epoch_ms(composer.last_updated_at) or to_epoch_ms(composer.created_at) or int(datetime.now().timestamp() * 1000),
                                "matchingText": matching_text,
                                "type": "composer",
                            })
                    except Exception as e:
                        _logger.warning(
                            "Failed to process Composer from composerData:%s during search: %s",
                            composer_id,
                            e,
                        )
                        parse_warnings.record_composer_skipped()

            except Exception:
                _logger.exception("Error searching global storage")
            finally:
                if conn is not None:
                    conn.close()

        # ---------------------------------------------------------------
        # Search per-workspace ItemTable (legacy format — fallback)
        # ---------------------------------------------------------------
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
                workspace_name = _workspace_display_name_from_folder(workspace_folder, fallback=name)

                # try/finally guarantees .close() on every exit path (issue #17).
                conn = None
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)

                    # Search chat logs
                    if search_type in ("all", "chat"):
                        chat_row = conn.execute(
                            "SELECT value FROM ItemTable WHERE [key] = 'workbench.panel.aichat.view.aichat.chatdata'"
                        ).fetchone()
                        if chat_row and chat_row[0]:
                            data = json.loads(chat_row[0])
                            for tab in (data.get("tabs") or []):
                                ct = tab.get("chatTitle") or ""
                                tab_model_names = None
                                tab_meta = tab.get("metadata")
                                if isinstance(tab_meta, dict):
                                    models_used = tab_meta.get("modelsUsed")
                                    if isinstance(models_used, list):
                                        tab_model_names = [str(m) for m in models_used if m]
                                    elif tab_meta.get("model"):
                                        tab_model_names = [str(tab_meta.get("model"))]

                                tab_bubble_texts = []
                                for bubble in (tab.get("bubbles") or []):
                                    text = bubble.get("text") or ""
                                    if text:
                                        tab_bubble_texts.append(text)

                                exclusion_text = _build_exclusion_searchable(
                                    project_name=workspace_name,
                                    chat_title=ct,
                                    model_names=tab_model_names,
                                    content_parts=tab_bubble_texts,
                                    metadata_parts=[
                                        _json_dump_safe(tab),
                                        _json_dump_safe(workspace_folder),
                                    ],
                                )
                                if is_excluded_by_rules(rules, exclusion_text):
                                    continue

                                has_match = False
                                matching_text = ""

                                if ct.lower().find(query_lower) != -1:
                                    has_match = True
                                    matching_text = ct

                                for bubble in (tab.get("bubbles") or []):
                                    text = bubble.get("text") or ""
                                    if text.lower().find(query_lower) != -1:
                                        has_match = True
                                        idx = text.lower().find(query_lower)
                                        start = max(0, idx - 80)
                                        end = min(len(text), idx + len(query) + 120)
                                        matching_text = ("..." if start > 0 else "") + text[start:end] + ("..." if end < len(text) else "")
                                        break

                                if has_match:
                                    results.append({
                                        "workspaceId": name,
                                        "workspaceFolder": workspace_folder,
                                        "chatId": tab.get("tabId"),
                                        "chatTitle": ct or f"Chat {(tab.get('tabId') or '')[:8]}",
                                        "timestamp": tab.get("lastSendTime") or datetime.now().isoformat(),
                                        "matchingText": matching_text,
                                        "type": "chat",
                                    })

                except Exception as e:
                    _logger.warning(
                        "Failed to search legacy workspace %s: %s",
                        name,
                        e,
                    )
                finally:
                    if conn is not None:
                        conn.close()
        except Exception as e:
            _logger.warning(
                "Failed to iterate legacy workspaces under %s: %s",
                workspace_path,
                e,
            )

        # ---------------------------------------------------------------
        # Search Cursor CLI sessions (only for type=all)
        # ---------------------------------------------------------------
        if search_type == "all":
            try:
                cli_projects = list_cli_projects(get_cli_chats_path())
                for cp in cli_projects:
                    ws_name = cp["workspace_name"] or cp["project_id"][:12]
                    for session in cp["sessions"]:
                        meta = session.get("meta", {})
                        session_id = session["session_id"]
                        created_ms: int = meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
                        session_name = meta.get("name") or f"Session {session_id[:8]}"

                        try:
                            messages = traverse_blobs(session["db_path"])
                        except Exception as e:
                            _logger.warning(
                                "Failed to traverse CLI session blobs for %s: %s",
                                session_id,
                                e,
                            )
                            continue

                        bubbles = messages_to_bubbles(messages, created_ms)
                        if not bubbles:
                            continue

                        # Derive title
                        title = session_name
                        if not title or title.startswith("New Agent"):
                            for b in bubbles:
                                if b["type"] == "user" and b.get("text"):
                                    first_lines = [ln for ln in b["text"].split("\n") if ln.strip()]
                                    if first_lines:
                                        title = first_lines[0][:100]
                                    break

                        bubble_texts = [b["text"] for b in bubbles if b.get("text")]
                        tool_payloads = [
                            tc.get("input") or tc.get("summary") or ""
                            for b in bubbles
                            for tc in (b.get("metadata") or {}).get("toolCalls") or []
                        ]
                        exclusion_text = _build_exclusion_searchable(
                            project_name=ws_name,
                            chat_title=title,
                            content_parts=bubble_texts + tool_payloads,
                        )
                        if is_excluded_by_rules(rules, exclusion_text):
                            continue

                        has_match = False
                        matching_text = ""

                        if title and query_lower in title.lower():
                            has_match = True
                            matching_text = title

                        if not has_match:
                            for text in bubble_texts:
                                if text and query_lower in text.lower():
                                    has_match = True
                                    idx = text.lower().find(query_lower)
                                    start = max(0, idx - 80)
                                    end = min(len(text), idx + len(query) + 120)
                                    matching_text = (
                                        ("..." if start > 0 else "")
                                        + text[start:end]
                                        + ("..." if end < len(text) else "")
                                    )
                                    break

                        if has_match:
                            results.append({
                                "workspaceId": f"cli:{cp['project_id']}",
                                "workspaceFolder": cp.get("workspace_path"),
                                "chatId": session_id,
                                "chatTitle": title,
                                "timestamp": created_ms,
                                "matchingText": matching_text,
                                "type": "cli_agent",
                                "source": "cli",
                            })
            except Exception:
                _logger.exception("Error searching CLI sessions")

        # Sort by timestamp descending
        def _ts(r):
            t = r.get("timestamp", 0)
            if isinstance(t, str):
                try:
                    return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
                except Exception:
                    return 0
            return t
        results.sort(key=_ts, reverse=True)

        payload: dict = {"results": results}
        return jsonify(parse_warnings.attach_to(payload))

    except Exception:
        _logger.exception("Search failed")
        return jsonify({"error": "Search failed", "results": []}), 500
