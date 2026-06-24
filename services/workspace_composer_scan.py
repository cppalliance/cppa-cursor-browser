"""Shared composerData scan helpers for list and summary paths (issue #95).

Keeps conversation filtering, exclusion rules, and placeholder handling
aligned between ``list_workspace_projects`` and ``list_workspace_tab_summaries``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from models import Bubble, Composer, ParseWarningCollector, SchemaError
from utils.exclusion_rules import build_searchable_text, is_excluded_by_rules
from services.workspace_resolver import determine_project_for_conversation

_logger = logging.getLogger(__name__)


def parse_composer_data_row(
    row_key: str,
    raw_value: object | None,
    *,
    parse_warnings: ParseWarningCollector,
) -> Composer | None:
    """Parse a ``composerData:*`` KV row into a :class:`Composer`.

    Returns ``None`` for null/placeholder payloads (e.g. ``empty-state-draft``)
    without emitting decode warnings. Logs and records skips for malformed JSON.
    """
    if not row_key.startswith("composerData:"):
        return None
    if raw_value is None:
        return None
    if not isinstance(raw_value, (str, bytes, bytearray)):
        parse_warnings.record_composer_skipped()
        return None
    composer_id = row_key.split(":", 1)[1]
    if not composer_id:
        parse_warnings.record_composer_skipped()
        return None
    try:
        cd = json.loads(raw_value)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        _logger.warning(
            "Failed to decode Composer from composerData:%s: %s",
            composer_id,
            e,
        )
        parse_warnings.record_composer_skipped()
        return None
    if not isinstance(cd, dict):
        parse_warnings.record_composer_skipped()
        return None
    try:
        composer = Composer.from_dict(cd, composer_id=composer_id)
    except SchemaError as e:
        _logger.warning(
            "Failed to parse Composer from composerData:%s: %s",
            composer_id,
            e,
        )
        parse_warnings.record_composer_skipped()
        return None
    if not composer.full_conversation_headers_only:
        return None
    return composer


def composer_model_names(composer: Composer) -> list[str] | None:
    """Model names used for exclusion-rule matching (summary/list parity)."""
    model_name = composer.model_name_from_config()
    if model_name and model_name != "default":
        return [model_name]
    return None


def composer_chat_title(composer: Composer) -> str:
    return composer.name or f"Conversation {composer.composer_id[:8]}"


def is_composer_excluded(
    rules: list[Any],
    *,
    project_name: str,
    composer: Composer,
) -> bool:
    """Return ``True`` when *composer* matches an exclusion rule."""
    return is_excluded_by_rules(
        rules,
        build_searchable_text(
            project_name=project_name,
            chat_title=composer_chat_title(composer),
            model_names=composer_model_names(composer),
        ),
    )


def assign_composer_workspace(
    composer: Composer,
    *,
    project_layouts_map: dict[str, list[str]],
    project_name_map: dict[str, str],
    workspace_path_map: dict[str, str],
    workspace_entries: list[dict[str, Any]],
    bubble_map: Mapping[str, Bubble],
    composer_id_to_ws: dict[str, str],
    invalid_workspace_ids: set[str],
    invalid_workspace_aliases: dict[str, str],
) -> str:
    """Resolve owning workspace folder id, or ``\"global\"`` when unassigned."""
    composer_id = composer.composer_id
    pid = determine_project_for_conversation(
        composer,
        composer_id,
        project_layouts_map,
        project_name_map,
        workspace_path_map,
        workspace_entries,
        bubble_map,
        composer_id_to_ws,
        invalid_workspace_ids,
    )
    mapped_ws = composer_id_to_ws.get(composer_id)
    if not pid and mapped_ws in invalid_workspace_ids:
        pid = invalid_workspace_aliases.get(mapped_ws)
    return pid if pid else "global"
