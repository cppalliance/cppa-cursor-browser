"""
Reader for Cursor CLI (``agent``) chat sessions.

Storage layout:
  ~/.cursor/chats/{project_id}/{session_id}/store.db

Each ``store.db`` is a SQLite database with two tables:

``meta``
    One row, key ``"0"``, value is a hex-encoded JSON string:
    ``{"agentId": "...", "latestRootBlobId": "...", "name": "...",
       "mode": "...", "createdAt": <ms>}``

``blobs``
    Content-addressed store keyed by SHA-256 hex.  Blobs are either:

    * **JSON messages** — raw UTF-8 bytes that parse as a dict with a
      ``"role"`` field.  Roles: ``system``, ``user``, ``assistant``,
      ``tool``.  Content follows the Vercel AI SDK format (string or array
      of typed parts).
    * **Binary chain nodes** — protobuf-like bytes where each 32-byte
      run prefixed by ``0a 20`` is a SHA-256 reference to another blob.
      These form the linked-list structure of the conversation.

Conversation reconstruction: BFS from ``latestRootBlobId``, collecting
JSON-decodable blobs in traversal order.

User messages contain a ``<user_info>..Workspace Path: ..</user_info>``
preamble injected by the CLI.  This is used to derive the workspace name
for a project.
"""

from __future__ import annotations

import json
from models import CliSessionMeta, SchemaError
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from typing import Generator


# ---------------------------------------------------------------------------
# Low-level store.db helpers
# ---------------------------------------------------------------------------

def _read_meta(db_path: str) -> dict:
    """Read and decode the session metadata row from a ``store.db``."""
    # `closing(...)` guarantees .close() on scope exit (issue #17).
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
            if row and row[0]:
                return json.loads(bytes.fromhex(row[0]).decode("utf-8"))
        except Exception:
            pass
    return {}


def _extract_blob_refs(data: bytes) -> list[str]:
    """Extract all 32-byte (SHA-256) blob references from a binary chain node.

    The encoding is: tag ``0x0a`` (field 1, length-delimited) followed by
    ``0x20`` (length = 32), followed by the 32-byte hash.
    """
    refs: list[str] = []
    i = 0
    while i + 33 < len(data):
        if data[i] == 0x0A and data[i + 1] == 0x20:
            refs.append(data[i + 2 : i + 34].hex())
            i += 34
        else:
            i += 1
    return refs


def traverse_blobs(db_path: str) -> list[dict]:
    """Reconstruct the conversation from a ``store.db`` blob graph.

    Starting from ``latestRootBlobId``, performs BFS over the blob DAG:
    - JSON blobs  → collected as conversation messages (preserving order)
    - Binary blobs → their SHA-256 references are queued for further traversal

    Returns a list of message dicts ``{"role": ..., "content": ...}`` in
    conversation order.  ``system`` messages are included; callers may filter
    them as needed.
    """
    # `closing(...)` guarantees .close() on scope exit (issue #17). Connection
    # is only needed to materialise the blob graph; subsequent BFS works on
    # in-memory dicts, so the connection is released as soon as we're done
    # reading.
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        meta_row = conn.execute("SELECT value FROM meta WHERE key = '0'").fetchone()
        if not meta_row or not meta_row[0]:
            return []
        try:
            meta = CliSessionMeta.from_dict(
                json.loads(bytes.fromhex(meta_row[0]).decode("utf-8"))
            )
        except SchemaError as e:
            print(f"Schema drift in CLI session meta at {db_path}: {e}")
            return []
        root_id: str = meta.latest_root_blob_id

        # Load all blobs, classifying each as JSON or binary
        json_blobs: dict[str, dict] = {}
        chain_blobs: dict[str, list[str]] = {}

        for blob_id, data in conn.execute("SELECT id, data FROM blobs"):
            if not isinstance(data, bytes):
                continue
            try:
                msg = json.loads(data.decode("utf-8"))
                if isinstance(msg, dict) and "role" in msg:
                    json_blobs[blob_id] = msg
                    continue
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            refs = _extract_blob_refs(data)
            chain_blobs[blob_id] = refs

    # BFS from root (newest-first by nature of the linked-list structure);
    # reverse at the end to restore chronological (oldest→newest) order.
    from collections import deque

    visited: set[str] = set()
    queue: deque[str] = deque([root_id])
    messages: list[dict] = []

    while queue:
        bid = queue.popleft()
        if bid in visited:
            continue
        visited.add(bid)

        if bid in json_blobs:
            messages.append(json_blobs[bid])
        elif bid in chain_blobs:
            for ref in chain_blobs[bid]:
                if ref not in visited:
                    queue.append(ref)

    messages.reverse()
    return messages


# ---------------------------------------------------------------------------
# Content extraction helpers
# ---------------------------------------------------------------------------

_USER_INFO_RE = re.compile(r"<user_info>.*?</user_info>", re.DOTALL)
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
_WORKSPACE_PATH_RE = re.compile(r"Workspace Path:\s*(.+?)(?:\n|$)")


def _content_to_text(content) -> str:
    """Flatten Vercel AI SDK content (string or typed-part array) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
                elif part.get("type") == "tool-result":
                    result = part.get("result", "")
                    parts.append(str(result) if result else "")
        return "\n".join(p for p in parts if p)
    return ""


def _extract_tool_calls(content) -> list[dict]:
    """Extract tool-call parts from assistant message content."""
    if not isinstance(content, list):
        return []
    calls: list[dict] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool-call":
            calls.append({
                "name": part.get("toolName", "unknown"),
                "args": part.get("args", {}),
                "toolCallId": part.get("toolCallId", ""),
            })
    return calls


def extract_workspace_path(messages: list[dict]) -> str | None:
    """Extract the workspace path from the ``<user_info>`` preamble in the
    first user message that contains one."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        text = content if isinstance(content, str) else _content_to_text(content)
        m = _WORKSPACE_PATH_RE.search(text)
        if m:
            return m.group(1).strip()
    return None


def _strip_user_info(text: str) -> str:
    """Remove the ``<user_info>`` preamble and return only the query text.

    If a ``<user_query>`` tag is present, its content is returned directly.
    Otherwise the user_info block is stripped and the remainder is returned.
    """
    qm = _USER_QUERY_RE.search(text)
    if qm:
        return qm.group(1).strip()
    return _USER_INFO_RE.sub("", text).strip()


def messages_to_bubbles(messages: list[dict], created_at_ms: int) -> list[dict]:
    """Convert CLI message dicts to the bubble format used by the browser UI.

    Each bubble has:
    ``{"type": "user"|"ai", "text": str, "timestamp": int, "metadata": dict|None}``

    Conversion rules:
    - ``system`` messages are skipped.
    - ``user`` messages: strip ``<user_info>`` preamble, keep query text.
    - ``assistant`` messages with text parts → ``type: "ai"`` bubble.
    - ``assistant`` messages with ``tool-call`` parts → ``type: "ai"`` bubble
      with ``metadata.toolCalls``.  The ``output`` field of each tool call is
      populated from the corresponding ``role: "tool"`` result message (matched
      by ``toolCallId``).
    - ``tool`` (result) messages are used only to populate tool-call outputs;
      they are not emitted as separate bubbles.

    Per-message timestamps are unavailable in CLI sessions; ``created_at_ms``
    is used for all bubbles with a 1 ms sequence offset so UI sorting is stable.
    """
    # Pre-scan: build toolCallId → output text map from role=="tool" messages.
    tool_outputs: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "tool-result":
                    tid = part.get("toolCallId", "")
                    result = part.get("result", "")
                    if tid:
                        tool_outputs[tid] = str(result) if result else ""
        elif isinstance(content, str) and content:
            # Plain string result with no toolCallId — store under empty key
            # only if not already set, to avoid clobbering a keyed entry.
            tool_outputs.setdefault("", content)

    bubbles: list[dict] = []
    seq = 0

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role in ("system", "tool"):
            continue

        ts = created_at_ms + seq
        seq += 1

        if role == "user":
            text = _content_to_text(content) if isinstance(content, list) else (content or "")
            # Skip pure preamble messages (contain <user_info> but no <user_query>).
            if "<user_info>" in text and "<user_query>" not in text:
                continue
            text = _strip_user_info(text)
            if not text:
                continue
            bubbles.append({"type": "user", "text": text, "timestamp": ts})

        elif role == "assistant":
            text = _content_to_text(content) if isinstance(content, list) else (content or "")
            tool_calls = _extract_tool_calls(content)

            if not text.strip() and not tool_calls:
                continue

            bubble: dict = {"type": "ai", "text": text, "timestamp": ts}
            if tool_calls:
                # Convert to the format parse_tool_call returns
                formatted_calls = []
                for tc in tool_calls:
                    args = tc.get("args", {})
                    name = tc.get("name", "unknown")
                    tid = tc.get("toolCallId", "")
                    # Build a human-readable summary
                    if name in ("Glob", "glob_file_search"):
                        pattern = args.get("glob_pattern") or args.get("pattern") or ""
                        summary = f"Glob: {pattern}"
                        input_display = pattern
                    elif name in ("Read", "read_file_v2"):
                        fp = args.get("path") or args.get("targetFile") or ""
                        summary = f"Read: {fp}"
                        input_display = fp
                    elif name in ("Shell", "run_terminal_command_v2"):
                        cmd = args.get("command") or ""
                        summary = f"Terminal: {cmd[:80]}"
                        input_display = cmd
                    elif name in ("Grep",):
                        pattern = args.get("pattern") or ""
                        summary = f"Search: /{pattern}/"
                        input_display = pattern
                    elif name in ("WebSearch", "web_search"):
                        q = args.get("search_term") or args.get("query") or ""
                        summary = f"Web search: {q[:60]}"
                        input_display = q
                    elif name in ("WebFetch", "web_fetch"):
                        url = args.get("url") or ""
                        summary = f"Fetch: {url[:60]}"
                        input_display = url
                    else:
                        summary = name
                        input_display = json.dumps(args, indent=2) if args else ""
                    formatted_calls.append({
                        "name": name,
                        "status": "",
                        "summary": summary,
                        "input": input_display,
                        "output": tool_outputs.get(tid, ""),
                        "toolCallId": tid,
                    })
                if not text.strip():
                    tc0 = formatted_calls[0]
                    bubble["text"] = f"**Tool: {tc0['summary']}**"
                bubble["metadata"] = {"toolCalls": formatted_calls}
            bubbles.append(bubble)

    return bubbles


# ---------------------------------------------------------------------------
# Project / session enumeration
# ---------------------------------------------------------------------------

def iter_sessions(chats_path: str) -> Generator[dict, None, None]:
    """Yield one dict per CLI session under ``chats_path``.

    Each dict contains:
    ``{"project_id", "session_id", "db_path", "meta"}``
    where ``meta`` is the decoded session metadata (may be ``{}`` on error).
    """
    if not os.path.isdir(chats_path):
        return
    for project_id in os.listdir(chats_path):
        project_path = os.path.join(chats_path, project_id)
        if not os.path.isdir(project_path):
            continue
        for session_id in os.listdir(project_path):
            session_path = os.path.join(project_path, session_id)
            db_path = os.path.join(session_path, "store.db")
            if not os.path.isfile(db_path):
                continue
            try:
                meta = _read_meta(db_path)
            except Exception:
                meta = {}
            yield {
                "project_id": project_id,
                "session_id": session_id,
                "db_path": db_path,
                "meta": meta,
            }


def list_cli_projects(chats_path: str) -> list[dict]:
    """Return one dict per CLI project (unique ``project_id``).

    Each dict:
    ``{"project_id", "workspace_path", "workspace_name", "sessions", "last_updated_ms"}``

    where ``sessions`` is a list of session dicts (``project_id``,
    ``session_id``, ``db_path``, ``meta``), ``workspace_path`` is extracted
    from the first user message found, and ``workspace_name`` is the last
    path segment of ``workspace_path``.
    """
    projects: dict[str, dict] = {}

    for session in iter_sessions(chats_path):
        pid = session["project_id"]
        if pid not in projects:
            projects[pid] = {
                "project_id": pid,
                "workspace_path": None,
                "workspace_name": None,
                "sessions": [],
                "last_updated_ms": 0,
            }
        projects[pid]["sessions"].append(session)

        created = session["meta"].get("createdAt") or 0
        try:
            mtime_ms = int(os.path.getmtime(session["db_path"]) * 1000)
        except OSError:
            mtime_ms = 0
        session_ts = max(created, mtime_ms)
        if session_ts > projects[pid]["last_updated_ms"]:
            projects[pid]["last_updated_ms"] = session_ts

    # Resolve workspace path / name from a session's messages
    for pid, proj in projects.items():
        if proj["workspace_path"]:
            continue
        for session in proj["sessions"]:
            try:
                msgs = traverse_blobs(session["db_path"])
                ws_path = extract_workspace_path(msgs)
                if ws_path:
                    proj["workspace_path"] = ws_path
                    parts = ws_path.replace("\\", "/").rstrip("/").split("/")
                    proj["workspace_name"] = parts[-1] if parts else ws_path
                    break
            except Exception:
                continue

        if not proj["workspace_name"]:
            proj["workspace_name"] = pid[:12]

    return list(projects.values())


# ---------------------------------------------------------------------------
# Aggregate statistics for a project's sessions
# ---------------------------------------------------------------------------

def aggregate_session_stats(session: dict) -> dict:
    """Return aggregate statistics for one CLI session.

    Reads and converts blobs, then counts tool calls and computes wall-clock
    duration.  Returns a dict suitable for embedding in Markdown frontmatter.
    """
    meta = session.get("meta", {})
    created_ms: int = meta.get("createdAt") or int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    try:
        messages = traverse_blobs(session["db_path"])
    except Exception:
        return {"created_ms": created_ms, "messages": []}

    bubbles = messages_to_bubbles(messages, created_ms)

    total_tool_calls = 0
    tool_breakdown: dict[str, int] = {}
    for b in bubbles:
        tcs = (b.get("metadata") or {}).get("toolCalls") or []
        total_tool_calls += len(tcs)
        for tc in tcs:
            tn = tc.get("name", "unknown")
            tool_breakdown[tn] = tool_breakdown.get(tn, 0) + 1

    return {
        "created_ms": created_ms,
        "messages": messages,
        "bubbles": bubbles,
        "total_tool_calls": total_tool_calls,
        "tool_breakdown": tool_breakdown,
        "mode": meta.get("mode", ""),
        "session_name": meta.get("name", ""),
    }
