"""Text extraction helpers mirroring the bubble/richText parsing in the Node.js codebase."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol


class HasBubbleRaw(Protocol):
    """Bubble model or any object exposing a Cursor JSON ``raw`` dict."""

    @property
    def raw(self) -> dict[str, Any]: ...


def extract_text_from_rich_text(children: list) -> str:
    """Recursively extract text from a Lexical rich-text tree."""
    if not isinstance(children, list):
        return ""
    text = ""
    for child in children:
        if not isinstance(child, dict):
            continue
        if child.get("type") == "text" and child.get("text"):
            text += child["text"]
        elif child.get("type") == "code" and child.get("children"):
            text += "\n```\n" + extract_text_from_rich_text(child["children"]) + "\n```\n"
        elif child.get("children") and isinstance(child["children"], list):
            text += extract_text_from_rich_text(child["children"])
    return text


def extract_text_from_bubble(bubble: HasBubbleRaw | dict[str, Any]) -> str:
    """Extract displayable text from a bubble object (text, richText, codeBlocks)."""
    raw: dict[str, Any] = bubble if isinstance(bubble, dict) else bubble.raw
    if not raw:
        return ""
    bubble = raw

    text = ""

    # Try text field first (coerce non-str values — Cursor payloads can drift)
    if bubble.get("text") and str(bubble["text"]).strip():
        text = str(bubble["text"])

    # Fall back to richText
    if not text and bubble.get("richText"):
        try:
            rich = json.loads(bubble["richText"]) if isinstance(bubble["richText"], str) else bubble["richText"]
            if isinstance(rich, dict) and rich.get("root") and rich["root"].get("children"):
                text = extract_text_from_rich_text(rich["root"]["children"])
        except Exception:
            pass

    # Append code blocks if present
    code_blocks = bubble.get("codeBlocks")
    if isinstance(code_blocks, list):
        for cb in code_blocks:
            if isinstance(cb, dict) and cb.get("content"):
                lang = cb.get("language", "")
                text += f"\n\n```{lang}\n{cb['content']}\n```"

    return text if isinstance(text, str) else ""


def slug(s: str) -> str:
    """Convert a string to a filesystem-safe slug (max 80 chars)."""
    s = re.sub(r'[<>:"/\\|?*]', "_", s or "")
    s = re.sub(r"\s+", "-", s)
    s = re.sub(r"-+", "-", s)
    s = s.strip("-")
    return s[:80] or "untitled"


def format_tool_action(action: dict) -> str:
    """Format a tool action / codeBlockDiff into readable text."""
    if not action:
        return ""
    result = ""

    # Code changes
    diffs = action.get("newModelDiffWrtV0")
    if isinstance(diffs, list) and diffs:
        for diff in diffs:
            modified = diff.get("modified") if isinstance(diff, dict) else None
            if isinstance(modified, list) and modified:
                result += f"\n\n**Code Changes:**\n```\n{chr(10).join(modified)}\n```"

    if action.get("filePath"):
        result += f"\n\n**File:** {action['filePath']}"
    if action.get("command"):
        result += f"\n\n**Command:** `{action['command']}`"
    if action.get("searchResults"):
        result += f"\n\n**Search Results:**\n{action['searchResults']}"
    if action.get("webResults"):
        result += f"\n\n**Web Search:**\n{action['webResults']}"

    # Tool actions with specific types
    if action.get("toolName"):
        result += f"\n\n**Tool Action:** {action['toolName']}"
        params = action.get("parameters")
        if params:
            try:
                p = json.loads(params) if isinstance(params, str) else params
                if isinstance(p, dict):
                    if p.get("command"):
                        result += f"\n**Command:** `{p['command']}`"
                    if p.get("target_file"):
                        result += f"\n**File:** {p['target_file']}"
                    if p.get("query"):
                        result += f"\n**Query:** {p['query']}"
                    if p.get("instructions"):
                        result += f"\n**Instructions:** {p['instructions']}"
            except Exception:
                pass

        action_result = action.get("result")
        if action_result:
            try:
                rd = json.loads(action_result) if isinstance(action_result, str) else action_result
                if isinstance(rd, dict):
                    if rd.get("output"):
                        result += f"\n\n**Output:**\n```\n{rd['output']}\n```"
                    if rd.get("contents"):
                        result += f"\n\n**File Contents:**\n```\n{rd['contents']}\n```"
                    if rd.get("exitCodeV2") is not None:
                        result += f"\n\n**Exit Code:** {rd['exitCodeV2']}"
                    files = rd.get("files")
                    if isinstance(files, list) and files:
                        result += "\n\n**Files Found:**"
                        for f in files:
                            fname = f.get("name") or f.get("path", "")
                            ftype = f.get("type", "file")
                            result += f"\n- {fname} ({ftype})"
                    results_list = rd.get("results")
                    if isinstance(results_list, list) and results_list:
                        result += "\n\n**Results:**"
                        for sr in results_list:
                            if isinstance(sr, dict) and sr.get("file") and sr.get("content"):
                                result += f"\n\n**File:** {sr['file']}"
                                result += f"\n```\n{sr['content']}\n```"
            except Exception:
                pass

    if isinstance(action.get("actionsTaken"), list) and action["actionsTaken"]:
        result += f"\n\n**Actions Taken:** {', '.join(action['actionsTaken'])}"
    if isinstance(action.get("filesModified"), list) and action["filesModified"]:
        result += "\n\n**Files Modified:**"
        for f in action["filesModified"]:
            result += f"\n- {f}"
    if action.get("gitStatus"):
        result += f"\n\n**Git Status:**\n```\n{action['gitStatus']}\n```"
    if action.get("directoryListed"):
        result += f"\n\n**Directory Listed:** {action['directoryListed']}"
    if isinstance(action.get("webSearchResults"), list):
        result += "\n\n**Web Search Results:**"
        for sr in action["webSearchResults"]:
            if isinstance(sr, dict) and sr.get("title"):
                result += f"\n- {sr['title']}"

    return result
