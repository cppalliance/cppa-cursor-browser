#!/usr/bin/env python3
"""
CLI: Export Cursor chats to Markdown (zip archive by default).
Usage: python scripts/export.py [--since all|last] [--out DIR] [--no-zip] [--no-composer]
Run with --help for full usage information.
Env: WORKSPACE_PATH for Cursor workspaceStorage path.
"""

import json
import logging
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote as _url_unquote

# Ensure project root is on path when run as python scripts/export.py
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

# noqa: E402 — these imports must come after the sys.path.insert above so the
# script can be run directly as `python scripts/export.py` from anywhere.
from utils.exclusion_rules import (  # noqa: E402
    resolve_exclusion_rules_path,
    load_rules,
    build_searchable_text,
    is_excluded_by_rules,
)
from utils.path_helpers import get_workspace_folder_paths as _shared_get_workspace_folder_paths  # noqa: E402
from utils.tool_parser import parse_tool_call  # noqa: E402
from utils.workspace_path import get_cli_chats_path  # noqa: E402
from utils.cli_chat_reader import (  # noqa: E402
    list_cli_projects,
    traverse_blobs,
    messages_to_bubbles,
)
from utils.cursor_md_exporter import cursor_cli_session_to_markdown  # noqa: E402
from models import ExportEntry, SchemaError  # noqa: E402

_logger = logging.getLogger(__name__)


def _json_dump_safe(value) -> str:
    """Best-effort JSON serialization for exclusion matching."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value) if value is not None else ""


def _load_manifest_entries(manifest_path: str) -> dict:
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


def _write_manifest_entries(manifest_path: str, entries_by_id: dict):
    """Write manifest entries to JSONL."""
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        for entry in entries_by_id.values():
            f.write(json.dumps(entry) + "\n")


def get_default_workspace_path() -> str:
    home = str(Path.home())
    release = ""
    try:
        release = os.uname().release.lower()
    except AttributeError:
        pass
    is_wsl = "microsoft" in release or "wsl" in release
    is_remote = bool(
        os.environ.get("SSH_CONNECTION")
        or os.environ.get("SSH_CLIENT")
        or os.environ.get("SSH_TTY")
    )

    if is_wsl:
        import subprocess
        username = os.getenv("USER", "")
        try:
            username = subprocess.check_output(
                ["cmd.exe", "/c", "echo", "%USERNAME%"],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
        except Exception:
            pass
        return f"/mnt/c/Users/{username}/AppData/Roaming/Cursor/User/workspaceStorage"

    if sys.platform == "win32":
        return os.path.join(home, "AppData", "Roaming", "Cursor", "User", "workspaceStorage")
    elif sys.platform == "darwin":
        return os.path.join(home, "Library", "Application Support", "Cursor", "User", "workspaceStorage")
    elif sys.platform == "linux":
        if is_remote:
            return os.path.join(home, ".cursor-server", "data", "User", "workspaceStorage")
        return os.path.join(home, ".config", "Cursor", "User", "workspaceStorage")
    return os.path.join(home, "workspaceStorage")


def resolve_workspace_path() -> str:
    env = os.environ.get("WORKSPACE_PATH", "").strip()
    if env:
        if env.startswith("~/"):
            return os.path.join(str(Path.home()), env[2:])
        return env
    return get_default_workspace_path()


def get_global_state_dir() -> str:
    # Honor XDG_STATE_HOME when set so the export state file (and manifest)
    # can be redirected — required for hermetic test runs and useful for
    # users following the XDG Base Directory spec. Falls back to the
    # historical ~/.cursor-chat-browser location when the env var is unset.
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "cursor-chat-browser")
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def normalize_file_path(p: str) -> str:
    n = re.sub(r"^file:///", "", p or "")
    n = re.sub(r"^file://", "", n)
    try:
        from urllib.parse import unquote
        n = unquote(n)
    except Exception:
        pass
    if sys.platform == "win32":
        n = n.replace("/", "\\")
        n = re.sub(r"^\\([a-zA-Z]:)", r"\1", n)
        n = n.lower()
    return n


def to_epoch_ms(value) -> int:
    """Convert a timestamp (int, float, or ISO-8601 string) to epoch ms."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        if value > 1e12:
            return int(value)
        if value > 0:
            return int(value * 1000)
        return 0
    if isinstance(value, str):
        try:
            cleaned = value.rstrip("Z") + "+00:00" if value.endswith("Z") else value
            dt = datetime.fromisoformat(cleaned)
            return int(dt.timestamp() * 1000)
        except Exception:
            pass
        try:
            return to_epoch_ms(float(value))
        except Exception:
            pass
    return 0


def slug(s: str) -> str:
    s = re.sub(r'[<>:"/\\|?*]', "_", s or "")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s[:80] or "untitled"


def extract_text_from_rich_text(children) -> str:
    if not isinstance(children, list):
        return ""
    t = ""
    for c in children:
        if not isinstance(c, dict):
            continue
        if c.get("type") == "text" and c.get("text"):
            t += c["text"]
        elif c.get("type") == "code" and c.get("children"):
            t += "\n```\n" + extract_text_from_rich_text(c["children"]) + "\n```\n"
        elif c.get("children"):
            t += extract_text_from_rich_text(c["children"])
    return t


def extract_text_from_bubble(bubble) -> str:
    if not bubble or not isinstance(bubble, dict):
        return ""
    t = ""
    if bubble.get("text") and str(bubble["text"]).strip():
        t = bubble["text"]
    if not t and bubble.get("richText"):
        try:
            r = json.loads(bubble["richText"]) if isinstance(bubble["richText"], str) else bubble["richText"]
            if isinstance(r, dict) and r.get("root") and r["root"].get("children"):
                t = extract_text_from_rich_text(r["root"]["children"])
        except Exception:
            pass
    cbs = bubble.get("codeBlocks")
    if isinstance(cbs, list):
        for cb in cbs:
            if isinstance(cb, dict) and cb.get("content"):
                t += f"\n\n```{cb.get('language', '')}\n{cb['content']}\n```"
    return t


def get_workspace_folder_paths(wd) -> list:
    return _shared_get_workspace_folder_paths(wd)


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
    opts = parse_args()
    since = opts["since"]
    out_dir = os.path.abspath(opts["out_dir"])
    use_zip = opts["zip"]
    exclusion_rules = load_rules(resolve_exclusion_rules_path(opts.get("exclusion_rules_path")))
    if opts.get("base_dir"):
        os.environ["WORKSPACE_PATH"] = opts["base_dir"]
    workspace_path = resolve_workspace_path()
    global_path = os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))

    state_dir = get_global_state_dir()
    state_path = os.path.join(state_dir, "export_state.json")
    last_export = 0
    if since == "last" and os.path.isfile(state_path):
        try:
            with open(state_path, "r") as f:
                st = json.load(f)
            ts = st.get("lastExportTime")
            if ts:
                last_export = int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000)
        except Exception:
            pass

    # Pre-initialize IDE data — populated below only if the IDE database is accessible.
    workspace_entries: list = []
    workspace_path_to_id: dict = {}
    project_name_to_ws: dict = {}
    workspace_id_to_slug: dict = {}
    workspace_id_to_display_name: dict[str, str] = {}
    project_layouts_map: dict = {}
    bubble_map: dict = {}
    code_block_diff_map: dict = {}
    ide_composer_rows: list = []

    # Load IDE chat data — skipped gracefully when the database is absent or locked.
    if not os.path.isfile(global_path):
        print(f"Note: Cursor IDE global storage not found at {global_path} — skipping IDE chats.", file=sys.stderr)
    else:
        _conn = None
        try:
            _conn = sqlite3.connect(f"file:{global_path}?mode=ro", uri=True)
            _conn.row_factory = sqlite3.Row

            # Build workspace entries
            try:
                for name in os.listdir(workspace_path):
                    full = os.path.join(workspace_path, name)
                    if os.path.isdir(full):
                        wp = os.path.join(full, "workspace.json")
                        if os.path.isfile(wp):
                            workspace_entries.append({"name": name, "workspaceJsonPath": wp})
            except Exception:
                pass

            for e in workspace_entries:
                try:
                    with open(e["workspaceJsonPath"], "r", encoding="utf-8") as f:
                        wd = json.load(f)
                    folders = get_workspace_folder_paths(wd)
                    first_folder = folders[0] if folders else None
                    if isinstance(first_folder, str) and first_folder:
                        fn = re.sub(r"^file://", "", first_folder).replace("\\", "/").split("/")[-1]
                        if fn:
                            workspace_id_to_slug[e["name"]] = slug(fn)
                            workspace_id_to_display_name[e["name"]] = _url_unquote(fn)
                    for folder in get_workspace_folder_paths(wd):
                        norm = normalize_file_path(folder)
                        workspace_path_to_id[norm] = e["name"]
                        fn2 = re.sub(r"^file://", "", folder).replace("\\", "/").split("/")[-1]
                        if fn2:
                            project_name_to_ws[fn2] = e["name"]
                except Exception:
                    pass

            # Project layouts
            try:
                for row in _conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"):
                    parts = row["key"].split(":")
                    if len(parts) < 2:
                        continue
                    cid = parts[1]
                    try:
                        ctx = json.loads(row["value"])
                        layouts = ctx.get("projectLayouts")
                        if isinstance(layouts, list):
                            project_layouts_map.setdefault(cid, [])
                            for layout in layouts:
                                try:
                                    o = json.loads(layout) if isinstance(layout, str) else layout
                                    if isinstance(o, dict) and o.get("rootPath"):
                                        project_layouts_map[cid].append(o["rootPath"])
                                except Exception:
                                    pass
                    except Exception:
                        pass
            except Exception:
                pass

            # Bubble map
            try:
                for row in _conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"):
                    parts = row["key"].split(":")
                    if len(parts) >= 3:
                        bid = parts[2]
                        try:
                            b = json.loads(row["value"])
                            if isinstance(b, dict):
                                bubble_map[bid] = b
                        except Exception:
                            pass
            except Exception:
                pass

            # Code block diffs
            try:
                for row in _conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"):
                    parts = row["key"].split(":")
                    cid = parts[1] if len(parts) > 1 else None
                    if not cid:
                        continue
                    try:
                        d = json.loads(row["value"])
                        code_block_diff_map.setdefault(cid, []).append({
                            **d,
                            "diffId": parts[2] if len(parts) > 2 else None,
                        })
                    except Exception:
                        pass
            except Exception:
                pass

            ide_composer_rows = _conn.execute(
                "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
                " AND value LIKE '%fullConversationHeadersOnly%'"
            ).fetchall()
        except Exception as e:
            print(f"Warning: Could not read Cursor IDE chats ({e}) — skipping.", file=sys.stderr)
        finally:
            # Guaranteed close on every exit path (issue #17). Replaces the
            # previous duplicate close-in-success-and-error pattern.
            if _conn is not None:
                try:
                    _conn.close()
                except Exception:
                    pass

    def get_project_from_file_path(fp):
        np = normalize_file_path(fp)
        best = None
        best_len = 0
        for e in workspace_entries:
            try:
                with open(e["workspaceJsonPath"], "r", encoding="utf-8") as f:
                    wd = json.load(f)
                for folder in get_workspace_folder_paths(wd):
                    wp = normalize_file_path(folder)
                    if np.startswith(wp) and len(wp) > best_len:
                        best_len = len(wp)
                        best = e["name"]
            except Exception:
                pass
        return best

    def assign_workspace(cd, cid):
        # Try project layouts
        pl = project_layouts_map.get(cid, [])
        best_layout = None
        best_len = 0
        for rp in pl:
            match = get_project_from_file_path(rp)
            if match:
                nl = len(normalize_file_path(rp))
                if nl > best_len:
                    best_len = nl
                    best_layout = match
        if best_layout:
            return best_layout

        # Try file paths
        paths = []
        for fi in (cd.get("newlyCreatedFiles") or []):
            if isinstance(fi, dict) and fi.get("uri") and fi["uri"].get("path"):
                paths.append(normalize_file_path(fi["uri"]["path"]))
        for fp in (cd.get("codeBlockData") or {}).keys():
            paths.append(normalize_file_path(re.sub(r"^file://", "", fp)))
        for h in (cd.get("fullConversationHeadersOnly") or []):
            b = bubble_map.get(h.get("bubbleId"))
            if not b:
                continue
            for fp in (b.get("relevantFiles") or []):
                if fp:
                    paths.append(normalize_file_path(fp))
            for u in (b.get("attachedFileCodeChunksUris") or []):
                if isinstance(u, dict) and u.get("path"):
                    paths.append(normalize_file_path(u["path"]))
            for fs_entry in (b.get("context", {}).get("fileSelections") or []):
                if isinstance(fs_entry, dict) and isinstance(fs_entry.get("uri"), dict) and fs_entry["uri"].get("path"):
                    paths.append(normalize_file_path(fs_entry["uri"]["path"]))

        sep = "\\" if sys.platform == "win32" else "/"
        best_id = None
        best_l = 0
        for p in paths:
            for e in workspace_entries:
                try:
                    with open(e["workspaceJsonPath"], "r", encoding="utf-8") as f:
                        wd = json.load(f)
                    for folder in get_workspace_folder_paths(wd):
                        fn = re.sub(r"^file://", "", folder).replace("\\", "/").split("/")[-1]
                        if not fn:
                            continue
                        needle = sep + fn + sep
                        needle_end = sep + fn
                        if needle in p or p.endswith(needle_end):
                            if len(fn) > best_l:
                                best_l = len(fn)
                                best_id = e["name"]
                except Exception:
                    pass
        return best_id or "global"

    today = datetime.now().strftime("%Y-%m-%d")
    exported = []
    count = 0

    # Process IDE composers (skipped entirely when --no-composer was passed)
    include_composer = opts.get("include_composer", True)
    for row in ide_composer_rows if include_composer else []:
        composer_id = row["key"].split(":")[1]
        try:
            cd = json.loads(row["value"])
        except Exception:
            continue

        headers = cd.get("fullConversationHeadersOnly") or []
        if not headers:
            continue

        updated_at = to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or 0
        if since == "last" and updated_at <= last_export:
            continue

        ws_id = assign_workspace(cd, composer_id)
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
            bubble_meta_parts.append(_json_dump_safe(b))

        code_diff_parts = [_json_dump_safe(d) for d in code_block_diff_map.get(composer_id, [])]
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
                    + [
                        _json_dump_safe(model_config),
                        _json_dump_safe(cd),
                    ]
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
        rel_dir = os.path.join(today, ws_slug, "chat")
        out_path = os.path.join(out_dir, rel_dir, filename)

        # Build bubbles with full metadata
        bubbles = []
        for h in headers:
            b = bubble_map.get(h.get("bubbleId"))
            if not b:
                continue
            text = extract_text_from_bubble(b)
            has_tool = isinstance(b.get("toolFormerData"), dict)
            has_thinking = bool(b.get("thinking"))
            if not text.strip() and not has_tool and not has_thinking:
                continue
            if not text.strip() and has_tool:
                text = f"**Tool: {b['toolFormerData'].get('name', 'unknown')}**"

            btype = "user" if h.get("type") == 1 else "ai"

            thinking = None
            thinking_duration_ms = None
            if b.get("thinking"):
                thinking = b["thinking"] if isinstance(b["thinking"], str) else (
                    b["thinking"].get("text") if isinstance(b["thinking"], dict) else None
                )
                thinking_duration_ms = b.get("thinkingDurationMs")

            tool_info = None
            if has_tool:
                tool_info = parse_tool_call(b["toolFormerData"])

            model_info = (b.get("modelInfo") or {}).get("modelName")
            if model_info == "default":
                model_info = None

            ctx_window = b.get("contextWindowStatusAtCreation") or {}
            ctx_tokens_used = ctx_window.get("tokensUsed", 0)
            ctx_token_limit = ctx_window.get("tokenLimit", 0)
            ctx_pct_remaining = ctx_window.get("percentageRemainingFloat") or ctx_window.get("percentageRemaining")

            bubbles.append({
                "type": btype,
                "text": text,
                "timestamp": to_epoch_ms(b.get("createdAt")) or to_epoch_ms(b.get("timestamp")) or int(datetime.now().timestamp() * 1000),
                "tool": tool_info,
                "thinking": thinking,
                "thinkingDurationMs": thinking_duration_ms,
                "model": model_info,
                "contextTokensUsed": ctx_tokens_used if ctx_tokens_used > 0 else None,
                "contextTokenLimit": ctx_token_limit if ctx_token_limit > 0 else None,
                "contextPctRemaining": round(ctx_pct_remaining, 1) if ctx_pct_remaining else None,
            })

        # Code block diffs
        for d in code_block_diff_map.get(composer_id, []):
            bubbles.append({
                "type": "ai",
                "text": f"**Code edit:** {json.dumps(d)}",
                "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
            })

        bubbles.sort(key=lambda bub: bub.get("timestamp") or 0)

        # Compute per-assistant-bubble response times
        last_user_ts = None
        for bub in bubbles:
            if bub["type"] == "user":
                last_user_ts = bub.get("timestamp")
            elif bub["type"] == "ai" and last_user_ts:
                bts = bub.get("timestamp")
                if bts and bts > last_user_ts:
                    bub["responseTimeMs"] = bts - last_user_ts

        # Session-level aggregates
        total_response_ms = sum(bub.get("responseTimeMs", 0) for bub in bubbles)
        total_thinking_ms = sum(bub.get("thinkingDurationMs", 0) or 0 for bub in bubbles)
        total_tool_calls = sum(1 for bub in bubbles if bub.get("tool"))
        max_ctx_used = max((bub.get("contextTokensUsed") or 0) for bub in bubbles) if bubbles else 0
        ctx_limit = max((bub.get("contextTokenLimit") or 0) for bub in bubbles) if bubbles else 0

        tool_breakdown = {}
        for bub in bubbles:
            if bub.get("tool"):
                tn = bub["tool"].get("name", "unknown")
                tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

        lines_added = cd.get("totalLinesAdded", 0)
        lines_removed = cd.get("totalLinesRemoved", 0)

        # Wall-clock duration from bubble timestamps
        ts_vals = [bub["timestamp"] for bub in bubbles if bub.get("timestamp")]
        wall_clock_sec = int((max(ts_vals) - min(ts_vals)) / 1000) if len(ts_vals) >= 2 else None

        # Collect file/command activity and tool result stats from tool calls
        files_read_list = []
        files_written_list = []
        commands_run_list = []
        tool_result_stats = {
            "terminal_success": 0, "terminal_error": 0,
            "file_reads": 0, "file_edits": 0,
            "searches": 0, "web": 0,
        }
        for bub in bubbles:
            if not bub.get("tool"):
                continue
            t = bub["tool"]
            tn = t.get("name", "")
            status = t.get("status") or ""
            raw_input = str(t.get("input") or "").strip()
            first_line = raw_input.split("\n")[0] if raw_input else ""
            if tn == "read_file_v2" and first_line:
                files_read_list.append(first_line)
                tool_result_stats["file_reads"] += 1
            elif tn == "edit_file_v2" and first_line:
                files_written_list.append(first_line)
                tool_result_stats["file_edits"] += 1
            elif tn == "run_terminal_command_v2" and raw_input:
                commands_run_list.append(raw_input)
                if status == "completed":
                    tool_result_stats["terminal_success"] += 1
                elif status in ("error", "failed"):
                    tool_result_stats["terminal_error"] += 1
                else:
                    tool_result_stats["terminal_success"] += 1
            elif tn in ("ripgrep_raw_search", "glob_file_search", "semantic_search_full"):
                tool_result_stats["searches"] += 1
            elif tn in ("web_search", "web_fetch"):
                tool_result_stats["web"] += 1

        # Frontmatter
        created_ms = to_epoch_ms(cd.get("createdAt")) or ts
        fm_lines = ["---"]
        fm_lines.append(f"log_id: {composer_id}")
        fm_lines.append("log_type: chat")
        fm_lines.append(f'title: "{title.replace(chr(34), chr(92)+chr(34))}"')
        fm_lines.append(f"created_at: {datetime.fromtimestamp(created_ms / 1000).isoformat()}")
        fm_lines.append(f"updated_at: {datetime.fromtimestamp(updated_at / 1000).isoformat() if updated_at else datetime.now().isoformat()}")
        fm_lines.append(f"workspace: {ws_slug}")
        fm_lines.append(f'workspace_name: "{ws_display_name}"')
        if model_name and model_name != "default":
            fm_lines.append(f"model: {model_name}")
        fm_lines.append(f"message_count: {len(bubbles)}")
        if total_tool_calls:
            fm_lines.append(f"total_tool_calls: {total_tool_calls}")
        if tool_breakdown:
            fm_lines.append("tool_call_breakdown:")
            for tn, cnt in sorted(tool_breakdown.items(), key=lambda x: -x[1]):
                fm_lines.append(f"  {tn}: {cnt}")
        total_think = sum(1 for bub in bubbles if bub.get("thinking"))
        if total_think:
            fm_lines.append(f"thinking_count: {total_think}")
        if wall_clock_sec is not None:
            fm_lines.append(f"wall_clock_seconds: {wall_clock_sec}")
        if total_response_ms:
            fm_lines.append(f"total_response_time_sec: {total_response_ms / 1000:.1f}")
        if total_thinking_ms:
            fm_lines.append(f"total_thinking_time_sec: {total_thinking_ms / 1000:.1f}")
        if max_ctx_used and ctx_limit:
            fm_lines.append(f"max_context_tokens_used: {max_ctx_used}")
            fm_lines.append(f"context_token_limit: {ctx_limit}")
        if lines_added or lines_removed:
            fm_lines.append(f"lines_added: {lines_added}")
            fm_lines.append(f"lines_removed: {lines_removed}")
        if files_read_list or files_written_list:
            fm_lines.append(f"files_read: {len(files_read_list)}")
            fm_lines.append(f"files_written: {len(files_written_list)}")
        if commands_run_list:
            fm_lines.append(f"commands_run: {len(commands_run_list)}")
        fm_lines.append("---")
        fm_str = "\n".join(fm_lines) + "\n\n"

        # Header
        header = f"# {title}\n\n"
        meta_parts = []
        if created_ms:
            meta_parts.append(f"Created: {datetime.fromtimestamp(created_ms / 1000).strftime('%Y-%m-%d %H:%M:%S')}")
        if model_name and model_name != "default":
            meta_parts.append(f"Model: {model_name}")
        if total_tool_calls:
            meta_parts.append(f"Tool calls: {total_tool_calls}")
        if wall_clock_sec is not None:
            hrs, rem = divmod(wall_clock_sec, 3600)
            mins, secs = divmod(rem, 60)
            dur = f"{hrs}h {mins}m" if hrs else (f"{mins}m {secs}s" if mins else f"{secs}s")
            meta_parts.append(f"Duration: {dur}")
        header += f"_{' | '.join(meta_parts)}_\n\n---\n\n" if meta_parts else "---\n\n"

        # Session summary block
        summary = ""
        if files_read_list or files_written_list or commands_run_list:
            summary += "## Session Summary\n\n"
            if files_written_list or files_read_list:
                summary += "### Files Touched\n\n"
                summary += "| Action | File |\n|--------|------|\n"
                for fp in files_written_list:
                    summary += f"| Edit | `{fp}` |\n"
                for fp in files_read_list:
                    summary += f"| Read | `{fp}` |\n"
                summary += "\n"
            if commands_run_list:
                summary += "### Commands Run\n\n"
                for i, cmd in enumerate(commands_run_list, 1):
                    summary += f"{i}. `{cmd}`\n"
                summary += "\n"
            non_zero = {k: v for k, v in tool_result_stats.items() if v > 0}
            if non_zero:
                summary += "### Tool Results\n\n"
                labels = {
                    "terminal_success": "Terminal Success",
                    "terminal_error": "Terminal Error",
                    "file_reads": "File Reads",
                    "file_edits": "File Edits",
                    "searches": "Searches",
                    "web": "Web Fetches",
                }
                for k, v in non_zero.items():
                    summary += f"- {labels.get(k, k)}: {v}\n"
                summary += "\n"
            summary += "---\n\n"

        # Body
        body = ""
        for bub in bubbles:
            role = "User" if bub["type"] == "user" else "Assistant"
            body += f"### {role}\n\n"
            # Per-message metadata line
            meta_parts = []
            if bub.get("model"):
                meta_parts.append(f"Model: {bub['model']}")
            if bub.get("responseTimeMs"):
                meta_parts.append(f"Response: {bub['responseTimeMs'] / 1000:.1f}s")
            if bub.get("thinkingDurationMs"):
                meta_parts.append(f"Thinking: {bub['thinkingDurationMs'] / 1000:.1f}s")
            if bub.get("contextTokensUsed") and bub.get("contextTokenLimit"):
                pct = bub["contextTokensUsed"] / bub["contextTokenLimit"] * 100
                meta_parts.append(f"Context: {bub['contextTokensUsed']:,} / {bub['contextTokenLimit']:,} tokens ({pct:.0f}% used)")
            elif bub.get("contextPctRemaining") is not None:
                meta_parts.append(f"Context: {bub['contextPctRemaining']}% remaining")
            if meta_parts:
                body += f"_{' | '.join(meta_parts)}_\n\n"
            if bub.get("timestamp"):
                body += f"_{datetime.fromtimestamp(bub['timestamp'] / 1000).isoformat()}_\n\n"
            if bub.get("thinking"):
                dur_str = f" ({bub['thinkingDurationMs'] / 1000:.1f}s)" if bub.get("thinkingDurationMs") else ""
                body += f"<details><summary>Thinking{dur_str}</summary>\n\n{bub['thinking']}\n\n</details>\n\n"
            body += bub["text"] + "\n\n"
            if bub.get("tool"):
                t = bub["tool"]
                tool_summary = t.get("summary") or t.get("name") or "unknown"
                tool_status = t.get("status") or ""
                status_str = f" ({tool_status})" if tool_status else ""
                body += f"> **Tool: {tool_summary}**{status_str}\n"
                if t.get("input"):
                    body += "> **INPUT:**\n> ```\n"
                    for iline in str(t["input"]).split("\n"):
                        body += f"> {iline}\n"
                    body += "> ```\n"
                if t.get("output"):
                    body += "> **OUTPUT:**\n> ```\n"
                    for oline in str(t["output"]).split("\n"):
                        body += f"> {oline}\n"
                    body += "> ```\n"
                body += "\n"
            body += "---\n\n"

        md = fm_str + header + summary + body

        rel_path = os.path.join(today, ws_slug, "chat", filename)
        exported.append({"id": composer_id, "rel_path": rel_path, "content": md,
                         "out_path": out_path, "updatedAt": updated_at,
                         "title": title, "workspace": ws_display_name})
        count += 1

    # --- Cursor CLI sessions ---
    try:
        cli_projects = list_cli_projects(get_cli_chats_path())
    except Exception as e:
        print(f"Warning: Could not enumerate CLI chats ({e}) — skipping.", file=sys.stderr)
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
                print(f"Warning: Could not read CLI session {session_id}: {e}", file=sys.stderr)
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
            rel_dir = os.path.join(today, ws_slug_cli, "cli")
            out_path = os.path.join(out_dir, rel_dir, filename)

            # Delegate Markdown generation to the shared exporter.
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
        # Archive all exported Markdown files into a single zip
        zip_name = f"cursor-export-{today}.zip"
        zip_path = os.path.join(out_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["rel_path"], entry["content"])
        print(f"Exported {count} chat(s) to {zip_path}")
    else:
        # Write individual Markdown files to disk
        for entry in exported:
            os.makedirs(os.path.dirname(entry["out_path"]), exist_ok=True)
            with open(entry["out_path"], "w", encoding="utf-8") as f:
                f.write(entry["content"])

        # Manifest in output directory
        manifest_path = os.path.join(out_dir, "manifest.jsonl")
        existing = _load_manifest_entries(manifest_path)

        for entry in exported:
            existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": os.path.relpath(entry["out_path"], out_dir),
                "updated_at": datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat() if entry["updatedAt"] else datetime.now().isoformat(),
            }

        if existing:
            _write_manifest_entries(manifest_path, existing)

        # Canonical manifest in user state dir so tracking survives changing --out paths
        global_manifest_path = os.path.join(state_dir, "manifest.jsonl")
        global_existing = _load_manifest_entries(global_manifest_path)
        for entry in exported:
            global_existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": entry["out_path"],
                "updated_at": datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat() if entry["updatedAt"] else datetime.now().isoformat(),
            }
        if global_existing:
            _write_manifest_entries(global_manifest_path, global_existing)
        print(f"Exported {count} chat(s) to {out_dir}")

    # Save state
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
