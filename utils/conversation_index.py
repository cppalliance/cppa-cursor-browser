"""Conversation index helpers for Cursor 3.x global storage.

Cursor still writes ``composerData.fullConversationHeadersOnly``, but recent
sessions leave that list empty and store the ordered header entries in
``composerConvHeaders:{composerId}:{chunk}``. Bubble ids in those headers
may contain colons (``{uuid}/s:0``, ``{uuid}/t:call-…``).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Mapping
from typing import Any

_logger = logging.getLogger(__name__)

_BUBBLE_KEY_PREFIX = "bubbleId:"
_CONV_HEADER_KEY_PREFIX = "composerConvHeaders:"


def bubble_id_from_kv_key(key: str) -> str | None:
    """Return the bubble id from a ``bubbleId:{composerId}:{bubbleId}`` key.

    The bubble id is everything after the composer id, including colons and
    newlines. ``str.split(":")`` drops that tail.
    """
    if not key.startswith(_BUBBLE_KEY_PREFIX):
        return None
    _composer_id, sep, bubble_id = key[len(_BUBBLE_KEY_PREFIX):].partition(":")
    if not sep or not bubble_id:
        return None
    return bubble_id


def _conv_header_key_parts(key: str) -> tuple[str, str] | None:
    """Return ``(composer_id, chunk_suffix)`` for a conv-header key."""
    if not key.startswith(_CONV_HEADER_KEY_PREFIX):
        return None
    composer_id, sep, chunk = key[len(_CONV_HEADER_KEY_PREFIX):].partition(":")
    if not sep or not composer_id or not chunk:
        return None
    return composer_id, chunk


def _json_object(value: object) -> object | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed: object = json.loads(value)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed


def load_conv_header_map(
    conn: sqlite3.Connection,
) -> dict[str, list[dict[str, Any]]]:
    """Load ``composerConvHeaders:*`` chunks into ``{composer_id: headers}``.

    Chunks for one composer are concatenated in suffix order (``000000``,
    then ``000001``, …). Malformed rows are skipped.
    """
    grouped: dict[str, list[tuple[str, list[dict[str, Any]]]]] = {}
    try:
        rows = conn.execute(
            "SELECT key, value FROM cursorDiskKV"
            " WHERE key LIKE 'composerConvHeaders:%'"
        ).fetchall()
    except sqlite3.Error as exc:
        _logger.debug("Could not read composerConvHeaders: %s", exc)
        return {}

    for row in rows:
        key = row["key"]
        parts = _conv_header_key_parts(key if isinstance(key, str) else "")
        if parts is None:
            continue
        composer_id, chunk = parts
        parsed = _json_object(row["value"])
        if not isinstance(parsed, list):
            _logger.debug("Skipping non-list composerConvHeaders row %s", key)
            continue
        headers = [item for item in parsed if isinstance(item, dict)]
        grouped.setdefault(composer_id, []).append((chunk, headers))

    merged: dict[str, list[dict[str, Any]]] = {}
    for composer_id, chunks in grouped.items():
        chunks.sort(key=lambda pair: pair[0])
        combined: list[dict[str, Any]] = []
        for _chunk, headers in chunks:
            combined.extend(headers)
        if combined:
            merged[composer_id] = combined
    return merged


def composer_data_with_headers(
    composer_data: dict[str, Any],
    composer_id: str,
    conv_header_map: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Return composer JSON whose header list is non-empty when chunks exist.

    A non-empty ``fullConversationHeadersOnly`` is left unchanged. An empty
    or missing list is replaced with the chunk map for *composer_id* when
    that map has entries. The input dict is not mutated.
    """
    inline = composer_data.get("fullConversationHeadersOnly")
    if isinstance(inline, list) and inline:
        return composer_data
    resolved = conv_header_map.get(composer_id) or []
    if not resolved:
        return composer_data
    copied = dict(composer_data)
    copied["fullConversationHeadersOnly"] = list(resolved)
    return copied
