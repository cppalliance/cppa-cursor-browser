#!/usr/bin/env python3
"""
CLI: Export Cursor chats to Markdown (zip archive by default).
Usage: python scripts/export.py [--since all|last] [--out DIR] [--no-zip] [--no-composer]
Run with --help for full usage information.
Env: WORKSPACE_PATH for Cursor workspaceStorage path.

When the package is installed via ``pip install -e .`` (or ``pip install .``),
this module is importable as ``scripts.export`` without any sys.path hacks.
The guard below is only necessary for direct invocation (``python scripts/export.py``).
"""

import json
import logging
import os
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# sys.path guard: only needed when the script is invoked directly
# (``python scripts/export.py``). When installed via the pyproject.toml
# entry point (``cursor-chat-export``) or imported as a module, the
# project root is already on sys.path.
if __name__ == "__main__":
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from utils.exclusion_rules import (  # noqa: E402
    resolve_exclusion_rules_path,
    load_rules,
    build_searchable_text,
    is_excluded_by_rules,
)
from utils.path_helpers import to_epoch_ms  # noqa: E402
from utils.text_extract import (  # noqa: E402
    extract_text_from_bubble,
    slug,
)
from utils.workspace_path import (  # noqa: E402
    get_cli_chats_path,
    resolve_workspace_path,
)
from utils.cli_chat_reader import (  # noqa: E402
    list_cli_projects,
    traverse_blobs,
    messages_to_bubbles,
)
from utils.cursor_md_exporter import (  # noqa: E402
    cursor_cli_session_to_markdown,
    cursor_ide_chat_to_markdown,
)
from models import Bubble, ExportEntry, SchemaError  # noqa: E402
from services.workspace_context import (  # noqa: E402
    enrich_workspace_context_from_global_db,
    resolve_workspace_context,
)
from services.workspace_db import (  # noqa: E402
    load_code_block_diff_map,
    open_global_db,
)
from services.workspace_resolver import (  # noqa: E402
    determine_project_for_conversation,
    infer_invalid_workspace_aliases,
    lookup_workspace_display_name,
)

_logger = logging.getLogger(__name__)


def configure_cli_logging() -> None:
    """Route log records to stderr so stdout stays for export progress lines."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def json_dump_safe(value) -> str:
    """Best-effort JSON serialization for exclusion matching."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value) if value is not None else ""


def load_manifest_entries(manifest_path: str) -> dict:
    """Load manifest entries keyed by log_id from a JSONL file."""
    existing: dict = {}
    if not os.path.isfile(manifest_path):
        return existing
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = ExportEntry.from_dict(json.loads(line))
                    existing[entry.log_id] = entry.raw
                except (SchemaError, json.JSONDecodeError, ValueError) as e:
                    # Pre-PR-30 manifests lack title/workspace — skip them so the
                    # next export rebuilds the entry under the new schema.
                    _logger.debug("Skipping manifest line in %s: %s", manifest_path, e)
    except OSError as e:
        _logger.debug("Failed to read manifest %s: %s", manifest_path, e)
    return existing


def write_manifest_entries(manifest_path: str, entries_by_id: dict):
    """Write manifest entries to JSONL."""
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        for entry in entries_by_id.values():
            f.write(json.dumps(entry) + "\n")


def get_global_state_dir() -> str:
    # Honor XDG_STATE_HOME when set so the export state file (and manifest)
    # can be redirected — required for hermetic test runs and useful for
    # users following the XDG Base Directory spec. Falls back to the
    # historical ~/.cursor-chat-browser location when the env var is unset.
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "cursor-chat-browser")
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        description="Export Cursor chat history to Markdown files.",
        epilog=(
            "By default exports ALL chats (including composer logs) as a zip archive\n"
            "into the current directory. Use the flags below to narrow the export.\n\n"
            "Env: WORKSPACE_PATH overrides the Cursor workspaceStorage path."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--since", choices=["all", "last"], default="all",
                        help="Export all chats or only those updated since last export. Default: all")
    parser.add_argument("--out", default=".",
                        help="Output directory. Default: current working directory (.)")
    parser.add_argument("--no-zip", action="store_true", default=False,
                        help="Write individual Markdown files instead of a zip archive.")
    parser.add_argument("--no-composer", action="store_true", default=False,
                        help="Exclude composer logs (export only chat logs).")
    parser.add_argument("--base-dir", default=None,
                        help="Override Cursor workspaceStorage path (also settable via WORKSPACE_PATH env var).")
    parser.add_argument(
        "--exclude-rules", "-e",
        default=None,
        metavar="PATH",
        dest="exclude_rules",
        help="Path to exclusion rules file (sensitive projects/chats are omitted). "
             "If omitted, uses ~/.cursor-chat-browser/exclusion-rules.txt if present.",
    )
    args = parser.parse_args()
    return {
        "since": args.since,
        "out_dir": args.out,
        "include_composer": not args.no_composer,
        "zip": not args.no_zip,
        "exclusion_rules_path": args.exclude_rules,
        "base_dir": args.base_dir,
    }


def main():
    configure_cli_logging()
    opts = parse_args()
    since = opts["since"]
    out_dir = os.path.abspath(opts["out_dir"])
    use_zip = opts["zip"]
    exclusion_rules = load_rules(resolve_exclusion_rules_path(opts.get("exclusion_rules_path")))
    if opts.get("base_dir"):
        os.environ["WORKSPACE_PATH"] = opts["base_dir"]
    workspace_path = resolve_workspace_path()

    state_dir = get_global_state_dir()
    state_path = os.path.join(state_dir, "export_state.json")
    last_export = 0
    if since == "last" and os.path.isfile(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            ts = st.get("lastExportTime")
            if ts:
                last_export = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except (json.JSONDecodeError, ValueError, OSError) as e:
            _logger.warning(
                "Could not read last export timestamp; defaulting to full export: %s",
                e,
            )

    # ── Workspace scanning via service layer ──────────────────────────────────
    ctx = resolve_workspace_context(workspace_path)
    workspace_entries = ctx.workspace_entries
    invalid_workspace_ids = ctx.invalid_workspace_ids
    project_name_map = ctx.project_name_to_workspace_id
    workspace_path_map = ctx.workspace_path_to_id
    composer_id_to_ws = ctx.composer_id_to_workspace_id

    # Build display-name and slug maps from workspace entries.
    # Entries whose workspace.json cannot be resolved are omitted so the
    # usage-site fallback (slug(ws_id[:12])) applies — matching original
    # behaviour where unresolvable workspaces were skipped.
    workspace_id_to_display_name: dict[str, str] = {}
    workspace_id_to_slug: dict[str, str] = {}
    for entry in workspace_entries:
        display = lookup_workspace_display_name(workspace_path, entry["name"])
        if display != entry["name"]:  # successfully resolved a human-readable name
            workspace_id_to_display_name[entry["name"]] = display
            workspace_id_to_slug[entry["name"]] = slug(display)

    # ── Database reading via service layer ────────────────────────────────────
    project_layouts_map: dict = {}
    bubble_map: dict[str, Bubble] = {}
    code_block_diff_map: dict = {}
    ide_composer_rows: list = []
    invalid_workspace_aliases: dict = {}

    with open_global_db(workspace_path) as (global_db, global_db_path):
        if global_db is None:
            _logger.info(
                "Cursor IDE global storage not found at %s — skipping IDE chats.",
                global_db_path,
            )
        else:
            ctx = enrich_workspace_context_from_global_db(
                ctx,
                global_db,
                populate_project_layouts=True,
                populate_bubble_map=True,
            )
            project_layouts_map = ctx.project_layouts_map
            bubble_map = ctx.bubble_map
            code_block_diff_map = load_code_block_diff_map(global_db)

            try:
                ide_composer_rows = global_db.execute(
                    "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                    " AND value LIKE '%fullConversationHeadersOnly%'"
                ).fetchall()
            except sqlite3.Error:
                pass

            invalid_workspace_aliases = infer_invalid_workspace_aliases(
                composer_rows=ide_composer_rows,
                project_layouts_map=project_layouts_map,
                project_name_map=project_name_map,
                workspace_path_map=workspace_path_map,
                workspace_entries=workspace_entries,
                bubble_map=bubble_map,
                composer_id_to_ws=composer_id_to_ws,
                invalid_workspace_ids=invalid_workspace_ids,
            )

    today = datetime.now().strftime("%Y-%m-%d")
    exported = []
    count = 0

    # ── Process IDE composers ────────────────────────────────────────────────
    include_composer = opts.get("include_composer", True)
    for row in ide_composer_rows if include_composer else []:
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

        headers = cd.get("fullConversationHeadersOnly") or []
        if not headers:
            continue

        updated_at = to_epoch_ms(cd.get("lastUpdatedAt"))
        if updated_at is None:
            updated_at = to_epoch_ms(cd.get("createdAt"))
        if updated_at is None:
            updated_at = 0
        if since == "last" and updated_at <= last_export:
            continue

        # Workspace assignment via service layer
        pid = determine_project_for_conversation(
            cd, composer_id, project_layouts_map,
            project_name_map, workspace_path_map,
            workspace_entries, bubble_map, composer_id_to_ws, invalid_workspace_ids,
        )
        mapped_ws = composer_id_to_ws.get(composer_id)
        if not pid and mapped_ws in invalid_workspace_ids:
            pid = invalid_workspace_aliases.get(mapped_ws)
        ws_id = pid if pid else "global"

        ws_slug = "other-chats" if ws_id == "global" else (workspace_id_to_slug.get(ws_id) or slug(ws_id[:12]))
        ws_display_name = "Other chats" if ws_id == "global" else (workspace_id_to_display_name.get(ws_id) or ws_slug)
        title = cd.get("name") or f"Chat {composer_id[:8]}"
        model_config = cd.get("modelConfig") or {}
        model_name = model_config.get("modelName")
        model_names = [model_name] if model_name and model_name != "default" else None

        # Build broad text for exclusion checks so any visible output term can match.
        # CLI export intentionally includes metadata/tool payload text in addition to
        # bubble text because these fields are emitted into exported markdown.
        bubble_texts = []
        bubble_meta_parts = []
        for h in headers:
            b = bubble_map.get(h.get("bubbleId"))
            if not b:
                continue
            text = extract_text_from_bubble(b)
            if text:
                bubble_texts.append(text)
            bubble_meta_parts.append(json_dump_safe(b))

        code_diff_parts = [json_dump_safe(d) for d in code_block_diff_map.get(composer_id, [])]
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

        # Markdown generation via shared exporter
        md = cursor_ide_chat_to_markdown(
            composer_data=cd,
            composer_id=composer_id,
            bubble_map=bubble_map,
            code_block_diff_map=code_block_diff_map,
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
        count += 1

    # ── Cursor CLI sessions ──────────────────────────────────────────────────
    try:
        cli_projects = list_cli_projects(get_cli_chats_path())
    except Exception as e:
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

        if is_excluded_by_rules(exclusion_rules, build_searchable_text(project_name=ws_name)):
            continue

        for session in cp["sessions"]:
            meta = session.get("meta", {})
            session_id = session["session_id"]
            created_ms: int = meta.get("createdAt") or int(datetime.now().timestamp() * 1000)
            session_name = meta.get("name") or f"Session {session_id[:8]}"

            # Use the store.db mtime as a proxy for "last updated" — createdAt
            # is immutable and would cause sessions with new turns to be skipped.
            try:
                db_mtime_ms = int(os.path.getmtime(session["db_path"]) * 1000)
            except OSError:
                db_mtime_ms = created_ms
            updated_ms = max(created_ms, db_mtime_ms)

            if since == "last" and updated_ms <= last_export:
                continue

            try:
                messages = traverse_blobs(session["db_path"])
                bubbles = messages_to_bubbles(messages, created_ms)
            except Exception as e:
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

            # Derive title for the filename (shared exporter does it too, but
            # we need it here first to build the output path).
            title = session_name
            if not title or title.startswith("New Agent"):
                for b in bubbles:
                    if b["type"] == "user" and b.get("text"):
                        first_lines = [ln for ln in b["text"].split("\n") if ln.strip()]
                        if first_lines:
                            title = first_lines[0][:100]
                            if len(title) == 100:
                                title += "..."
                        break

            bubble_texts = [b["text"] for b in bubbles if b.get("text")]
            tool_call_texts = [
                tc.get("input", "") or tc.get("summary", "")
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
            ts_str = datetime.fromtimestamp(created_ms / 1000).strftime("%Y-%m-%dT%H-%M-%S")
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
            count += 1

    if count == 0:
        label = " since last export" if since == "last" else ""
        print(f"No conversations found{label}.")
        sys.exit(0)

    os.makedirs(out_dir, exist_ok=True)

    if use_zip:
        zip_name = f"cursor-export-{today}.zip"
        zip_path = os.path.join(out_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["rel_path"], entry["content"])
        print(f"Exported {count} chat(s) to {zip_path}")
    else:
        for entry in exported:
            os.makedirs(os.path.dirname(entry["out_path"]), exist_ok=True)
            with open(entry["out_path"], "w", encoding="utf-8") as f:
                f.write(entry["content"])

        manifest_path = os.path.join(out_dir, "manifest.jsonl")
        existing = load_manifest_entries(manifest_path)
        for entry in exported:
            existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": os.path.relpath(entry["out_path"], out_dir),
                "updated_at": datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat() if entry["updatedAt"] else datetime.now().isoformat(),
            }
        if existing:
            write_manifest_entries(manifest_path, existing)

        global_manifest_path = os.path.join(state_dir, "manifest.jsonl")
        global_existing = load_manifest_entries(global_manifest_path)
        for entry in exported:
            global_existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": entry["out_path"],
                "updated_at": datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat() if entry["updatedAt"] else datetime.now().isoformat(),
            }
        if global_existing:
            write_manifest_entries(global_manifest_path, global_existing)
        print(f"Exported {count} chat(s) to {out_dir}")

    state = {
        "lastExportTime": datetime.now().isoformat(),
        "exportedCount": count,
        "exportDir": out_dir,
    }
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "export_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    print(f"State saved to {os.path.join(state_dir, 'export_state.json')}")


if __name__ == "__main__":
    main()
