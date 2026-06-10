"""
Shared utility for parsing Cursor's toolFormerData into structured tool call objects.
Used by both workspaces.py (browser API) and export_api.py (bulk export).
"""

from __future__ import annotations

import json
from typing import Any


def short_path(p: str) -> str:
    """Shorten a file path for display."""
    if not p:
        return ""
    parts = p.replace("\\", "/").split("/")
    if len(parts) > 3:
        return ".../" + "/".join(parts[-3:])
    return p


def parse_tool_call(tfd: dict[str, Any]) -> dict[str, str]:
    """Parse toolFormerData into a structured tool call object with human-readable summaries."""
    name = tfd.get("name") or "unknown"
    status = tfd.get("status") or ""

    # Parse params — try params first, then rawArgs
    params_raw = tfd.get("params") or tfd.get("rawArgs") or ""
    params_parsed = {}
    if isinstance(params_raw, str) and params_raw:
        try:
            params_parsed = json.loads(params_raw)
        except Exception:
            pass
    elif isinstance(params_raw, dict):
        params_parsed = params_raw

    # Parse result
    result_raw = tfd.get("result") or ""
    result_parsed = {}
    if isinstance(result_raw, str) and result_raw:
        try:
            result_parsed = json.loads(result_raw)
        except Exception:
            result_parsed = {"output": result_raw}

    # Build human-readable summary and structured output based on tool name
    summary = ""
    input_display = ""
    output_display = ""

    if name == "read_file_v2":
        fp = params_parsed.get("targetFile") or params_parsed.get("path") or ""
        offset = params_parsed.get("offset")
        limit = params_parsed.get("limit")
        range_str = ""
        if offset is not None and limit is not None:
            range_str = f" (lines {offset}-{offset + limit})"
        elif offset is not None:
            range_str = f" (from line {offset})"
        summary = f"Read: {short_path(fp)}{range_str}"
        input_display = fp
        contents = result_parsed.get("contents") or ""
        output_display = contents

    elif name == "edit_file_v2":
        fp = params_parsed.get("relativeWorkspacePath") or params_parsed.get("targetFile") or ""
        summary = f"Edit: {short_path(fp)}"
        input_display = fp
        before_id = result_parsed.get("beforeContentId", "")
        after_id = result_parsed.get("afterContentId", "")
        if before_id or after_id:
            output_display = "File modified"
        streaming = params_parsed.get("streamingContent") or ""
        if streaming:
            output_display = streaming

    elif name == "run_terminal_command_v2":
        cmd = params_parsed.get("command") or ""
        summary = f"Terminal: {cmd[:80]}{'...' if len(cmd) > 80 else ''}"
        input_display = cmd
        output = result_parsed.get("output") or ""
        output_display = output

    elif name == "ripgrep_raw_search":
        pattern = params_parsed.get("pattern") or ""
        path = params_parsed.get("path") or ""
        summary = f"Search: /{pattern}/ in {short_path(path)}"
        input_display = f"Pattern: {pattern}\nPath: {path}"
        success = result_parsed.get("success", {})
        if isinstance(success, dict):
            ws_results = success.get("workspaceResults", {})
            if isinstance(ws_results, dict):
                all_content = []
                for ws_path_key, ws_data in ws_results.items():
                    if isinstance(ws_data, dict):
                        content = ws_data.get("content", {})
                        if isinstance(content, dict):
                            for match_file, match_data in content.items():
                                all_content.append(f"# {match_file}")
                                if isinstance(match_data, dict):
                                    lines = match_data.get("lines") or match_data.get("content") or ""
                                    if lines:
                                        all_content.append(str(lines))
                output_display = "\n".join(all_content) if all_content else "No matches"
            else:
                output_display = str(success)
        else:
            output_display = str(result_parsed)

    elif name == "semantic_search_full":
        query = params_parsed.get("query") or ""
        summary = f"Semantic search: {query[:60]}"
        input_display = query
        code_results = result_parsed.get("codeResults", [])
        if isinstance(code_results, list):
            lines = []
            for cr in code_results:
                if isinstance(cr, dict):
                    cb = cr.get("codeBlock", {})
                    if isinstance(cb, dict):
                        fp = cb.get("relativeWorkspacePath") or ""
                        lines.append(f"# {fp}")
                        contents = cb.get("contents") or ""
                        if contents:
                            lines.append(contents)
            output_display = "\n".join(lines) if lines else "No results"

    elif name == "glob_file_search":
        pattern = params_parsed.get("pattern") or params_parsed.get("glob") or params_parsed.get("query") or ""
        summary = f"Glob: {pattern}" if pattern else "Glob search"
        input_display = json.dumps(params_parsed, indent=2) if params_parsed else pattern
        files = result_parsed.get("files") or result_parsed.get("results") or []
        if isinstance(files, list):
            output_display = "\n".join(str(f) for f in files)

    elif name == "list_dir_v2":
        path = params_parsed.get("path") or params_parsed.get("relativeWorkspacePath") or ""
        summary = f"List dir: {short_path(path)}"
        input_display = path
        output_display = str(result_parsed)

    elif name == "web_search":
        query = params_parsed.get("searchTerm") or params_parsed.get("query") or params_parsed.get("search_term") or ""
        summary = f"Web search: {query[:60]}" if query else "Web search"
        input_display = query
        output_display = str(result_parsed)

    elif name == "web_fetch":
        url = params_parsed.get("url") or ""
        summary = f"Fetch: {url[:60]}"
        input_display = url
        output_display = str(result_parsed)

    elif name == "todo_write":
        summary = "Todo write"
        todos = result_parsed.get("finalTodos") or []
        if isinstance(todos, list):
            lines = []
            for t in todos:
                if isinstance(t, dict):
                    lines.append(f"[{t.get('status', '?')}] {t.get('content', '')}")
            output_display = "\n".join(lines)

    elif name == "task_v2":
        desc = params_parsed.get("description") or ""
        summary = f"Task: {desc[:60]}"
        input_display = desc
        output_display = str(result_parsed)

    elif name == "read_lints":
        path = params_parsed.get("path") or ""
        summary = f"Read lints: {short_path(path)}"
        output_display = str(result_parsed)

    else:
        # Generic fallback — preserve full content for all tool types
        summary = f"{name}"
        input_display = json.dumps(params_parsed, indent=2) if params_parsed else ""
        output_display = json.dumps(result_parsed, indent=2) if result_parsed else ""

    return {
        "name": name,
        "status": status,
        "summary": summary,
        "input": input_display,
        "output": output_display,
    }
