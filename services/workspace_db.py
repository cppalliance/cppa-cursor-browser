from __future__ import annotations

import hashlib
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlite3

_logger = logging.getLogger(__name__)

from utils.path_helpers import get_workspace_folder_paths
from utils.workspace_descriptor import read_json_file


# ── Global-DB KV loaders ────────────────────────────────────────────────────
# Each function accepts an already-opened sqlite3.Connection (row_factory must
# be set to sqlite3.Row by the caller, as open_global_db does) and returns
# a populated dict.  sqlite3.Error is caught internally so a missing or
# corrupt table cannot propagate to callers.


def load_bubble_map(global_db) -> dict[str, dict]:
    """Load all ``bubbleId:*`` KV entries into ``{bubble_id: bubble_dict}``.

    Skips rows whose JSON value is not a dict; JSON parse errors are logged at
    DEBUG level so a single malformed row cannot block the rest.
    """
    bubble_map: dict[str, dict] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
        ).fetchall()
    except sqlite3.Error:
        return bubble_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        bid = parts[2]
        try:
            b = json.loads(row["value"])
            if isinstance(b, dict):
                bubble_map[bid] = b
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed bubbleId row %s: %s", row["key"], e)
    return bubble_map


def _extract_root_paths_from_context(ctx: dict) -> list[str]:
    """Pull ``rootPath`` strings from a messageRequestContext JSON object."""
    paths: list[str] = []
    layouts = ctx.get("projectLayouts")
    if not isinstance(layouts, list):
        return paths
    for layout in layouts:
        try:
            o = json.loads(layout) if isinstance(layout, str) else layout
            if isinstance(o, dict) and o.get("rootPath"):
                paths.append(o["rootPath"])
        except (json.JSONDecodeError, ValueError, KeyError, TypeError):
            continue
    return paths


def load_project_layouts_for_composer(global_db, composer_id: str) -> list[str]:
    """Scoped MRC load: ``messageRequestContext:{composer_id}:%`` only."""
    paths: list[str] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"messageRequestContext:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return paths
    for row in rows:
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                paths.extend(_extract_root_paths_from_context(ctx))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed messageRequestContext row %s: %s",
                row["key"],
                e,
            )
    return paths


_SCOPED_MRC_BATCH_THRESHOLD = 20


def composer_mapped_to_other_workspace(
    composer_id: str,
    composer_id_to_workspace_id: dict[str, str],
    matching_workspace_ids: set[str],
    invalid_workspace_ids: set[str] | None = None,
) -> bool:
    """Return True when definitive mapping assigns *composer_id* elsewhere.

    Unmapped composers and invalid-workspace mappings still need heuristic
    resolution and are not skipped.
    """
    invalid = invalid_workspace_ids or set()
    mapped = composer_id_to_workspace_id.get(composer_id)
    if not mapped or mapped in invalid:
        return False
    return mapped not in matching_workspace_ids


def filter_composer_rows_for_workspace_scope(
    composer_rows: list,
    composer_id_to_workspace_id: dict[str, str],
    matching_workspace_ids: set[str],
    invalid_workspace_ids: set[str] | None = None,
) -> tuple[list, set[str]]:
    """Drop composers definitively owned by another workspace; collect unmapped ids.

    Returns ``(rows_to_process, unmapped_composer_ids)`` where *unmapped* ids
    may need ``projectLayouts`` from messageRequestContext during assignment.
    """
    invalid = invalid_workspace_ids or set()
    filtered: list = []
    unmapped: set[str] = set()
    for row in composer_rows:
        cid = row["key"].split(":")[1]
        mapped = composer_id_to_workspace_id.get(cid)
        if mapped and mapped not in invalid and mapped not in matching_workspace_ids:
            continue
        filtered.append(row)
        if cid not in composer_id_to_workspace_id:
            unmapped.add(cid)
    return filtered, unmapped


def load_project_layouts_for_composers(
    global_db,
    composer_ids: set[str] | frozenset[str],
) -> dict[str, list]:
    """Load ``projectLayouts`` for a set of composers in one or few SQL passes."""
    if not composer_ids:
        return {}
    ids = set(composer_ids)
    if len(ids) <= _SCOPED_MRC_BATCH_THRESHOLD:
        return {
            cid: load_project_layouts_for_composer(global_db, cid)
            for cid in ids
        }
    layouts_map: dict[str, list] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"
        ).fetchall()
    except sqlite3.Error:
        return layouts_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 2:
            continue
        cid = parts[1]
        if cid not in ids:
            continue
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                layouts_map.setdefault(cid, [])
                layouts_map[cid].extend(_extract_root_paths_from_context(ctx))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed messageRequestContext row %s: %s",
                row["key"],
                e,
            )
    return layouts_map


def prefetch_project_layouts_for_unmapped(
    global_db,
    unmapped_composer_ids: set[str],
    project_layouts_map: dict[str, list],
    *,
    invalid_workspace_ids: set[str] | None,
) -> None:
    """Merge batched MRC layouts for *unmapped_composer_ids* into *project_layouts_map*."""
    if invalid_workspace_ids:
        return
    missing = {cid for cid in unmapped_composer_ids if cid not in project_layouts_map}
    if not missing:
        return
    project_layouts_map.update(load_project_layouts_for_composers(global_db, missing))


def load_project_layouts_map(global_db) -> dict[str, list]:
    """Load ``projectLayouts`` from all ``messageRequestContext:*`` KV entries.

    Returns ``{composer_id: [root_path_str, ...]}``.  Prefer
    :func:`load_project_layouts_for_composer` on list paths when only a few
    composers need layout fallbacks.
    """
    layouts_map: dict[str, list] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'messageRequestContext:%'"
        ).fetchall()
    except sqlite3.Error:
        return layouts_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 2:
            continue
        cid = parts[1]
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                layouts_map.setdefault(cid, [])
                layouts_map[cid].extend(_extract_root_paths_from_context(ctx))
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed messageRequestContext row %s: %s", row["key"], e)
    return layouts_map


def load_code_block_diff_map(global_db) -> dict[str, list]:
    """Load ``codeBlockDiff:*`` KV entries into ``{composer_id: [diff_dict]}``.

    Each diff dict contains all fields from the raw JSON value plus a
    ``diffId`` key taken from the third path component of the KV key.
    """
    diff_map: dict[str, list] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'codeBlockDiff:%'"
        ).fetchall()
    except sqlite3.Error:
        return diff_map
    for row in rows:
        parts = row["key"].split(":")
        cid = parts[1] if len(parts) > 1 else None
        if not cid:
            continue
        try:
            d = json.loads(row["value"])
            if isinstance(d, dict):
                diff_map.setdefault(cid, []).append({
                    **d,
                    "diffId": parts[2] if len(parts) > 2 else None,
                })
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed codeBlockDiff row %s: %s", row["key"], e)
    return diff_map


def load_bubbles_for_composer(global_db, composer_id: str) -> dict[str, dict]:
    """Load ``bubbleId:{composer_id}:*`` KV entries into ``{bubble_id: bubble_dict}``.

    Scoped alternative to :func:`load_bubble_map` for single-conversation assembly;
    avoids a full global ``bubbleId:%`` scan.
    """
    bubble_map: dict[str, dict] = {}
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"bubbleId:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return bubble_map
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        bid = parts[2]
        try:
            b = json.loads(row["value"])
            if isinstance(b, dict):
                bubble_map[bid] = b
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug("Skipping malformed bubbleId row %s: %s", row["key"], e)
    return bubble_map


def load_message_request_context_for_composer(
    global_db, composer_id: str
) -> list[dict]:
    """Load ``messageRequestContext:{composer_id}:*`` KV entries.

    Returns a list of context dicts, each with an injected ``contextId`` key
    taken from the third path component of the KV key.  Scoped alternative to
    the global MRC pass inside :func:`load_project_layouts_map`.
    """
    contexts: list[dict] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"messageRequestContext:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return contexts
    for row in rows:
        parts = row["key"].split(":")
        if len(parts) < 3:
            continue
        context_id = parts[2]
        try:
            ctx = json.loads(row["value"])
            if isinstance(ctx, dict):
                contexts.append({**ctx, "contextId": context_id})
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed messageRequestContext row %s: %s",
                row["key"],
                e,
            )
    return contexts


def load_code_block_diffs_for_composer(
    global_db, composer_id: str
) -> list[dict]:
    """Load ``codeBlockDiff:{composer_id}:*`` KV entries.

    Returns a list of diff dicts, each with an injected ``diffId`` key.
    Scoped alternative to :func:`load_code_block_diff_map` for single-conversation
    assembly.
    """
    diffs: list[dict] = []
    try:
        rows = global_db.execute(
            "SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
            (f"codeBlockDiff:{composer_id}:%",),
        ).fetchall()
    except sqlite3.Error:
        return diffs
    for row in rows:
        parts = row["key"].split(":")
        try:
            d = json.loads(row["value"])
            if isinstance(d, dict):
                diffs.append({**d, "diffId": parts[2] if len(parts) > 2 else None})
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as e:
            _logger.debug(
                "Skipping malformed codeBlockDiff row %s: %s", row["key"], e
            )
    return diffs


def collect_workspace_entries(workspace_path: str) -> list[dict]:
    """Scan workspace directory and return entries with workspace.json.

    Args:
        workspace_path: Cursor workspace storage root (parent of per-workspace folders).

    Returns:
        List of dicts with keys ``name`` (folder id) and ``workspaceJsonPath``.
        Returns an empty list if ``workspace_path`` is missing or unreadable.
    """
    entries = []
    try:
        for name in os.listdir(workspace_path):
            full = os.path.join(workspace_path, name)
            if os.path.isdir(full):
                wj = os.path.join(full, "workspace.json")
                if os.path.isfile(wj):
                    entries.append({"name": name, "workspaceJsonPath": wj})
    except OSError:
        # workspace_path missing / not readable / not a directory — return what
        # we have so far. OSError covers FileNotFoundError, PermissionError,
        # and NotADirectoryError.
        pass
    return entries


def collect_invalid_workspace_ids(workspace_entries: list[dict]) -> set[str]:
    """Return workspace IDs whose descriptors have no resolvable folder paths.

    Args:
        workspace_entries: Output of :func:`collect_workspace_entries`.

    Returns:
        Set of workspace folder names that cannot be mapped to a folder path.
    """
    invalid: set[str] = set()
    for entry in workspace_entries:
        try:
            wd = read_json_file(entry["workspaceJsonPath"])
            folders = get_workspace_folder_paths(wd)
            if not folders:
                invalid.add(entry["name"])
        except (OSError, ValueError, KeyError, TypeError):
            # OSError: workspace.json unreadable. ValueError covers
            # json.JSONDecodeError. KeyError / TypeError: malformed entry
            # dict. Any of these mean we can't resolve folders → mark invalid,
            # matching the pre-narrowing behaviour.
            invalid.add(entry["name"])
    return invalid


# Composers that have at least one header entry (list/summary paths).
_COMPOSER_HEADERS_WHERE = (
    " key LIKE 'composerData:%'"
    " AND LENGTH(value) > 10"
    " AND value LIKE '%fullConversationHeadersOnly%'"
    " AND value NOT LIKE '%fullConversationHeadersOnly\":[]%'"
    " AND value NOT LIKE '%fullConversationHeadersOnly\": []%'"
)

COMPOSER_ROWS_WITH_HEADERS_SQL = f"SELECT key, value FROM cursorDiskKV WHERE{_COMPOSER_HEADERS_WHERE}"

COMPOSER_KEYS_WITH_HEADERS_SQL = f"SELECT key FROM cursorDiskKV WHERE{_COMPOSER_HEADERS_WHERE}"

COMPOSER_DATA_KEYS_SQL = "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"

_COMPOSER_FETCH_CHUNK = 400
_LOCAL_DB_READ_WORKERS = 32

# Cursor sidebar placeholder; may appear in local allComposers without global payload.
_NON_CONVERSATION_COMPOSER_IDS = frozenset({"empty-state-draft"})

_registry_memory_cache: tuple[str, "WorkspaceComposerRegistry"] | None = None


@dataclass(frozen=True)
class WorkspaceComposerRegistry:
    """Composer ownership and local sidebar metadata from per-workspace DBs."""

    composer_id_to_ws: dict[str, str]
    composers_by_workspace: dict[str, list[dict[str, Any]]]

    def composer_ids_for_workspaces(self, workspace_ids: set[str]) -> set[str]:
        ids: set[str] = set()
        for ws_id in workspace_ids:
            for composer in self.composers_by_workspace.get(ws_id, []):
                if isinstance(composer, dict):
                    cid = composer.get("composerId")
                    if cid:
                        cid_str = str(cid)
                        if cid_str not in _NON_CONVERSATION_COMPOSER_IDS:
                            ids.add(cid_str)
        return ids


def _composer_id_from_row_key(key: str) -> str:
    return key.split(":", 1)[1]


def fetch_composer_keys_with_headers(global_db) -> list[str]:
    """Return composerData keys with non-empty headers (no value payload)."""
    try:
        rows = global_db.execute(COMPOSER_KEYS_WITH_HEADERS_SQL).fetchall()
    except sqlite3.Error:
        return []
    return [row["key"] for row in rows]


def fetch_composer_data_keys(global_db) -> set[str]:
    """Return all ``composerData:*`` keys without reading values."""
    try:
        rows = global_db.execute(COMPOSER_DATA_KEYS_SQL).fetchall()
    except sqlite3.Error:
        return set()
    return {_composer_id_from_row_key(row["key"]) for row in rows}


def fetch_composer_rows_by_ids(global_db, composer_ids: set[str] | frozenset[str]) -> list:
    """Fetch ``composerData:{id}`` rows for *composer_ids* via batched ``IN`` queries."""
    if not composer_ids:
        return []
    ids_list = list(composer_ids)
    rows: list = []
    for offset in range(0, len(ids_list), _COMPOSER_FETCH_CHUNK):
        chunk = ids_list[offset : offset + _COMPOSER_FETCH_CHUNK]
        placeholders = ",".join("?" * len(chunk))
        keys = [f"composerData:{cid}" for cid in chunk]
        try:
            rows.extend(
                global_db.execute(
                    f"SELECT key, value FROM cursorDiskKV WHERE key IN ({placeholders})",
                    keys,
                ).fetchall()
            )
        except sqlite3.Error:
            continue
    return rows


def composer_ids_for_matching_workspaces(
    composer_id_to_workspace_id: dict[str, str],
    matching_workspace_ids: set[str],
) -> set[str]:
    return {
        cid for cid, ws in composer_id_to_workspace_id.items()
        if ws in matching_workspace_ids
    }


def load_composer_rows_for_project_list(
    global_db,
    composer_id_to_workspace_id: dict[str, str],
) -> tuple[list, set[str]]:
    """Load global composer rows for the project list (mapped + unmapped)."""
    mapped_ids = set(composer_id_to_workspace_id.keys())
    mapped_rows = fetch_composer_rows_by_ids(global_db, mapped_ids)
    all_keys = fetch_composer_data_keys(global_db)
    unmapped_ids = {cid for cid in all_keys if cid not in mapped_ids}
    unmapped_rows = fetch_composer_rows_by_ids(global_db, unmapped_ids)
    return mapped_rows + unmapped_rows, unmapped_ids


def load_composer_rows_for_workspace_summary(
    global_db,
    composer_id_to_workspace_id: dict[str, str],
    matching_workspace_ids: set[str],
    invalid_workspace_ids: set[str] | None,
    *,
    registry: WorkspaceComposerRegistry | None = None,
) -> tuple[list, list, set[str]]:
    """Load composer rows for ``?summary=1`` with workspace-scoped I/O.

    Returns ``(rows_to_process, alias_source_rows, unmapped_composer_ids)``.

    *alias_source_rows* supplies ``infer_invalid_workspace_aliases`` when needed.
    When *registry* is supplied, per-workspace views read composer ids from local
    workspace DBs instead of scanning the full ownership map path.
    """
    invalid = invalid_workspace_ids or set()
    mapped_ids = set(composer_id_to_workspace_id.keys())
    is_global_view = matching_workspace_ids == {"global"}

    if registry is not None and not invalid and not is_global_view:
        target_ids = registry.composer_ids_for_workspaces(matching_workspace_ids)
        rows = fetch_composer_rows_by_ids(global_db, target_ids)
        return rows, [], set()

    if not invalid and not is_global_view:
        target_ids = composer_ids_for_matching_workspaces(
            composer_id_to_workspace_id, matching_workspace_ids,
        )
        rows = fetch_composer_rows_by_ids(global_db, target_ids)
        return rows, [], set()

    if not invalid and is_global_view:
        all_keys = fetch_composer_data_keys(global_db)
        unmapped_ids = {cid for cid in all_keys if cid not in mapped_ids}
        rows = fetch_composer_rows_by_ids(global_db, unmapped_ids)
        return rows, [], unmapped_ids

    invalid_mapped_ids = {
        cid for cid, ws in composer_id_to_workspace_id.items() if ws in invalid
    }
    alias_rows = fetch_composer_rows_by_ids(global_db, invalid_mapped_ids)

    if is_global_view:
        all_keys = fetch_composer_data_keys(global_db)
        unmapped_ids = {cid for cid in all_keys if cid not in mapped_ids}
        rows = fetch_composer_rows_by_ids(global_db, unmapped_ids | invalid_mapped_ids)
        return rows, alias_rows, unmapped_ids

    target_ids = composer_ids_for_matching_workspaces(
        composer_id_to_workspace_id, matching_workspace_ids,
    )
    all_keys = fetch_composer_data_keys(global_db)
    unmapped_ids = {cid for cid in all_keys if cid not in mapped_ids}
    rows = fetch_composer_rows_by_ids(
        global_db,
        target_ids | unmapped_ids | invalid_mapped_ids,
    )
    return rows, alias_rows, unmapped_ids


def assigned_workspace_from_mapping(
    composer_id: str,
    composer_id_to_workspace_id: dict[str, str],
    invalid_workspace_ids: set[str] | None,
    invalid_workspace_aliases: dict[str, str] | None,
) -> str | None:
    """Return workspace when definitive mapping applies; ``None`` if heuristics needed."""
    invalid = invalid_workspace_ids or set()
    aliases = invalid_workspace_aliases or {}
    mapped = composer_id_to_workspace_id.get(composer_id)
    if not mapped:
        return None
    if mapped in invalid:
        return aliases.get(mapped)
    return mapped


def global_storage_db_path(workspace_path: str) -> str:
    """Resolved path to Cursor global ``state.vscdb`` for a workspace storage root."""
    return os.path.normpath(os.path.join(workspace_path, "..", "globalStorage", "state.vscdb"))


def _read_workspace_composer_entry(
    workspace_path: str,
    entry: dict,
) -> tuple[str, list[dict[str, Any]], dict[str, str]]:
    """Read ``composer.composerData`` from one workspace folder."""
    ws_id = entry["name"]
    composers: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    db_path = os.path.join(workspace_path, ws_id, "state.vscdb")
    if not os.path.isfile(db_path):
        return ws_id, composers, mapping
    db_uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(db_uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE [key] = 'composer.composerData'"
            ).fetchone()
    except sqlite3.Error:
        return ws_id, composers, mapping
    if not (row and row[0]):
        return ws_id, composers, mapping
    try:
        data = json.loads(row[0])
    except (json.JSONDecodeError, ValueError):
        return ws_id, composers, mapping
    all_composers = data.get("allComposers") if isinstance(data, dict) else None
    if not isinstance(all_composers, list):
        return ws_id, composers, mapping
    for composer in all_composers:
        if not isinstance(composer, dict):
            continue
        cid = composer.get("composerId")
        if not cid:
            continue
        cid_str = str(cid)
        if cid_str in _NON_CONVERSATION_COMPOSER_IDS:
            continue
        mapping[cid_str] = ws_id
        composers.append(composer)
    return ws_id, composers, mapping


def build_workspace_composer_registry(
    workspace_path: str,
    workspace_entries: list,
) -> WorkspaceComposerRegistry:
    """Build composer ownership from local ``state.vscdb`` files (parallel)."""
    composers_by_workspace: dict[str, list[dict[str, Any]]] = {}
    composer_id_to_ws: dict[str, str] = {}
    if not workspace_entries:
        return WorkspaceComposerRegistry(composer_id_to_ws, composers_by_workspace)

    workers = min(_LOCAL_DB_READ_WORKERS, max(1, len(workspace_entries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_read_workspace_composer_entry, workspace_path, entry)
            for entry in workspace_entries
        ]
        for future in as_completed(futures):
            ws_id, composers, mapping = future.result()
            if composers:
                composers_by_workspace[ws_id] = composers
            composer_id_to_ws.update(mapping)
    return WorkspaceComposerRegistry(composer_id_to_ws, composers_by_workspace)


def build_composer_id_to_workspace_id(workspace_path: str, workspace_entries: list) -> dict:
    """Build mapping from composer ID to workspace folder name."""
    return build_workspace_composer_registry(
        workspace_path, workspace_entries,
    ).composer_id_to_ws


def _fingerprint_cache_key(fingerprint: dict[str, Any]) -> str:
    payload = json.dumps(fingerprint, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_workspace_composer_registry_cached(
    workspace_path: str,
    workspace_entries: list,
    rules: list,
    *,
    nocache: bool = False,
) -> WorkspaceComposerRegistry:
    """Local composer registry with in-process and disk cache."""
    global _registry_memory_cache
    from services.summary_cache import (
        fingerprint_composer_map,
        get_cached_composer_registry,
        nocache_enabled,
        set_cached_composer_registry,
    )

    fingerprint = fingerprint_composer_map(workspace_path, workspace_entries, rules)
    cache_key = _fingerprint_cache_key(fingerprint)
    if not nocache_enabled(request_nocache=nocache):
        if _registry_memory_cache is not None and _registry_memory_cache[0] == cache_key:
            return _registry_memory_cache[1]
        cached = get_cached_composer_registry(fingerprint)
        if cached is not None:
            _registry_memory_cache = (cache_key, cached)
            return cached

    registry = build_workspace_composer_registry(workspace_path, workspace_entries)
    if not nocache_enabled(request_nocache=nocache):
        set_cached_composer_registry(fingerprint, registry)
        _registry_memory_cache = (cache_key, registry)
    return registry


def build_composer_id_to_workspace_id_cached(
    workspace_path: str,
    workspace_entries: list,
    rules: list,
    *,
    nocache: bool = False,
) -> dict:
    """Like :func:`build_composer_id_to_workspace_id` with optional disk cache."""
    return get_workspace_composer_registry_cached(
        workspace_path, workspace_entries, rules, nocache=nocache,
    ).composer_id_to_ws


@contextmanager
def open_global_db(workspace_path: str):
    """Open Cursor global storage SQLite database read-only.

    Args:
        workspace_path: Cursor workspace storage root.

    Yields:
        ``(conn, path)`` where ``conn`` is a :class:`sqlite3.Connection` with
        ``row_factory=sqlite3.Row``, or ``None`` if the database file is missing
        or cannot be opened. ``path`` is always the resolved global DB path.
    """
    global_db_path = global_storage_db_path(workspace_path)
    if not os.path.isfile(global_db_path):
        yield None, global_db_path
        return
    db_uri = Path(global_db_path).resolve().as_uri() + "?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
    except sqlite3.Error:
        yield None, global_db_path
        return
    conn.row_factory = sqlite3.Row
    try:
        yield conn, global_db_path
    finally:
        conn.close()
