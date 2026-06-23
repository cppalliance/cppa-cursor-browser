"""Disk cache for derived workspace summaries (issue #84 Phase 3).

Caches project lists and per-workspace tab summaries keyed by storage mtimes
so repeat page loads avoid re-scanning Cursor's global KV index.

Bypass: set env ``CURSOR_CHAT_BROWSER_NOCACHE=1`` or pass ``?nocache=1`` on API
requests. Cache files live under ``~/.cache/cursor-chat-browser/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

CACHE_VERSION = 1
CACHE_DIR = Path.home() / ".cache" / "cursor-chat-browser"
PROJECTS_CACHE_FILE = CACHE_DIR / "projects.json"
COMPOSER_MAP_CACHE_FILE = CACHE_DIR / "composer-id-to-ws.json"
TAB_SUMMARIES_PREFIX = "tab-summaries-"


def nocache_enabled(*, request_nocache: bool = False) -> bool:
    if request_nocache:
        return True
    return os.environ.get("CURSOR_CHAT_BROWSER_NOCACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _rules_digest(rules: list[Any]) -> str:
    try:
        payload = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        payload = repr(rules)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _file_mtime_ns(path: str | None) -> int | None:
    if not path or not os.path.isfile(path):
        return None
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def fingerprint_workspace_storage(
    workspace_path: str,
    workspace_entries: list[dict[str, Any]],
    *,
    global_db_path: str | None,
    rules: list[Any],
    cli_chats_path: str | None = None,
) -> dict[str, Any]:
    """Build a fingerprint dict for cache invalidation."""
    ws_mt: list[list[str | int]] = []

    def _entry_mtimes(entry: dict[str, Any]) -> list[list[str | int]]:
        rows: list[list[str | int]] = []
        name = entry.get("name")
        if not isinstance(name, str):
            return rows
        base = os.path.join(workspace_path, name)
        for rel in ("state.vscdb", "workspace.json"):
            p = os.path.join(base, rel)
            mtime = _file_mtime_ns(p)
            if mtime is not None:
                rows.append([f"{name}/{rel}", mtime])
        return rows

    if workspace_entries:
        max_workers = min(32, len(workspace_entries))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_entry_mtimes, entry) for entry in workspace_entries]
            for fut in as_completed(futures):
                ws_mt.extend(fut.result())
    ws_mt.sort(key=lambda row: row[0])

    return {
        "version": CACHE_VERSION,
        "workspace_path": os.path.normpath(workspace_path),
        "global_db_mtime_ns": _file_mtime_ns(global_db_path),
        "workspace_files": ws_mt,
        "rules_digest": _rules_digest(rules),
        "cli_chats_mtime_ns": _file_mtime_ns(cli_chats_path),
    }


def _normalize_fingerprint(fp: dict[str, Any]) -> dict[str, Any]:
    """Normalize fingerprint for comparison (JSON round-trip uses lists, not tuples)."""
    normalized = dict(fp)
    wf = fp.get("workspace_files")
    if isinstance(wf, list):
        normalized["workspace_files"] = [
            [row[0], row[1]] if isinstance(row, (list, tuple)) and len(row) == 2 else row
            for row in wf
        ]
    return normalized


def _fingerprint_equal(a: object, b: dict[str, Any]) -> bool:
    if not isinstance(a, dict):
        return False
    return _normalize_fingerprint(a) == _normalize_fingerprint(b)


def _read_cache_file(path: Path | str) -> dict[str, Any] | None:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError) as e:
        _logger.debug("Summary cache read failed for %s: %s", path, e)
        return None


def _write_cache_file(path: Path | str, payload: dict[str, Any]) -> None:
    p = Path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        tmp.replace(p)
    except OSError as e:
        _logger.warning("Summary cache write failed for %s: %s", path, e)


def get_cached_projects(
    fingerprint: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    data = _read_cache_file(PROJECTS_CACHE_FILE)
    if not data:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    projects = data.get("projects")
    warnings = data.get("warnings")
    if not isinstance(projects, list):
        return None
    if not isinstance(warnings, list):
        warnings = []
    return projects, warnings


def set_cached_projects(
    fingerprint: dict[str, Any],
    projects: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    _write_cache_file(
        PROJECTS_CACHE_FILE,
        {
            "fingerprint": fingerprint,
            "projects": projects,
            "warnings": warnings,
        },
    )


def get_cached_composer_id_to_ws(
    fingerprint: dict[str, Any],
) -> dict[str, str] | None:
    data = _read_cache_file(COMPOSER_MAP_CACHE_FILE)
    if not data:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    mapping = data.get("composer_id_to_ws")
    if not isinstance(mapping, dict):
        return None
    return {str(k): str(v) for k, v in mapping.items()}


def set_cached_composer_id_to_ws(
    fingerprint: dict[str, Any],
    mapping: dict[str, str],
) -> None:
    _write_cache_file(
        COMPOSER_MAP_CACHE_FILE,
        {
            "fingerprint": fingerprint,
            "composer_id_to_ws": mapping,
        },
    )


def _tab_summaries_path(workspace_id: str) -> Path:
    safe = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{TAB_SUMMARIES_PREFIX}{safe}.json"


def get_cached_tab_summaries(
    fingerprint: dict[str, Any],
    workspace_id: str,
) -> tuple[dict[str, Any], int] | None:
    data = _read_cache_file(_tab_summaries_path(workspace_id))
    if not data:
        return None
    if data.get("workspace_id") != workspace_id:
        return None
    if not _fingerprint_equal(data.get("fingerprint"), fingerprint):
        return None
    payload = data.get("payload")
    status = data.get("status", 200)
    if not isinstance(payload, dict) or not isinstance(status, int):
        return None
    return payload, status


def set_cached_tab_summaries(
    fingerprint: dict[str, Any],
    workspace_id: str,
    payload: dict[str, Any],
    status: int,
) -> None:
    _write_cache_file(
        _tab_summaries_path(workspace_id),
        {
            "workspace_id": workspace_id,
            "fingerprint": fingerprint,
            "payload": payload,
            "status": status,
        },
    )
