from __future__ import annotations

import hashlib
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
    warn_workspace_json_read,
)
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.text_extract import extract_text_from_bubble
from utils.tool_parser import parse_tool_call
from utils.workspace_descriptor import read_json_file
from models import Bubble, Composer, ParseWarningCollector, SchemaError
from services.summary_cache import (
    fingerprint_workspace_storage,
    get_cached_tab_summaries,
    nocache_enabled,
    set_cached_tab_summaries,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    assigned_workspace_from_mapping,
    build_composer_id_to_workspace_id_cached,
    get_workspace_composer_registry_cached,
    collect_invalid_workspace_ids,
    collect_workspace_entries,
    global_storage_db_path,
    load_bubbles_for_composer,
    load_code_block_diff_map,
    load_code_block_diffs_for_composer,
    load_composer_rows_for_workspace_summary,
    load_message_request_context_for_composer,
    load_project_layouts_for_composer,
    load_project_layouts_map,
    open_global_db,
    prefetch_project_layouts_for_unmapped,
)
from utils.workspace_path import get_cli_chats_path
from services.workspace_resolver import (
    create_project_name_to_workspace_id_map,
    create_workspace_path_to_id_map,
    determine_project_for_conversation,
    infer_invalid_workspace_aliases,
    lookup_workspace_display_name,
)
from services.workspace_listing import _composer_valid_for_listing



def _loads_kv_value_logged(key: str, raw: object | None) -> Any | None:
    """Parse a cursorDiskKV ``value``; log and return ``None`` on decode failure."""
    if raw is None:
        return None
    if not isinstance(raw, (str, bytes, bytearray)):
        payload_len, payload_fp = _kv_payload_log_meta(raw)
        _logger.warning(
            "Failed to decode cursorDiskKV value for %s: unsupported type %s (payload_len=%d, payload_sha256=%s)",
            key,
            type(raw).__name__,
            payload_len,
            payload_fp,
        )
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        payload_len, payload_fp = _kv_payload_log_meta(raw)
        _logger.warning(
            "Failed to decode cursorDiskKV value for %s: %s (payload_len=%d, payload_sha256=%s)",
            key,
            e,
            payload_len,
            payload_fp,
        )
        return None


def _kv_payload_log_meta(value: object | None) -> tuple[int, str | None]:
    """Byte length and short SHA-256 prefix for logs without emitting raw KV payloads."""
    if value is None:
        return 0, None
    if isinstance(value, bytes):
        payload = value
    else:
        payload = str(value).encode("utf-8", errors="replace")
    return len(payload), hashlib.sha256(payload).hexdigest()[:12]


def _assemble_tab_from_composer_data(
    composer_id: str,
    cd: dict,
    bubble_map: dict[str, dict],
    contexts: list[dict],
    code_block_diffs: list[dict],
    workspace_display_name: str,
    rules: list,
    parse_warnings: ParseWarningCollector,
) -> dict | None:
    """Assemble a single tab dict from an already-parsed composer dict.

    Args:
        composer_id: Composer UUID.
        cd: Raw ``composerData`` dict (``composer.raw``).
        bubble_map: ``{bubble_id: bubble_dict}`` — may be global or scoped.
        contexts: ``messageRequestContext`` entries for *this* composer
            (list of dicts, each with an injected ``contextId`` key and a
            ``bubbleId`` field from the JSON value).
        code_block_diffs: ``codeBlockDiff`` entries for *this* composer.
        workspace_display_name: Human-readable workspace name for rule matching.
        rules: Exclusion rule token lists.
        parse_warnings: Collector for skipped-bubble warnings.

    Returns:
        A tab dict on success, or ``None`` when the tab should be omitted
        (no renderable bubbles or excluded by rules).
    """
    headers = cd.get("fullConversationHeadersOnly") or []

    bubbles: list[dict[str, Any]] = []
    for header in headers:
        if not isinstance(header, dict):
            continue
        bubble_id = header.get("bubbleId")
        if not isinstance(bubble_id, str):
            continue
        bubble = bubble_map.get(bubble_id)
        if not bubble:
            continue

        is_user = header.get("type") == 1
        msg_type = "user" if is_user else "ai"
        text = extract_text_from_bubble(bubble)

        context_text = ""
        for ctx in contexts:
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

        tool_calls = None
        tfd = raw.get("toolFormerData")
        if isinstance(tfd, dict):
            tool_call = parse_tool_call(tfd)
            if isinstance(tool_call, dict):
                tool_calls = [tool_call]

        thinking = None
        thinking_duration_ms = None
        if raw.get("thinking"):
            thinking = raw["thinking"] if isinstance(raw["thinking"], str) else (raw["thinking"].get("text") if isinstance(raw["thinking"], dict) else None)
            thinking_duration_ms = raw.get("thinkingDurationMs")

        has_content = full_text.strip() or tool_calls or thinking
        if not has_content:
            continue

        ctx_window = raw.get("contextWindowStatusAtCreation") or {}
        ctx_pct = None
        if isinstance(ctx_window, dict):
            if ctx_window.get("percentageRemainingFloat") is not None:
                ctx_pct = ctx_window.get("percentageRemainingFloat")
            elif ctx_window.get("percentageRemaining") is not None:
                ctx_pct = ctx_window.get("percentageRemaining")

        display_text = full_text.strip()
        if not display_text and tool_calls:
            tc = tool_calls[0]
            if isinstance(tc, dict):
                display_text = f"**Tool: {tc.get('name', 'unknown')}**"
                if tc.get("status"):
                    display_text += f" ({tc['status']})"
        if not display_text and thinking:
            display_text = thinking

        bubble_meta = None
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
        return None

    title = cd.get("name") or f"Conversation {composer_id[:8]}"
    if not cd.get("name") and bubbles:
        first_msg = bubbles[0].get("text", "")
        if first_msg:
            first_lines = [ln for ln in first_msg.split("\n") if ln.strip()]
            if first_lines:
                title = first_lines[0][:100]
                if len(title) == 100:
                    title += "..."

    _early_model_config = cd.get("modelConfig") or {}
    _early_model_name = _early_model_config.get("modelName")
    _early_model_names = [_early_model_name] if _early_model_name and _early_model_name != "default" else None
    if is_excluded_by_rules(rules, build_searchable_text(
        project_name=workspace_display_name,
        chat_title=title,
        model_names=_early_model_names,
    )):
        return None

    bubbles.sort(key=lambda b: b.get("timestamp") or 0)

    last_user_ts = None
    for b in bubbles:
        if b["type"] == "user":
            last_user_ts = b.get("timestamp")
        elif b["type"] == "ai" and last_user_ts is not None:
            ts = b.get("timestamp")
            if ts and ts > last_user_ts:
                meta = b.setdefault("metadata", {})
                meta["responseTimeMs"] = ts - last_user_ts

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

    usage = cd.get("usageData") or {}
    composer_cost = usage.get("cost") or usage.get("estimatedCost")
    if isinstance(composer_cost, (int, float)) and total_cost == 0:
        total_cost = composer_cost

    lines_added = cd.get("totalLinesAdded", 0)
    lines_removed = cd.get("totalLinesRemoved", 0)
    files_added = cd.get("addedFiles", 0)
    files_removed = cd.get("removedFiles", 0)

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

    model_config = cd.get("modelConfig") or {}
    model_name_from_config = model_config.get("modelName")
    if model_name_from_config and model_name_from_config != "default":
        if not tab_meta:
            tab_meta = {}
        models_used = tab_meta.get("modelsUsed")
        if not isinstance(models_used, list):
            tab_meta["modelsUsed"] = [model_name_from_config]
        elif model_name_from_config not in models_used:
            models_used.insert(0, model_name_from_config)

    tab: dict[str, Any] = {
        "id": composer_id,
        "title": title,
        "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
        "bubbles": [{
            "type": b["type"],
            "text": b.get("text", ""),
            "timestamp": b.get("timestamp", 0),
            **({"metadata": b["metadata"]} if b.get("metadata") else {}),
        } for b in bubbles],
        "codeBlockDiffs": code_block_diffs,
    }
    if tab_meta:
        tab["metadata"] = tab_meta
    return tab


def _build_matching_ws_ids(workspace_id: str, workspace_path: str, workspace_entries: list) -> set[str]:
    """Return the set of workspace folder IDs that share the same project folder as *workspace_id*.

    Cursor sometimes creates multiple workspace entries for the same on-disk
    project; conversations recorded under any of those IDs belong to the same
    project view.
    """
    matching: set[str] = {workspace_id}
    if workspace_id == "global":
        return matching
    target_folder = ""
    wj_path = os.path.join(workspace_path, workspace_id, "workspace.json")
    try:
        wd = read_json_file(wj_path)
        folders = get_workspace_folder_paths(wd)
        first_folder = folders[0] if folders else None
        if first_folder:
            target_folder = normalize_file_path(first_folder)
    except Exception as e:
        warn_workspace_json_read(_logger, workspace_id, e)
    if target_folder:
        for entry in workspace_entries:
            try:
                wd2 = read_json_file(entry["workspaceJsonPath"])
                folders2 = get_workspace_folder_paths(wd2)
                f2 = folders2[0] if folders2 else None
                if f2 and normalize_file_path(f2) == target_folder:
                    matching.add(entry["name"])
            except Exception as e:
                warn_workspace_json_read(_logger, entry["name"], e)
    return matching


def list_workspace_tab_summaries(
    workspace_id: str,
    workspace_path: str,
    rules: list,
    *,
    nocache: bool = False,
) -> tuple[dict, int]:
    """Return summary tab list for GET /api/workspaces/<id>/tabs?summary=1.

    Does **not** load the global ``bubbleId:%`` index.  Each tab entry contains
    only the fields needed by the sidebar: ``id``, ``title``, ``timestamp``,
    ``messageCount``, and an optional ``metadata.modelsUsed``.  Full bubble
    bodies are omitted; the UI fetches them on demand via
    ``GET /api/workspaces/<id>/tabs/<composer_id>``.

    Args:
        workspace_id: Workspace folder name, or ``"global"`` for unassigned chats.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists.

    Returns:
        ``(payload, status)`` — same envelope as :func:`assemble_workspace_tabs`
        but ``tabs`` entries carry no ``bubbles`` field.
    """
    workspace_entries = collect_workspace_entries(workspace_path)
    gdb = global_storage_db_path(workspace_path)
    cli_path = get_cli_chats_path()
    fingerprint = fingerprint_workspace_storage(
        workspace_path,
        workspace_entries,
        global_db_path=gdb if os.path.isfile(gdb) else None,
        rules=rules,
        cli_chats_path=cli_path if os.path.isdir(cli_path) else None,
    )
    if not nocache_enabled(request_nocache=nocache):
        cached = get_cached_tab_summaries(fingerprint, workspace_id)
        if cached is not None:
            return cached

    payload, status = _build_workspace_tab_summaries_uncached(
        workspace_id, workspace_path, rules, workspace_entries, nocache=nocache,
    )
    if status == 200 and not nocache_enabled(request_nocache=nocache):
        set_cached_tab_summaries(fingerprint, workspace_id, payload, status)
    return payload, status


def _build_workspace_tab_summaries_uncached(
    workspace_id: str,
    workspace_path: str,
    rules: list,
    workspace_entries: list,
    *,
    nocache: bool,
) -> tuple[dict, int]:
    parse_warnings = ParseWarningCollector()
    response: dict = {"tabs": []}

    invalid_workspace_ids = collect_invalid_workspace_ids(workspace_entries)
    project_name_map = create_project_name_to_workspace_id_map(workspace_entries)
    workspace_path_map = create_workspace_path_to_id_map(workspace_entries)
    registry = get_workspace_composer_registry_cached(
        workspace_path, workspace_entries, rules, nocache=nocache,
    )
    composer_id_to_ws = registry.composer_id_to_ws
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": "Global storage not found"}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        def _safe_fetchall(query: str, params: tuple = ()) -> list:
            try:
                return global_db.execute(query, params).fetchall()
            except sqlite3.Error:
                return []

        project_layouts_map: dict[str, list] = {}
        if invalid_workspace_ids:
            project_layouts_map = load_project_layouts_map(global_db)

        composer_rows, alias_rows, unmapped_for_layouts = load_composer_rows_for_workspace_summary(
            global_db,
            composer_id_to_ws,
            matching_ws_ids,
            invalid_workspace_ids,
            registry=registry,
        )
        prefetch_project_layouts_for_unmapped(
            global_db,
            unmapped_for_layouts,
            project_layouts_map,
            invalid_workspace_ids=invalid_workspace_ids,
        )

        invalid_workspace_aliases: dict[str, str] = {}
        if invalid_workspace_ids:
            invalid_workspace_aliases = infer_invalid_workspace_aliases(
                composer_rows=alias_rows,
                project_layouts_map=project_layouts_map,
                project_name_map=project_name_map,
                workspace_path_map=workspace_path_map,
                workspace_entries=workspace_entries,
                bubble_map={},
                composer_id_to_ws=composer_id_to_ws,
                invalid_workspace_ids=invalid_workspace_ids,
            )

        for row in composer_rows:
            composer_id = row["key"].split(":")[1]
            cd = _loads_kv_value_logged(row["key"], row["value"])
            if not isinstance(cd, dict):
                parse_warnings.record_composer_skipped()
                continue
            if not _composer_valid_for_listing(cd, composer_id, parse_warnings):
                continue
            try:
                assigned = assigned_workspace_from_mapping(
                    composer_id,
                    composer_id_to_ws,
                    invalid_workspace_ids,
                    invalid_workspace_aliases,
                )
                if assigned is None:
                    pid = determine_project_for_conversation(
                        cd, composer_id, project_layouts_map,
                        project_name_map, workspace_path_map,
                        workspace_entries, {}, composer_id_to_ws, invalid_workspace_ids,
                    )
                    assigned = pid if pid else "global"

                if assigned not in matching_ws_ids:
                    continue

                headers = cd.get("fullConversationHeadersOnly") or []
                if not headers:
                    continue

                title = cd.get("name") or f"Conversation {composer_id[:8]}"

                _early_model_config = cd.get("modelConfig") or {}
                _early_model_name = _early_model_config.get("modelName")
                _early_model_names = [_early_model_name] if _early_model_name and _early_model_name != "default" else None
                if is_excluded_by_rules(rules, build_searchable_text(
                    project_name=workspace_display_name,
                    chat_title=title,
                    model_names=_early_model_names,
                )):
                    continue

                tab_meta: dict | None = None
                if _early_model_names:
                    tab_meta = {"modelsUsed": _early_model_names}

                tab_entry: dict = {
                    "id": composer_id,
                    "title": title,
                    "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
                    "messageCount": len(headers),
                }
                if tab_meta:
                    tab_entry["metadata"] = tab_meta
                response["tabs"].append(tab_entry)

            except Exception as e:
                _logger.warning(
                    "Failed to process Composer from composerData:%s: %s",
                    composer_id,
                    e,
                )
                parse_warnings.record_composer_processing_failure()

        response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)
        return parse_warnings.attach_to(response), 200


def assemble_single_tab(
    workspace_id: str,
    composer_id: str,
    workspace_path: str,
    rules: list,
) -> tuple[dict, int]:
    """Assemble a single conversation tab for GET /api/workspaces/<id>/tabs/<composer_id>.

    Loads only the KV rows scoped to *composer_id* (``bubbleId:{id}:%``,
    ``messageRequestContext:{id}:%``, ``codeBlockDiff:{id}:%``) instead of
    performing a full global scan.

    Args:
        workspace_id: Workspace folder name, or ``"global"``.
        composer_id: UUID of the composer / conversation to assemble.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists.

    Returns:
        ``(payload, status)``.  On success (``200``), *payload* is
        ``{"tab": {...}}``, optionally with ``"warnings"``.  ``404`` when the
        global DB is missing, the composer is not found, or it is not assigned
        to *workspace_id*.
    """
    parse_warnings = ParseWarningCollector()

    workspace_entries = collect_workspace_entries(workspace_path)
    invalid_workspace_ids = collect_invalid_workspace_ids(workspace_entries)
    project_name_map = create_project_name_to_workspace_id_map(workspace_entries)
    workspace_path_map = create_workspace_path_to_id_map(workspace_entries)
    composer_id_to_ws = build_composer_id_to_workspace_id_cached(
        workspace_path, workspace_entries, rules,
    )
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": "Global storage not found"}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        def _safe_fetchall(query: str, params: tuple = ()) -> list:
            try:
                return global_db.execute(query, params).fetchall()
            except sqlite3.Error:
                return []

        rows = _safe_fetchall(
            "SELECT key, value FROM cursorDiskKV WHERE key = ?",
            (f"composerData:{composer_id}",),
        )
        if not rows:
            return {"error": "Conversation not found"}, 404

        row = rows[0]
        try:
            parsed = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            payload_len, payload_fp = _kv_payload_log_meta(row["value"])
            _logger.warning(
                "Failed to decode Composer from composerData:%s: %s (payload_len=%d, payload_sha256=%s)",
                composer_id,
                e,
                payload_len,
                payload_fp,
            )
            return {"error": "Failed to parse conversation"}, 500
        try:
            composer = Composer.from_dict(parsed, composer_id=composer_id)
        except SchemaError as e:
            _logger.warning(
                "Failed to parse Composer from composerData:%s: %s",
                composer_id,
                e,
            )
            return {"error": "Failed to parse conversation"}, 500

        cd = composer.raw

        # Verify the conversation belongs to the requested workspace.
        # Always scoped: only load messageRequestContext rows for this composer.
        project_layouts_map: dict[str, list] = {}
        invalid_workspace_aliases: dict[str, str] = {}
        project_layouts_map[composer_id] = load_project_layouts_for_composer(
            global_db, composer_id,
        )
        if invalid_workspace_ids:
            # Alias resolution still needs the composer roster, but project layouts
            # are intentionally limited to this composer (single-tab scope).
            composer_rows_for_aliases = _safe_fetchall(COMPOSER_ROWS_WITH_HEADERS_SQL)
            invalid_workspace_aliases = infer_invalid_workspace_aliases(
                composer_rows=composer_rows_for_aliases,
                project_layouts_map=project_layouts_map,
                project_name_map=project_name_map,
                workspace_path_map=workspace_path_map,
                workspace_entries=workspace_entries,
                bubble_map={},
                composer_id_to_ws=composer_id_to_ws,
                invalid_workspace_ids=invalid_workspace_ids,
            )

        pid = determine_project_for_conversation(
            cd, composer_id, project_layouts_map,
            project_name_map, workspace_path_map,
            workspace_entries, {}, composer_id_to_ws, invalid_workspace_ids,
        )
        mapped_ws = composer_id_to_ws.get(composer_id)
        if not pid and mapped_ws in invalid_workspace_ids:
            pid = invalid_workspace_aliases.get(mapped_ws)
        assigned = pid if pid else "global"

        if assigned not in matching_ws_ids:
            return {"error": "Conversation not found"}, 404

        # Scoped loads — only rows for this composer_id.
        bubble_map = load_bubbles_for_composer(global_db, composer_id)
        contexts = load_message_request_context_for_composer(global_db, composer_id)
        code_block_diffs = load_code_block_diffs_for_composer(global_db, composer_id)

        tab = _assemble_tab_from_composer_data(
            composer_id=composer_id,
            cd=cd,
            bubble_map=bubble_map,
            contexts=contexts,
            code_block_diffs=code_block_diffs,
            workspace_display_name=workspace_display_name,
            rules=rules,
            parse_warnings=parse_warnings,
        )

        if tab is None:
            return {"error": "Conversation not found"}, 404

        response: dict = {"tab": tab}
        return parse_warnings.attach_to(response), 200


def assemble_workspace_tabs(
    workspace_id: str,
    workspace_path: str,
    rules: list,
) -> tuple[dict, int]:
    """Build tabs payload for GET /api/workspaces/<id>/tabs (IDE workspaces).

    Args:
        workspace_id: Workspace folder name, or ``"global"`` for unassigned chats.
        workspace_path: Cursor ``workspaceStorage`` root.
        rules: Exclusion rule token lists from :func:`utils.exclusion_rules.load_rules`.

    Returns:
        ``(payload, status)``. On success (``200``), *payload* contains ``tabs``
        (list of tab dicts with ``id``, ``title``, ``timestamp``, ``bubbles``,
        optional ``metadata`` / ``codeBlockDiffs``) and optional ``warnings``
        when parse failures were skipped. On failure (``404``), *payload* is
        ``{"error": "Global storage not found"}``.
    """
    parse_warnings = ParseWarningCollector()
    response: dict = {"tabs": []}

    workspace_entries = collect_workspace_entries(workspace_path)
    invalid_workspace_ids = collect_invalid_workspace_ids(workspace_entries)
    project_name_map = create_project_name_to_workspace_id_map(workspace_entries)
    workspace_path_map = create_workspace_path_to_id_map(workspace_entries)
    composer_id_to_ws = build_composer_id_to_workspace_id_cached(
        workspace_path, workspace_entries, rules,
    )
    matching_ws_ids = _build_matching_ws_ids(workspace_id, workspace_path, workspace_entries)

    bubble_map: dict[str, dict] = {}
    code_block_diff_map: dict[str, list] = {}
    message_request_context_map: dict[str, list] = {}

    with open_global_db(workspace_path) as (global_db, _):
        if global_db is None:
            return {"error": "Global storage not found"}, 404

        workspace_display_name = lookup_workspace_display_name(workspace_path, workspace_id)

        def _safe_fetchall(query: str, params: tuple = ()) -> list:
            try:
                return global_db.execute(query, params).fetchall()
            except sqlite3.Error:
                return []

        # Load bubbles
        for row in _safe_fetchall(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
            " AND value IS NOT NULL"
        ):
            parts = row["key"].split(":")
            if len(parts) >= 3:
                bid = parts[2]
                try:
                    parsed = json.loads(row["value"])

                except (json.JSONDecodeError, TypeError, ValueError) as e:
                    payload_len, payload_fp = _kv_payload_log_meta(row["value"])
                    _logger.warning(
                        "Failed to decode Bubble from %s: %s (payload_len=%d, payload_sha256=%s)",
                        row["key"],
                        e,
                        payload_len,
                        payload_fp,
                    )
                    parse_warnings.record_bubble_skipped()
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
                    parse_warnings.record_bubble_skipped()

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
            ctx = _loads_kv_value_logged(row["key"], row["value"])
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
                        layout = _loads_kv_value_logged(
                            f"{row['key']}:projectLayout",
                            layout,
                        )
                        if not isinstance(layout, dict):
                            continue
                    if isinstance(layout, dict) and layout.get("rootPath"):
                        project_layouts_map[chat_id].append(layout["rootPath"])

        # Get composer data entries with conversations
        composer_rows = _safe_fetchall(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
            " AND value IS NOT NULL"
            " AND value LIKE '%fullConversationHeadersOnly%'"
            " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
        )

        invalid_workspace_aliases = infer_invalid_workspace_aliases(
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
            except (json.JSONDecodeError, TypeError, ValueError) as e:
                payload_len, payload_fp = _kv_payload_log_meta(row["value"])
                _logger.warning(
                    "Failed to decode Composer from composerData:%s: %s (key=%s, payload_len=%d, payload_sha256=%s)",
                    composer_id,
                    e,
                    row["key"],
                    payload_len,
                    payload_fp,
                )
                parse_warnings.record_composer_skipped()
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
                parse_warnings.record_composer_skipped()
                continue
            try:
                cd = composer.raw

                # Determine project
                pid = determine_project_for_conversation(
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

                tab = _assemble_tab_from_composer_data(
                    composer_id=composer_id,
                    cd=cd,
                    bubble_map=bubble_map,
                    contexts=message_request_context_map.get(composer_id, []),
                    code_block_diffs=code_block_diff_map.get(composer_id, []),
                    workspace_display_name=workspace_display_name,
                    rules=rules,
                    parse_warnings=parse_warnings,
                )
                if tab is not None:
                    response["tabs"].append(tab)

            except Exception as e:
                _logger.warning(
                    "Failed to process Composer from composerData:%s: %s",
                    composer_id,
                    e,
                )
                parse_warnings.record_composer_processing_failure()

        # Sort tabs by timestamp descending (newest first)
        response["tabs"].sort(key=lambda t: t.get("timestamp") or 0, reverse=True)

        return parse_warnings.attach_to(response), 200
