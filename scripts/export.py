#!/usr/bin/env python3
"""
CLI: Export Cursor chats to Markdown (zip archive by default).
Usage: python scripts/export.py [--since all|last] [--out DIR] [--no-zip] [--no-composer]
Run with --help for full usage information.
Env: WORKSPACE_PATH for Cursor workspaceStorage path.
"""

import json
import os
import re
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path

# Ensure project root is on path when run as python scripts/export.py
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from utils.exclusion_rules import (
    resolve_exclusion_rules_path,
    load_rules,
    build_searchable_text,
    is_excluded_by_rules,
)


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
    paths = []
    if wd.get("folder"):
        paths.append(wd["folder"])
    folders = wd.get("folders")
    if isinstance(folders, list):
        for f in folders:
            if isinstance(f, dict) and f.get("path"):
                paths.append(f["path"])
    return paths


HELP_TEXT = """\
Export Cursor chat history to Markdown files.

By default exports ALL chats (including composer logs) as a zip archive
into the current directory. Use the flags below to narrow the export.

Usage:
  python scripts/export.py [OPTIONS]

Options:
  --since all|last   Export all chats or only those updated since last export.
                     Default: all
  --out DIR          Output directory. Default: current working directory (.)
  --no-zip           Write individual Markdown files instead of a zip archive.
  --no-composer      Exclude composer logs (export only chat logs).
  --exclude-rules P  Path to exclusion rules file (sensitive projects/chats are omitted).
                     If omitted, uses ~/.cursor-chat-browser/exclusion-rules.txt if present.
  --help             Show this help message and exit.
"""


def parse_args():
    args = sys.argv[1:]
    out = {"since": "all", "out_dir": ".", "include_composer": True, "zip": True, "exclusion_rules_path": None}
    i = 0
    while i < len(args):
        if args[i] in ("--help", "-h"):
            print(HELP_TEXT)
            sys.exit(0)
        elif args[i] == "--since" and i + 1 < len(args):
            i += 1
            out["since"] = args[i]
        elif args[i] == "--out" and i + 1 < len(args):
            i += 1
            out["out_dir"] = args[i]
        elif args[i] in ("--exclude-rules", "-e") and i + 1 < len(args):
            i += 1
            out["exclusion_rules_path"] = args[i]
        elif args[i] == "--no-composer":
            out["include_composer"] = False
        elif args[i] == "--no-zip":
            out["zip"] = False
        i += 1
    return out


def main():
    opts = parse_args()
    since = opts["since"]
    out_dir = os.path.abspath(opts["out_dir"])
    use_zip = opts["zip"]
    exclusion_rules = load_rules(resolve_exclusion_rules_path(opts.get("exclusion_rules_path")))
    workspace_path = resolve_workspace_path()
    global_path = os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))

    if not os.path.isfile(global_path):
        print(f"Cursor global storage not found: {global_path}", file=sys.stderr)
        sys.exit(1)

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

    conn = sqlite3.connect(f"file:{global_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Build workspace entries
    workspace_entries = []
    try:
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if os.path.isdir(full):
                wp = os.path.join(full, "workspace.json")
                if os.path.isfile(wp):
                    workspace_entries.append({"name": name, "workspaceJsonPath": wp})
    except Exception:
        pass

    workspace_path_to_id = {}
    project_name_to_ws = {}
    workspace_id_to_slug = {}
    workspace_id_to_display_name: dict[str, str] = {}  # human-readable, URL-decoded folder name
    for e in workspace_entries:
        try:
            with open(e["workspaceJsonPath"], "r", encoding="utf-8") as f:
                wd = json.load(f)
            first_folder = wd.get("folder") or (wd.get("folders", [{}])[0] or {}).get("path")
            if first_folder:
                from urllib.parse import unquote as _unquote
                fn = re.sub(r"^file://", "", first_folder).replace("\\", "/").split("/")[-1]
                if fn:
                    workspace_id_to_slug[e["name"]] = slug(fn)
                    workspace_id_to_display_name[e["name"]] = _unquote(fn)
            for folder in get_workspace_folder_paths(wd):
                norm = normalize_file_path(folder)
                workspace_path_to_id[norm] = e["name"]
                fn2 = re.sub(r"^file://", "", folder).replace("\\", "/").split("/")[-1]
                if fn2:
                    project_name_to_ws[fn2] = e["name"]
        except Exception:
            pass

    # Project layouts
    project_layouts_map = {}
    try:
        for row in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"):
            parts = row["key"].split(":")
            if len(parts) < 2:
                continue
            cid = parts[1]
            try:
                ctx = json.loads(row["value"])
                layouts = ctx.get("projectLayouts")
                if isinstance(layouts, list):
                    project_layouts_map.setdefault(cid, [])
                    for l in layouts:
                        try:
                            o = json.loads(l) if isinstance(l, str) else l
                            if isinstance(o, dict) and o.get("rootPath"):
                                project_layouts_map[cid].append(o["rootPath"])
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    # Bubble map
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

    # Code block diffs
    code_block_diff_map = {}
    try:
        for row in conn.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"):
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

    # Process composers
    composer_rows = conn.execute(
        "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%' AND value LIKE '%fullConversationHeadersOnly%'"
    ).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    exported = []
    count = 0

    for row in composer_rows:
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
        searchable = build_searchable_text(
            project_name=ws_display_name,
            chat_title=title,
            model_names=model_names,
        )
        if is_excluded_by_rules(exclusion_rules, searchable):
            continue
        title_slug = slug(title)
        ts = updated_at or int(datetime.now().timestamp() * 1000)
        ts_str = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%dT%H-%M-%S")
        filename = f"{ts_str}__{title_slug}__{composer_id[:8]}.md"
        rel_dir = os.path.join(today, ws_slug, "chat")
        out_path = os.path.join(out_dir, rel_dir, filename)

        # Build bubbles
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

            tool_calls = None
            if has_tool:
                tfd = b["toolFormerData"]
                tool_calls = [{
                    "name": tfd.get("name"),
                    "params": tfd.get("params") if isinstance(tfd.get("params"), str) else tfd.get("rawArgs"),
                    "result": (tfd.get("result") or "") if isinstance(tfd.get("result"), str) else None,
                    "status": tfd.get("status"),
                }]

            thinking = None
            if b.get("thinking"):
                thinking = b["thinking"] if isinstance(b["thinking"], str) else (b["thinking"].get("text") if isinstance(b["thinking"], dict) else None)

            bubbles.append({
                "type": btype,
                "text": text,
                "timestamp": to_epoch_ms(b.get("createdAt")) or to_epoch_ms(b.get("timestamp")) or int(datetime.now().timestamp() * 1000),
                "toolCalls": tool_calls,
                "thinking": thinking,
            })

        # Code block diffs
        for d in code_block_diff_map.get(composer_id, []):
            bubbles.append({
                "type": "ai",
                "text": f"**Code edit:** {json.dumps(d)}",
                "timestamp": to_epoch_ms(cd.get("lastUpdatedAt")) or to_epoch_ms(cd.get("createdAt")) or int(datetime.now().timestamp() * 1000),
            })

        bubbles.sort(key=lambda b: b.get("timestamp") or 0)

        # Frontmatter
        fm = {
            "log_id": composer_id,
            "log_type": "chat",
            "title": title,
            "created_at": datetime.fromtimestamp((to_epoch_ms(cd.get("createdAt")) or ts) / 1000).isoformat(),
            "updated_at": datetime.fromtimestamp(updated_at / 1000).isoformat() if updated_at else datetime.now().isoformat(),
            "workspace_id": ws_id,
            "workspace_path": None if ws_id == "global" else ws_id,
            "storage_kind": "global",
            "message_count": len(bubbles),
        }
        total_tc = sum(len(b.get("toolCalls") or []) for b in bubbles)
        total_think = sum(1 for b in bubbles if b.get("thinking"))
        if total_tc:
            fm["tool_calls_count"] = total_tc
        if total_think:
            fm["thinking_count"] = total_think

        # Body
        body = ""
        for bubble in bubbles:
            role = "user" if bubble["type"] == "user" else "assistant"
            body += f"### {role}\n\n"
            if bubble.get("timestamp"):
                body += f"_{datetime.fromtimestamp(bubble['timestamp'] / 1000).isoformat()}_\n\n"
            if bubble.get("thinking"):
                body += f"<details><summary>Thinking</summary>\n\n{bubble['thinking']}\n\n</details>\n\n"
            body += bubble["text"] + "\n\n"
            if bubble.get("toolCalls"):
                for tc in bubble["toolCalls"]:
                    body += f"> **Tool: {tc.get('name', 'unknown')}**"
                    if tc.get("status"):
                        body += f" ({tc['status']})"
                    body += "\n"
                    if tc.get("params"):
                        body += f"> **INPUT:**\n> ```\n"
                        for pline in str(tc['params']).split("\n"):
                            body += f"> {pline}\n"
                        body += f"> ```\n"
                    if tc.get("result"):
                        body += f"> **OUTPUT:**\n> ```\n"
                        for rline in str(tc['result']).split("\n"):
                            body += f"> {rline}\n"
                        body += f"> ```\n"
                    body += "\n"
            body += "---\n\n"

        # Assemble markdown
        fm_str = "---\n"
        for k, v in fm.items():
            if v is None:
                fm_str += f"{k}: null\n"
            elif isinstance(v, dict):
                fm_str += f"{k}: {json.dumps(v)}\n"
            else:
                fm_str += f"{k}: {v}\n"
        fm_str += "---\n\n"
        md = fm_str + body

        rel_path = os.path.join(today, ws_slug, "chat", filename)
        exported.append({"id": composer_id, "rel_path": rel_path, "content": md,
                         "out_path": out_path, "updatedAt": updated_at})
        count += 1

    conn.close()

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
            for e in exported:
                zf.writestr(e["rel_path"], e["content"])
        print(f"Exported {count} chat(s) to {zip_path}")
    else:
        # Write individual Markdown files to disk
        for e in exported:
            os.makedirs(os.path.dirname(e["out_path"]), exist_ok=True)
            with open(e["out_path"], "w", encoding="utf-8") as f:
                f.write(e["content"])

        # Manifest
        manifest_path = os.path.join(out_dir, "manifest.jsonl")
        existing = {}
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                entry = json.loads(line)
                                if entry.get("log_id"):
                                    existing[entry["log_id"]] = entry
                            except Exception:
                                pass
            except Exception:
                pass

        for e in exported:
            existing[e["id"]] = {
                "log_id": e["id"],
                "path": os.path.relpath(e["out_path"], out_dir),
                "updated_at": datetime.fromtimestamp(e["updatedAt"] / 1000).isoformat() if e["updatedAt"] else datetime.now().isoformat(),
            }
        if existing:
            with open(manifest_path, "w", encoding="utf-8") as f:
                for entry in existing.values():
                    f.write(json.dumps(entry) + "\n")
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
