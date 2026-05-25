from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime
from typing import Any

_logger = logging.getLogger(__name__)

from utils.path_helpers import (
    get_workspace_folder_paths,
    normalize_file_path,
    to_epoch_ms,
)
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.text_extract import extract_text_from_bubble
from utils.tool_parser import parse_tool_call as _parse_tool_call
from utils.workspace_descriptor import read_json_file
from models import Bubble, Composer, SchemaError
from services.workspace_db import (
    _build_composer_id_to_workspace_id,
    _collect_invalid_workspace_ids,
    _collect_workspace_entries,
    load_code_block_diff_map,
    _open_global_db,
)
from services.workspace_resolver import (
    _create_project_name_to_workspace_id_map,
    _create_workspace_path_to_id_map,
    _determine_project_for_conversation,
    _get_workspace_display_name,
    _infer_invalid_workspace_aliases,
)



def _try_loads_kv_value(raw: str | None) -> Any | None:
    """Parse a cursorDiskKV ``value`` column; ``None`` on missing or unparseable input (no raise)."""
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def assemble_workspace_tabs(
    workspace_id: str,
    workspace_path: str,
    rules: list,
) -> tuple[dict, int]:
    """Build (payload, status) for GET /api/workspaces/<id>/tabs; status=404 if global storage is missing."""
    response: dict = {"tabs": []}

    workspace_entries = _collect_workspace_entries(workspace_path)
    invalid_workspace_ids = _collect_invalid_workspace_ids(workspace_entries)
    project_name_map = _create_project_name_to_workspace_id_map(workspace_entries)
    workspace_path_map = _create_workspace_path_to_id_map(workspace_entries)
    composer_id_to_ws = _build_composer_id_to_workspace_id(workspace_path, workspace_entries)

    # Build set of all workspace IDs that share the same folder as workspace_id
    # (handles Cursor creating multiple workspace entries for the same project)
    matching_ws_ids = {workspace_id}
    if workspace_id != "global":
        target_folder = ""
        wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
        try:
            wd = read_json_file(wj_path)
            folders = get_workspace_folder_paths(wd)
            first_folder = folders[0] if folders else None
            if first_folder:
                target_folder = normalize_file_path(first_folder)
        except Exception as e:
            _logger.warning(
                "Failed to read workspace.json for %s: %s",
                workspace_id,
                e,
            )
        if target_folder:
            for entry in workspace_entries:
                try:
                    wd2 = read_json_file(entry["workspaceJsonPath"])
                    folders2 = get_workspace_folder_paths(wd2)
                    f2 = folders2[0] if folders2 else None
                    if f2 and normalize_file_path(f2) == target_folder:
                        matching_ws_ids.add(entry["name"])
                except Exception as e:
                    _logger.warning(
                        "Failed to read workspace.json for %s: %s",
                        entry["name"],
                        e,
                    )

    bubble_map: dict[str, dict] = {}
    code_block_diff_map: dict[str, list] = {}
    message_request_context_map: dict[str, list] = {}

    with _open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": "Global storage not found"}, 404

        workspace_display_name = _get_workspace_display_name(workspace_path, workspace_id)

        def _safe_fetchall(query: str, params: tuple = ()) -> list:
            try:
                return global_db.execute(query, params).fetchall()
            except sqlite3.Error:
                return []

        # Load bubbles
        for row in _safe_fetchall("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
            parts = row["key"].split(":")
            if len(parts) >= 3:
                bid = parts[2]
                try:
                    parsed = json.loads(row["value"])
                except json.JSONDecodeError as e:
                    _logger.warning(
                        "Failed to decode Bubble from %s: %s (value: %r)",
                        row["key"],
                        e,
                        row["value"],
                    )
                    continue
                try:
                    bubble_obj = Bubble.from_dict(parsed, bubble_id=bid)
                    bubble_map[bid] = bubble_obj.raw
                except SchemaError as e:
                    # Drift logged so the operator can chase disappearing
                    # bubbles instead of guessing. Bad row still skipped so the
                    # tabs endpoint can't 500 on one malformed bubble.
                    _logger.warning(
                        "Failed to parse Bubble from bubbleId:%s: %s",
                        bid,
                        e,
                    )

        # Load codeBlockDiffs
        code_block_diff_map = load_code_block_diff_map(global_db)

        # Load messageRequestContext rows once; build both
        # message_request_context_map and project_layouts_map from the same pass.
        project_layouts_map: dict[str, list] = {}
        for row in _safe_fetchall("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"):
            parts = row["key"].split(":")
            if len(parts) < 2:
                continue
            chat_id = parts[1]
            ctx = _try_loads_kv_value(row["value"])
            if not isinstance(ctx, dict):
                continue

            # Per-bubble context map (needs the contextId at parts[2])
            if len(parts) >= 3:
                context_id = parts[2]
                message_request_context_map.setdefault(chat_id, []).append({
                    **ctx,
                    "contextId": context_id,
                })

            # Project-layout map (root paths used by the resolver)
            layouts = ctx.get("projectLayouts")
            if isinstance(layouts, list):
                project_layouts_map.setdefault(chat_id, [])
                for layout in layouts:
                    if isinstance(layout, str):
                        layout = _try_loads_kv_value(layout)
                        if not isinstance(layout, dict):
                            continue
                    if isinstance(layout, dict) and layout.get("rootPath"):
                        project_layouts_map[chat_id].append(layout["rootPath"])

        # Get composer data entries with conversations
        composer_rows = _safe_fetchall(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            " AND value LIKE '%fullConversationHeadersOnly%'"
            " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
        )

        invalid_workspace_aliases = _infer_invalid_workspace_aliases(
            composer_rows=composer_rows,
            project_layouts_map=project_layouts_map,
            project_name_map=project_name_map,
            workspace_path_map=workspace_path_map,
            workspace_entries=workspace_entries,
            bubble_map=bubble_map,
            composer_id_to_ws=composer_id_to_ws,
            invalid_workspace_ids=invalid_workspace_ids,
        )

        for row in composer_rows:
            composer_id = row["key"].split(":")[1]
            try:
                parsed = json.loads(row["value"])
            except json.JSONDecodeError as e:
                _logger.warning(
                    "Failed to decode Composer from composerData:%s: %s (value: %r)",
                    composer_id,
                    e,
                    row["value"],
                )
                continue
            try:
                composer = Composer.from_dict(parsed, composer_id=composer_id)
            except SchemaError as e:
                # Drift skipped + logged so the two primary conversation
                # paths (list_workspaces + get_workspace_tabs) agree on what
                # counts as a valid composer.
                _logger.warning(
                    "Failed to parse Composer from composerData:%s: %s",
                    composer_id,
                    e,
                )
                continue
            try:
                cd = composer.raw

                # Determine project
                pid = _determine_project_for_conversation(
                    cd, composer_id, project_layouts_map,
                    project_name_map, workspace_path_map,
                    workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids,
                )
                mapped_ws = composer_id_to_ws.get(composer_id)
                if not pid and mapped_ws in invalid_workspace_ids:
                    pid = invalid_workspace_aliases.get(mapped_ws)
                assigned = pid if pid else "global"

                if assigned not in matching_ws_ids:
                    continue

                headers = cd.get("fullConversationHeadersOnly") or []

                # Build bubbles. Annotated as list[dict[str, Any]] so mypy
                # treats nested .get("metadata") / m["inputTokens"] etc. as
                # accessing dict values rather than `object`.
                bubbles: list[dict[str, Any]] = []
                for header in headers:
                    if not isinstance(header, dict):
                        continue
                    bubble_id = header.get("bubbleId")
                    bubble = bubble_map.get(bubble_id)
                    if not bubble:
                        continue

                    is_user = header.get("type") == 1
                    msg_type = "user" if is_user else "ai"
                    text = extract_text_from_bubble(bubble)

                    # Append messageRequestContext info
                    context_text = ""
                    for ctx in message_request_context_map.get(composer_id, []):
                        if ctx.get("bubbleId") == bubble_id:
                            if ctx.get("gitStatusRaw"):
                                context_text += f"\n\n**Git Status:**\n```\n{ctx['gitStatusRaw']}\n```"
                            tf = ctx.get("terminalFiles")
                            if isinstance(tf, list) and tf:
                                context_text += "\n\n**Terminal Files:**"
                                for f in tf:
                                    if not isinstance(f, dict):
                                        continue
                                    context_text += f"\n- {f.get('path', '')}"
                            af = ctx.get("attachedFoldersListDirResults")
                            if isinstance(af, list) and af:
                                context_text += "\n\n**Attached Folders:**"
                                for fld in af:
                                    if not isinstance(fld, dict):
                                        continue
                                    files = fld.get("files")
                                    if isinstance(files, list) and files:
                                        context_text += f"\n\n**Folder:** {fld.get('path', 'Unknown')}"
                                        for fi in files:
                                            if not isinstance(fi, dict):
                                                continue
                                            context_text += f"\n- {fi.get('name', '')} ({fi.get('type', '')})"
                            cr = ctx.get("cursorRules")
                            if isinstance(cr, list) and cr:
                                context_text += "\n\n**Cursor Rules:**"
                                for rule in cr:
                                    if not isinstance(rule, dict):
                                        continue
                                    context_text += f"\n- {rule.get('name') or rule.get('description') or 'Rule'}"
                            sc = ctx.get("summarizedComposers")
                            if isinstance(sc, list) and sc:
                                context_text += "\n\n**Related Conversations:**"
                                for comp in sc:
                                    if not isinstance(comp, dict):
                                        continue
                                    context_text += f"\n- {comp.get('name') or comp.get('composerId') or 'Conversation'}"

                    full_text = text + context_text

                    raw = bubble
                    token_count = raw.get("tokenCount")

                    # Tool calls
                    tool_calls = None
                    tfd = raw.get("toolFormerData")
                    if isinstance(tfd, dict):
                        tool_call = _parse_tool_call(tfd)
                        if isinstance(tool_call, dict):
                            tool_calls = [tool_call]

                    # Thinking
                    thinking = None
                    thinking_duration_ms = None
                    if raw.get("thinking"):
                        thinking = raw["thinking"] if isinstance(raw["thinking"], str) else (raw["thinking"].get("text") if isinstance(raw["thinking"], dict) else None)
                        thinking_duration_ms = raw.get("thinkingDurationMs")

                    has_content = full_text.strip() or tool_calls or thinking
                    if not has_content:
                        continue

                    # Context window
                    ctx_window = raw.get("contextWindowStatusAtCreation") or {}
                    ctx_pct = ctx_window.get("percentageRemainingFloat") or ctx_window.get("percentageRemaining")

                    # Display text fallbacks
                    display_text = full_text.strip()
                    if not display_text and tool_calls:
                        tc = tool_calls[0]
                        if isinstance(tc, dict):
                            display_text = f"**Tool: {tc.get('name', 'unknown')}**"
                            if tc.get("status"):
                                display_text += f" ({tc['status']})"
                    if not display_text and thinking:
                        display_text = thinking

                    # Build metadata for BOTH user and AI bubbles
                    bubble_meta = None
                    if bubble:
                        model_info = raw.get("modelInfo") or {}
                        model_name = model_info.get("modelName")
                        if model_name == "default":
                            model_name = None

                        if msg_type == "ai":
                            tc_dict = token_count if isinstance(token_count, dict) else {}
                            in_tok = tc_dict.get("inputTokens") or 0
                            out_tok = tc_dict.get("outputTokens") or 0
                            cached_tok = tc_dict.get("cachedTokens") or 0
                            bubble_meta = {
                                "modelName": model_name,
                                "inputTokens": in_tok if in_tok > 0 else None,
                                "outputTokens": out_tok if out_tok > 0 else None,
                                "cachedTokens": cached_tok if cached_tok > 0 else None,
                                "toolResultsCount": (len(tool_calls) if tool_calls else None) or (len(raw["toolResults"]) if isinstance(raw.get("toolResults"), list) and raw["toolResults"] else None),
                                "toolResults": raw.get("toolResults") if isinstance(raw.get("toolResults"), list) and raw["toolResults"] else None,
                                "toolCalls": tool_calls,
                                "thinking": thinking,
                                "thinkingDurationMs": thinking_duration_ms,
                                "contextWindowPercent": ctx_pct,
                            }
                        elif msg_type == "user":
                            bubble_meta = {
                                "modelName": model_name,
                                "contextWindowPercent": ctx_pct,
                            }
                            if ctx_window:
                                tokens_used = ctx_window.get("tokensUsed", 0)
                                token_limit = ctx_window.get("tokenLimit", 0)
                                if tokens_used > 0:
                                    bubble_meta["contextTokensUsed"] = tokens_used
                                if token_limit > 0:
                                    bubble_meta["contextTokenLimit"] = token_limit

                        if bubble_meta:
                            bubble_meta = {k: v for k, v in bubble_meta.items() if v is not None}
                            if not bubble_meta:
                                bubble_meta = None

                    b_entry = {
                        "type": msg_type,
                        "text": display_text,
                        "timestamp": to_epoch_ms(bubble.get("createdAt")) or to_epoch_ms(bubble.get("timestamp")) or int(datetime.now().timestamp() * 1000),
                    }
                    if bubble_meta:
                        b_entry["metadata"] = bubble_meta
                    bubbles.append(b_entry)

                if not bubbles:
                    continue

                # Title
                title = cd.get("name") or f"Conversation {composer_id[:8]}"
                if not cd.get("name") and bubbles:
                    first_msg = bubbles[0].get("text", "")
                    if first_msg:
                        first_lines = [ln for ln in first_msg.split("\n") if ln.strip()]
                        if first_lines:
                            title = first_lines[0][:100]
                            if len(title) == 100:
                                title += "..."

                # Early exclusion check — before expensive metadata aggregation
                _early_model_config = cd.get("modelConfig") or {}
                _early_model_name = _early_model_config.get("modelName")
                _early_model_names = [_early_model_name] if _early_model_name and _early_model_name != "default" else None
                if is_excluded_by_rules(rules, build_searchable_text(
                    project_name=workspace_display_name,
                    chat_title=title,
                    model_names=_early_model_names,
                )):
                    continue

                # codeBlockDiffs are emitted as a structured ``tab.codeBlockDiffs``
                # field below; the dashboard reads them from there (download.js,
                # workspace.html). Previously this loop also pushed a synthetic
                # ``Tool Action`` AI bubble into ``tab.bubbles``, double-representing
                # every diff on the wire and forcing a ``synthetic`` filter in the
                # response-time pass. Dropping the synthesis — frontend never read it.
                diffs = code_block_diff_map.get(composer_id, [])

                bubbles.sort(key=lambda b: b.get("timestamp") or 0)

                # Response time calculation
                last_user_ts = None
                for b in bubbles:
                    if b["type"] == "user":
                        last_user_ts = b.get("timestamp")
                    elif b["type"] == "ai" and last_user_ts is not None:
                        ts = b.get("timestamp")
                        if ts and ts > last_user_ts:
                            meta = b.setdefault("metadata", {})
                            meta["responseTimeMs"] = ts - last_user_ts

                # Aggregate metadata
                total_input = 0
                total_output = 0
                total_cached = 0
                total_response_ms = 0
                total_cost = 0.0
                total_tool_calls = 0
                total_thinking_ms = 0
                models_set: set = set()
                for b in bubbles:
                    m = b.get("metadata") or {}
                    if m.get("inputTokens"):
                        total_input += m["inputTokens"]
                    if m.get("outputTokens"):
                        total_output += m["outputTokens"]
                    if m.get("cachedTokens"):
                        total_cached += m["cachedTokens"]
                    if m.get("responseTimeMs"):
                        total_response_ms += m["responseTimeMs"]
                    if m.get("cost") is not None:
                        total_cost += m["cost"]
                    if m.get("modelName"):
                        models_set.add(m["modelName"])
                    if m.get("toolCalls"):
                        total_tool_calls += len(m["toolCalls"])
                    if m.get("thinkingDurationMs"):
                        total_thinking_ms += m["thinkingDurationMs"]

                # Composer-level cost fallback
                usage = cd.get("usageData") or {}
                composer_cost = usage.get("cost") or usage.get("estimatedCost")
                if isinstance(composer_cost, (int, float)) and total_cost == 0:
                    total_cost = composer_cost

                # Composer-level lines/files changed
                lines_added = cd.get("totalLinesAdded", 0)
                lines_removed = cd.get("totalLinesRemoved", 0)
                files_added = cd.get("addedFiles", 0)
                files_removed = cd.get("removedFiles", 0)

                # Context window progression from user bubbles
                max_ctx_tokens = 0
                ctx_token_limit = 0
                for b in bubbles:
                    m = b.get("metadata") or {}
                    if m.get("contextTokensUsed", 0) > max_ctx_tokens:
                        max_ctx_tokens = m["contextTokensUsed"]
                    if m.get("contextTokenLimit", 0) > ctx_token_limit:
                        ctx_token_limit = m["contextTokenLimit"]

                tab_meta = None
                has_any = any([total_input, total_output, total_cached, total_response_ms,
                              total_cost, models_set, total_tool_calls, total_thinking_ms,
                              lines_added, lines_removed, files_added, files_removed,
                              max_ctx_tokens])
                if has_any:
                    tab_meta_raw = {
                        "totalInputTokens": total_input or None,
                        "totalOutputTokens": total_output or None,
                        "totalCachedTokens": total_cached or None,
                        "modelsUsed": list(models_set) if models_set else None,
                        "totalResponseTimeMs": total_response_ms or None,
                        "totalCost": total_cost if total_cost > 0 else None,
                        "totalToolCalls": total_tool_calls or None,
                        "totalThinkingDurationMs": total_thinking_ms or None,
                        "totalLinesAdded": lines_added if lines_added else None,
                        "totalLinesRemoved": lines_removed if lines_removed else None,
                        "totalFilesAdded": files_added if files_added else None,
                        "totalFilesRemoved": files_removed if files_removed else None,
                        "maxContextTokensUsed": max_ctx_tokens if max_ctx_tokens else None,
                        "contextTokenLimit": ctx_token_limit if ctx_token_limit else None,
                    }
                    tab_meta = {k: v for k, v in tab_meta_raw.items() if v is not None}

                # Model config from composer data
                model_config = cd.get("modelConfig") or {}
                model_name_from_config = model_config.get("modelName")
                if model_name_from_config and model_name_from_config != "default":
                    if not tab_meta:
                        tab_meta = {}
                    if not tab_meta.get("modelsUsed"):
                        tab_meta["modelsUsed"] = [model_name_from_config]
                    elif model_name_from_config not in tab_meta["modelsUsed"]:
                        tab_meta["modelsUsed"].insert(0, model_name_from_config)

                tab = {
                    "id": composer_id,
                    "title": title,
                    "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
                    "bubbles": [{
                        "type": b["type"],
                        "text": b.get("text", ""),
                        "timestamp": b.get("timestamp", 0),
                        **({"metadata": b["metadata"]} if b.get("metadata") else {}),
                    } for b in bubbles],
                    "codeBlockDiffs": diffs,
                }
                if tab_meta:
                    tab["metadata"] = tab_meta

                response["tabs"].append(tab)

            except Exception as e:
                _logger.warning(
                    "Failed to process Composer from composerData:%s: %s",
                    composer_id,
                    e,
                )

        # Sort tabs by timestamp descending (newest first)
        response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)

        return response, 200
