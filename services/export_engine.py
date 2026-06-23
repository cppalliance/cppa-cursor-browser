"""Shared export orchestration for CLI and web paths."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, TypedDict

from models import Bubble
from services.summary_cache import fingerprint_workspace_storage
from services.workspace_context import (
    WorkspaceContext,
    enrich_workspace_context_from_global_db,
    resolve_workspace_context_cached,
)
from services.workspace_db import (
    COMPOSER_ROWS_WITH_HEADERS_SQL,
    collect_workspace_entries,
    global_storage_db_path,
    load_code_block_diff_map,
    open_global_db,
    safe_fetchall,
)
from services.workspace_resolver import (
    determine_project_for_conversation,
    infer_invalid_workspace_aliases,
    lookup_workspace_display_name,
)
from utils.cli_chat_reader import (
    list_cli_projects,
    messages_to_bubbles,
    traverse_blobs,
)
from utils.cursor_md_exporter import (
    cursor_cli_session_to_markdown,
    cursor_ide_chat_to_markdown,
)
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from utils.path_helpers import to_epoch_ms
from utils.text_extract import extract_text_from_bubble, slug
from utils.workspace_path import get_cli_chats_path

_logger = logging.getLogger(__name__)

SinceMode = Literal["all", "last"]


class ExportEntry(TypedDict):
    """One exportable conversation with rendered markdown."""

    id: str
    rel_path: str
    content: str
    out_path: str
    updatedAt: int
    title: str
    workspace: str


@dataclass(frozen=True)
class WorkspaceOrchestration:
    """Precomputed workspace maps shared by listing and export."""

    workspace_path: str
    workspace_entries: list[dict[str, Any]]
    fingerprint: dict[str, Any]
    ctx: WorkspaceContext
    workspace_id_to_display_name: dict[str, str]
    workspace_id_to_slug: dict[str, str]


@dataclass(frozen=True)
class GlobalDbExportData:
    """Global KV data loaded for export orchestration."""

    project_layouts_map: dict[str, list[str]]
    bubble_map: dict[str, Bubble]
    code_block_diff_map: dict[str, list[Any]]
    ide_composer_rows: list[sqlite3.Row]
    invalid_workspace_aliases: dict[str, str]


def json_dump_safe(value: object) -> str:
    """Best-effort JSON serialization for exclusion matching."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:  # noqa: BLE001 — best-effort fallback when value is not JSON-serializable
        return str(value) if value is not None else ""


def build_workspace_display_maps(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, str]]:
    """Build display-name and slug maps from workspace entries.

    Entries whose ``workspace.json`` cannot be resolved are omitted so the
    usage-site fallback (``slug(ws_id[:12])``) applies.
    """
    workspace_id_to_display_name: dict[str, str] = {}
    workspace_id_to_slug: dict[str, str] = {}
    for entry in workspace_entries:
        display = lookup_workspace_display_name(workspace_path, entry["name"])
        if display != entry["name"]:
            workspace_id_to_display_name[entry["name"]] = display
            workspace_id_to_slug[entry["name"]] = slug(display)
    return workspace_id_to_display_name, workspace_id_to_slug


def prepare_workspace_orchestration(
    workspace_path: str,
    rules: list[Any],
    *,
    nocache: bool = False,
    workspace_entries: list[dict[str, Any]] | None = None,
) -> WorkspaceOrchestration:
    """Scan workspace storage and resolve maps (with summary-cache fingerprint)."""
    entries = (
        workspace_entries
        if workspace_entries is not None
        else collect_workspace_entries(workspace_path)
    )
    gdb = global_storage_db_path(workspace_path)
    cli_path = get_cli_chats_path()
    fingerprint = fingerprint_workspace_storage(
        workspace_path,
        entries,
        global_db_path=gdb if os.path.isfile(gdb) else None,
        rules=rules,
        cli_chats_path=cli_path if os.path.isdir(cli_path) else None,
    )
    ctx = resolve_workspace_context_cached(
        workspace_path,
        rules,
        workspace_entries=entries,
        nocache=nocache,
    )
    display_name, slug_map = build_workspace_display_maps(workspace_path, entries)
    return WorkspaceOrchestration(
        workspace_path=workspace_path,
        workspace_entries=entries,
        fingerprint=fingerprint,
        ctx=ctx,
        workspace_id_to_display_name=display_name,
        workspace_id_to_slug=slug_map,
    )


def load_global_db_export_data(
    orch: WorkspaceOrchestration,
) -> GlobalDbExportData | None:
    """Load global DB maps needed for IDE composer export."""
    ctx = orch.ctx
    project_layouts_map: dict[str, list[str]] = {}
    bubble_map: dict[str, Bubble] = {}
    code_block_diff_map: dict[str, list[Any]] = {}
    ide_composer_rows: list[sqlite3.Row] = []
    invalid_workspace_aliases: dict[str, str] = {}

    with open_global_db(orch.workspace_path) as (global_db, global_db_path):
        if global_db is None:
            _logger.info(
                "Cursor IDE global storage not found at %s — skipping IDE chats.",
                global_db_path,
            )
            return None

        enriched = enrich_workspace_context_from_global_db(
            ctx,
            global_db,
            populate_project_layouts=True,
            populate_bubble_map=True,
        )
        project_layouts_map = enriched.project_layouts_map
        bubble_map = enriched.bubble_map
        code_block_diff_map = load_code_block_diff_map(global_db)
        ide_composer_rows = safe_fetchall(global_db, COMPOSER_ROWS_WITH_HEADERS_SQL)

        invalid_workspace_aliases = infer_invalid_workspace_aliases(
            composer_rows=ide_composer_rows,
            project_layouts_map=project_layouts_map,
            project_name_map=ctx.project_name_to_workspace_id,
            workspace_path_map=ctx.workspace_path_to_id,
            workspace_entries=orch.workspace_entries,
            bubble_map=bubble_map,
            composer_id_to_ws=ctx.composer_id_to_workspace_id,
            invalid_workspace_ids=ctx.invalid_workspace_ids,
        )

    return GlobalDbExportData(
        project_layouts_map=project_layouts_map,
        bubble_map=bubble_map,
        code_block_diff_map=code_block_diff_map,
        ide_composer_rows=ide_composer_rows,
        invalid_workspace_aliases=invalid_workspace_aliases,
    )


def _collect_ide_export_entries(
    *,
    orch: WorkspaceOrchestration,
    db_data: GlobalDbExportData,
    exclusion_rules: list[Any],
    since: SinceMode,
    last_export_ms: int,
    today: str,
    out_dir: str,
) -> list[ExportEntry]:
    ctx = orch.ctx
    exported: list[ExportEntry] = []
    for row in db_data.ide_composer_rows:
        composer_id = row["key"].split(":")[1]
        try:
            cd = json.loads(row["value"])
        except (json.JSONDecodeError, ValueError) as parse_err:
            _logger.debug(
                "Skipping corrupt composerData row %s: %s",
                composer_id,
                parse_err,
            )
            continue

        if not isinstance(cd, dict):
            _logger.debug(
                "Skipping corrupt composerData row %s: expected object, got %s",
                composer_id,
                type(cd).__name__,
            )
            continue

        headers = cd.get("fullConversationHeadersOnly") or []
        if not isinstance(headers, list) or not headers:
            continue

        updated_at = to_epoch_ms(cd.get("lastUpdatedAt"))
        if updated_at is None:
            updated_at = to_epoch_ms(cd.get("createdAt"))
        if updated_at is None:
            updated_at = 0
        if since == "last" and updated_at <= last_export_ms:
            continue

        pid = determine_project_for_conversation(
            cd,
            composer_id,
            db_data.project_layouts_map,
            ctx.project_name_to_workspace_id,
            ctx.workspace_path_to_id,
            orch.workspace_entries,
            db_data.bubble_map,
            ctx.composer_id_to_workspace_id,
            ctx.invalid_workspace_ids,
        )
        mapped_ws = ctx.composer_id_to_workspace_id.get(composer_id)
        if not pid and mapped_ws in ctx.invalid_workspace_ids:
            pid = db_data.invalid_workspace_aliases.get(mapped_ws)
        ws_id = pid if pid else "global"

        ws_slug = (
            "other-chats"
            if ws_id == "global"
            else (orch.workspace_id_to_slug.get(ws_id) or slug(ws_id[:12]))
        )
        ws_display_name = (
            "Other chats"
            if ws_id == "global"
            else (orch.workspace_id_to_display_name.get(ws_id) or ws_slug)
        )
        title = cd.get("name") or f"Chat {composer_id[:8]}"
        model_config = cd.get("modelConfig") or {}
        model_name = model_config.get("modelName")
        model_names = [model_name] if model_name and model_name != "default" else None

        bubble_texts: list[str] = []
        bubble_meta_parts: list[str] = []
        for h in headers:
            if not isinstance(h, dict):
                continue
            bubble_id = h.get("bubbleId")
            if not isinstance(bubble_id, str):
                continue
            b = db_data.bubble_map.get(bubble_id)
            if not b:
                continue
            text = extract_text_from_bubble(b)
            if text:
                bubble_texts.append(text)
            bubble_meta_parts.append(json_dump_safe(b))

        code_diff_parts = [
            json_dump_safe(d) for d in db_data.code_block_diff_map.get(composer_id, [])
        ]
        searchable = build_searchable_text(
            project_name=ws_display_name,
            chat_title=title,
            model_names=model_names,
            chat_content_snippet="\n\n".join(
                p
                for p in (
                    bubble_texts
                    + bubble_meta_parts
                    + code_diff_parts
                    + [json_dump_safe(model_config), json_dump_safe(cd)]
                )
                if p
            ),
        )
        if is_excluded_by_rules(exclusion_rules, searchable):
            continue

        title_slug = slug(title)
        ts = updated_at or int(datetime.now().timestamp() * 1000)
        ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H-%M-%S")
        filename = f"{ts_str}__{title_slug}__{composer_id[:8]}.md"
        out_path = os.path.join(out_dir, today, ws_slug, "chat", filename)

        md = cursor_ide_chat_to_markdown(
            composer_data=cd,
            composer_id=composer_id,
            bubble_map=db_data.bubble_map,
            code_block_diff_map=db_data.code_block_diff_map,
            workspace_info={"ws_slug": ws_slug, "ws_display_name": ws_display_name},
        )

        rel_path = os.path.join(today, ws_slug, "chat", filename)
        exported.append({
            "id": composer_id,
            "rel_path": rel_path,
            "content": md,
            "out_path": out_path,
            "updatedAt": updated_at,
            "title": title,
            "workspace": ws_display_name,
        })
    return exported


def _collect_cli_export_entries(
    *,
    exclusion_rules: list[Any],
    since: SinceMode,
    last_export_ms: int,
    today: str,
    out_dir: str,
) -> list[ExportEntry]:
    exported: list[ExportEntry] = []
    try:
        cli_projects = list_cli_projects(get_cli_chats_path())
    except Exception as e:  # noqa: BLE001 — log and skip CLI enumeration on any failure
        _logger.warning(
            "Could not enumerate CLI chats: %s (%s) — skipping",
            e,
            type(e).__name__,
            exc_info=True,
        )
        cli_projects = []

    for cp in cli_projects:
        ws_name = cp["workspace_name"] or cp["project_id"][:12]
        ws_slug_cli = slug(ws_name)

        if is_excluded_by_rules(
            exclusion_rules, build_searchable_text(project_name=ws_name),
        ):
            continue

        for session in cp["sessions"]:
            meta = session.get("meta", {})
            session_id = session["session_id"]
            created_ms: int = meta.get("createdAt") or int(
                datetime.now().timestamp() * 1000,
            )
            session_name = meta.get("name") or f"Session {session_id[:8]}"

            try:
                db_mtime_ms = int(os.path.getmtime(session["db_path"]) * 1000)
            except OSError:
                db_mtime_ms = created_ms
            updated_ms = max(created_ms, db_mtime_ms)

            if since == "last" and updated_ms <= last_export_ms:
                continue

            try:
                messages = traverse_blobs(session["db_path"])
                bubbles = messages_to_bubbles(messages, created_ms)
            except Exception as e:  # noqa: BLE001 — log and skip session on read/parse failure
                _logger.warning(
                    "Could not read CLI session %s: %s (%s)",
                    session_id,
                    e,
                    type(e).__name__,
                    exc_info=True,
                )
                continue

            if not bubbles:
                continue

            title = session_name
            if not title or title.startswith("New Agent"):
                for b in bubbles:
                    if b["type"] == "user" and b.get("text"):
                        first_lines = [
                            ln for ln in b["text"].split("\n") if ln.strip()
                        ]
                        if first_lines:
                            title = first_lines[0][:100]
                            if len(title) == 100:
                                title += "..."
                        break

            bubble_texts = [b["text"] for b in bubbles if b.get("text")]
            tool_call_texts = [
                json_dump_safe(tc.get("input", "") or tc.get("summary", ""))
                for b in bubbles
                for tc in (b.get("metadata") or {}).get("toolCalls") or []
            ]
            searchable = build_searchable_text(
                project_name=ws_name,
                chat_title=title,
                chat_content_snippet="\n\n".join(bubble_texts + tool_call_texts),
            )
            if is_excluded_by_rules(exclusion_rules, searchable):
                continue

            title_slug = slug(title)
            ts_str = datetime.fromtimestamp(created_ms / 1000).strftime(
                "%Y-%m-%dT%H-%M-%S",
            )
            filename = f"{ts_str}__{title_slug}__{session_id[:8]}.md"
            out_path = os.path.join(out_dir, today, ws_slug_cli, "cli", filename)

            md = cursor_cli_session_to_markdown(
                session["db_path"],
                session_meta=meta,
                workspace_info={
                    "workspace": ws_slug_cli,
                    "workspace_name": ws_name,
                    "workspace_path": cp.get("workspace_path"),
                    "project_id": cp["project_id"],
                },
                bubbles=bubbles,
                title_override=title,
            )
            rel_path = os.path.join(today, ws_slug_cli, "cli", filename)
            exported.append({
                "id": session_id,
                "rel_path": rel_path,
                "content": md,
                "out_path": out_path,
                "updatedAt": updated_ms,
                "title": title,
                "workspace": ws_name,
            })
    return exported


def collect_export_entries(
    *,
    workspace_path: str,
    exclusion_rules: list[Any],
    since: SinceMode,
    last_export_ms: int,
    out_dir: str,
    include_composer: bool = True,
    nocache: bool = False,
) -> list[ExportEntry]:
    """Collect exportable conversations (IDE + CLI) via shared orchestration."""
    orch = prepare_workspace_orchestration(
        workspace_path, exclusion_rules, nocache=nocache,
    )
    today = datetime.now().strftime("%Y-%m-%d")
    exported: list[ExportEntry] = []

    if include_composer:
        db_data = load_global_db_export_data(orch)
        if db_data is not None:
            exported.extend(
                _collect_ide_export_entries(
                    orch=orch,
                    db_data=db_data,
                    exclusion_rules=exclusion_rules,
                    since=since,
                    last_export_ms=last_export_ms,
                    today=today,
                    out_dir=out_dir,
                ),
            )

    exported.extend(
        _collect_cli_export_entries(
            exclusion_rules=exclusion_rules,
            since=since,
            last_export_ms=last_export_ms,
            today=today,
            out_dir=out_dir,
        ),
    )
    return exported
