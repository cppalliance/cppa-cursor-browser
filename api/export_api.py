"""
API route for export — produces per-chat Markdown in a zip download.
POST /api/export  { since: "all"|"last", zip: true }
GET  /api/export/state — returns last export time
"""

import io
import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Blueprint, Response, current_app, jsonify, request

from utils.workspace_path import resolve_workspace_path
from utils.path_helpers import normalize_file_path, get_workspace_folder_paths, to_epoch_ms
from utils.text_extract import extract_text_from_bubble
from utils.tool_parser import parse_tool_call
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules

bp = Blueprint("export_api", __name__)


def _get_state_dir() -> str:
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def _get_export_state() -> dict:
    """Read the export state file."""
    state_path = os.path.join(_get_state_dir(), "export_state.json")
    if os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_export_state(count: int):
    """Save export state after an export."""
    state_dir = _get_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    state = {
        "lastExportTime": datetime.now().isoformat(),
        "exportedCount": count,
    }
    state_path = os.path.join(state_dir, "export_state.json")
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def _slug(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s or "")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s[:80] or "untitled"


@bp.route("/api/export/state")
def get_export_state():
    """Return the last export timestamp."""
    state = _get_export_state()
    return jsonify(state)


@bp.route("/api/export", methods=["POST"])
def export_chats():
    """Export chats as a zip archive.

    Exclusion rules (``EXCLUSION_RULES`` app config key) are evaluated against
    each chat's project name, title, and model.  Rules are loaded once at
    application startup; an app restart is required to pick up changes to the
    exclusion rules file.
    """
    try:
        body = request.get_json(silent=True) or {}
        since = "last" if body.get("since") == "last" else "all"

        workspace_path = resolve_workspace_path()
        global_db_path = os.path.normpath(
            os.path.join(workspace_path, "..", "globalStorage", "state.vscdb")
        )

        if not os.path.isfile(global_db_path):
            return jsonify({"error": "Cursor global storage not found"}), 404

        # Determine last export timestamp for filtering
        last_export_ms = 0
        if since == "last":
            state = _get_export_state()
            ts_str = state.get("lastExportTime")
            if ts_str:
                last_export_ms = to_epoch_ms(ts_str)

        conn = sqlite3.connect(f"file:{global_db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        # Build workspace mapping
        workspace_entries = []
        ws_id_to_slug = {}
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            wj = os.path.join(full, "workspace.json")
            if os.path.isdir(full) and os.path.isfile(wj):
                workspace_entries.append({"name": name, "path": wj})
                try:
                    with open(wj, "r", encoding="utf-8") as f:
                        wd = json.load(f)
                    first_folder = wd.get("folder") or (wd.get("folders", [{}])[0] or {}).get("path")
                    if first_folder:
                        fn = first_folder.replace("\\", "/").split("/")[-1]
                        if fn:
                            ws_id_to_slug[name] = _slug(fn)
                except Exception:
                    pass

        # Build composer → workspace from per-workspace dbs
        composer_id_to_ws = {}
        for entry in workspace_entries:
            db_path = os.path.join(workspace_path, entry["name"], "state.vscdb")
            if not os.path.isfile(db_path):
                continue
            try:
                wconn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                row = wconn.execute(
                    "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
                ).fetchone()
                if row and row[0]:
                    data = json.loads(row[0])
                    for c in (data.get("allComposers") or []):
                        cid = c.get("composerId") if isinstance(c, dict) else None
                        if cid:
                            composer_id_to_ws[cid] = entry["name"]
                wconn.close()
            except Exception:
                pass

        # Load bubble data for text extraction
        bubble_map = {}
        for row in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            parts = row["key"].split(":")
            if len(parts) >= 3:
                bid = parts[2]
                try:
                    b = json.loads(row["value"])
                    if isinstance(b, dict):
                        bubble_map[bid] = b
                except Exception:
                    pass

        # Process composers
        composer_rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            " AND value LIKE '%fullConversationHeadersOnly%'"
            " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
        ).fetchall()

        today = datetime.now().strftime("%Y-%m-%d")
        exported = []
        rules = current_app.config.get("EXCLUSION_RULES") or []

        for row in composer_rows:
            composer_id = row["key"].split(":")[1]
            try:
                cd = json.loads(row["value"])
                headers = cd.get("fullConversationHeadersOnly") or []
                if not headers:
                    continue

                updated_at_ms = to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0
                if since == "last" and updated_at_ms and updated_at_ms <= last_export_ms:
                    continue

                ws_id = composer_id_to_ws.get(composer_id, "global")
                ws_slug = "other-chats" if ws_id == "global" else (ws_id_to_slug.get(ws_id) or _slug(ws_id[:12]))
                title = cd.get("name") or f"Chat {composer_id[:8]}"
                model_config = cd.get("modelConfig") or {}
                model_name = model_config.get("modelName")
                model_names = [model_name] if model_name and model_name != "default" else None
                searchable = build_searchable_text(
                    project_name=ws_slug,
                    chat_title=title,
                    model_names=model_names,
                )
                if is_excluded_by_rules(rules, searchable):
                    continue
                title_slug = _slug(title)
                ts_ms = updated_at_ms or int(datetime.now().timestamp() * 1000)
                ts_str = datetime.fromtimestamp(ts_ms / 1000).strftime("%Y-%m-%dT%H-%M-%S")
                filename = f"{ts_str}__{title_slug}__{composer_id[:8]}.md"
                rel_path = os.path.join(today, ws_slug, "chat", filename)

                # Build markdown content
                bubbles = []
                for h in headers:
                    bid = h.get("bubbleId")
                    b = bubble_map.get(bid)
                    if not b:
                        continue
                    text = extract_text_from_bubble(b)
                    has_tool = isinstance(b.get("toolFormerData"), dict)
                    has_thinking = bool(b.get("thinking"))
                    if not text.strip() and not has_tool and not has_thinking:
                        continue
                    if not text.strip() and has_tool:
                        text = f"**Tool: {b['toolFormerData'].get('name', 'unknown')}**"

                    btype = "user" if h.get("type") == 1 else "assistant"
                    bubble_ts = to_epoch_ms(b.get("createdAt")) or to_epoch_ms(b.get("timestamp")) or 0

                    thinking = None
                    thinking_duration_ms = None
                    if b.get("thinking"):
                        thinking = b["thinking"] if isinstance(b["thinking"], str) else (
                            b["thinking"].get("text") if isinstance(b["thinking"], dict) else None
                        )
                        thinking_duration_ms = b.get("thinkingDurationMs")

                    # Full tool call parsing with input/output
                    tool_info = None
                    if has_tool:
                        tool_info = parse_tool_call(b["toolFormerData"])

                    # Per-bubble model info
                    model_info = (b.get("modelInfo") or {}).get("modelName")
                    if model_info == "default":
                        model_info = None

                    # Context window from user bubbles
                    ctx_window = b.get("contextWindowStatusAtCreation") or {}
                    ctx_tokens_used = ctx_window.get("tokensUsed", 0)
                    ctx_token_limit = ctx_window.get("tokenLimit", 0)
                    ctx_pct_remaining = ctx_window.get("percentageRemainingFloat") or ctx_window.get("percentageRemaining")

                    bubbles.append({
                        "type": btype,
                        "text": text,
                        "timestamp": bubble_ts,
                        "thinking": thinking,
                        "thinkingDurationMs": thinking_duration_ms,
                        "tool": tool_info,
                        "model": model_info,
                        "contextTokensUsed": ctx_tokens_used if ctx_tokens_used > 0 else None,
                        "contextTokenLimit": ctx_token_limit if ctx_token_limit > 0 else None,
                        "contextPctRemaining": round(ctx_pct_remaining, 1) if ctx_pct_remaining else None,
                    })

                bubbles.sort(key=lambda x: x["timestamp"] or 0)

                # Compute response times
                last_user_ts = None
                for b_item in bubbles:
                    if b_item["type"] == "user":
                        last_user_ts = b_item.get("timestamp")
                    elif b_item["type"] == "assistant" and last_user_ts:
                        bts = b_item.get("timestamp")
                        if bts and bts > last_user_ts:
                            b_item["responseTimeMs"] = bts - last_user_ts

                # Aggregated metrics
                total_response_ms = sum(b_item.get("responseTimeMs", 0) for b_item in bubbles)
                total_thinking_ms = sum(b_item.get("thinkingDurationMs", 0) or 0 for b_item in bubbles)
                total_tool_calls = sum(1 for b_item in bubbles if b_item.get("tool"))
                lines_added = cd.get("totalLinesAdded", 0)
                lines_removed = cd.get("totalLinesRemoved", 0)
                files_added = cd.get("addedFiles", 0)
                files_removed = cd.get("removedFiles", 0)
                max_ctx_used = max((b_item.get("contextTokensUsed", 0) or 0) for b_item in bubbles) if bubbles else 0
                ctx_limit = max((b_item.get("contextTokenLimit", 0) or 0) for b_item in bubbles) if bubbles else 0

                # Build frontmatter
                created_ms = to_epoch_ms(cd.get("createdAt")) or ts_ms
                md = "---\n"
                md += f"log_id: {composer_id}\n"
                md += f"title: {title}\n"
                md += f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}\n"
                md += f"updated_at: {datetime.fromtimestamp(updated_at_ms / 1000).isoformat() if updated_at_ms else datetime.now().isoformat()}\n"
                md += f"workspace: {ws_slug}\n"
                md += f"message_count: {len(bubbles)}\n"
                if model_name:
                    md += f"model: {model_name}\n"
                if total_response_ms:
                    md += f"total_response_time_sec: {total_response_ms / 1000:.1f}\n"
                if total_thinking_ms:
                    md += f"total_thinking_time_sec: {total_thinking_ms / 1000:.1f}\n"
                if total_tool_calls:
                    md += f"total_tool_calls: {total_tool_calls}\n"
                if max_ctx_used and ctx_limit:
                    md += f"max_context_tokens_used: {max_ctx_used}\n"
                    md += f"context_token_limit: {ctx_limit}\n"
                if lines_added or lines_removed:
                    md += f"lines_added: {lines_added}\n"
                    md += f"lines_removed: {lines_removed}\n"
                if files_added or files_removed:
                    md += f"files_added: {files_added}\n"
                    md += f"files_removed: {files_removed}\n"
                md += "---\n\n"
                md += f"# {title}\n\n"
                md += f"_Created: {datetime.fromtimestamp(created_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
                md += "---\n\n"

                for bubble in bubbles:
                    role_label = "User" if bubble["type"] == "user" else "Assistant"
                    md += f"### {role_label}\n\n"
                    # Bubble metadata line
                    meta_parts = []
                    if bubble.get("model"):
                        meta_parts.append(f"Model: {bubble['model']}")
                    if bubble.get("responseTimeMs"):
                        meta_parts.append(f"Response: {bubble['responseTimeMs'] / 1000:.1f}s")
                    if bubble.get("thinkingDurationMs"):
                        meta_parts.append(f"Thinking: {bubble['thinkingDurationMs'] / 1000:.1f}s")
                    if bubble.get("contextTokensUsed") and bubble.get("contextTokenLimit"):
                        pct = bubble["contextTokensUsed"] / bubble["contextTokenLimit"] * 100
                        meta_parts.append(f"Context: {bubble['contextTokensUsed']:,} / {bubble['contextTokenLimit']:,} tokens ({pct:.0f}% used)")
                    elif bubble.get("contextPctRemaining") is not None:
                        meta_parts.append(f"Context: {bubble['contextPctRemaining']}% remaining")
                    if meta_parts:
                        md += f"_{' | '.join(meta_parts)}_\n\n"
                    if bubble["timestamp"]:
                        md += f"_{datetime.fromtimestamp(bubble['timestamp'] / 1000).strftime('%Y-%m-%d %H:%M:%S')}_\n\n"
                    if bubble.get("thinking"):
                        dur_str = f" ({bubble['thinkingDurationMs'] / 1000:.1f}s)" if bubble.get("thinkingDurationMs") else ""
                        md += f"<details><summary>Thinking{dur_str}</summary>\n\n{bubble['thinking']}\n\n</details>\n\n"
                    md += bubble["text"] + "\n\n"
                    # Full tool call with input/output
                    if bubble.get("tool"):
                        t = bubble["tool"]
                        tool_name = t.get("name") or "unknown"
                        tool_status = t.get("status") or ""
                        tool_summary = t.get("summary") or tool_name
                        status_str = f" ({tool_status})" if tool_status else ""
                        md += f"> **Tool: {tool_summary}**{status_str}\n"
                        if t.get("input"):
                            md += f">\n> **INPUT:**\n> ```\n"
                            for iline in str(t["input"]).split("\n"):
                                md += f"> {iline}\n"
                            md += f"> ```\n"
                        if t.get("output"):
                            md += f">\n> **OUTPUT:**\n> ```\n"
                            for oline in str(t["output"]).split("\n"):
                                md += f"> {oline}\n"
                            md += f"> ```\n"
                        md += "\n"
                    md += "---\n\n"

                exported.append({"path": rel_path, "content": md, "updatedAt": updated_at_ms})

            except Exception as e:
                print(f"Error processing composer {composer_id} for export: {e}")

        conn.close()

        count = len(exported)
        if count == 0:
            return jsonify({"error": "No conversations to export" + (
                " since last export" if since == "last" else ""
            )}), 404

        # Build zip in memory
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["path"], entry["content"])

        buf.seek(0)

        # Save export state
        _save_export_state(count)

        filename = "cursor-export.zip"
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Export-Count": str(count),
            },
        )

    except Exception as e:
        print(f"Export error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": f"Export failed: {str(e)}"}), 500
