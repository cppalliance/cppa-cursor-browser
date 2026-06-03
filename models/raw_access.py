"""Optional-key reads from Cursor JSON blobs with schema-drift logging."""

from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def warn_missing_raw_key(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> None:
    """Log when a frequently-used optional field is absent (likely key rename)."""
    suffix = f" {entity_id}" if entity_id else ""
    _logger.warning(
        "Schema drift in %s%s: missing optional field %s",
        model,
        suffix,
        key,
    )


def optional_raw_value(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
    expected_type: type[Any] | tuple[type[Any], ...] | None = None,
) -> Any | None:
    """Return ``raw[key]`` when present and typed; log drift and return ``None`` otherwise."""
    if key not in raw:
        warn_missing_raw_key(raw, key, model=model, entity_id=entity_id)
        return None
    value = raw[key]
    if expected_type is not None and not isinstance(value, expected_type):
        suffix = f" {entity_id}" if entity_id else ""
        _logger.warning(
            "Schema drift in %s%s: invalid type for %s (expected %s, got %s)",
            model,
            suffix,
            key,
            expected_type,
            type(value).__name__,
        )
        return None
    return value


def optional_raw_list(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> list[Any] | None:
    return optional_raw_value(
        raw,
        key,
        model=model,
        entity_id=entity_id,
        expected_type=list,
    )


def optional_raw_dict(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> dict[str, Any] | None:
    return optional_raw_value(
        raw,
        key,
        model=model,
        entity_id=entity_id,
        expected_type=dict,
    )


def optional_raw_str(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> str | None:
    return optional_raw_value(
        raw,
        key,
        model=model,
        entity_id=entity_id,
        expected_type=str,
    )


def optional_raw_number(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
    default: int | float = 0,
) -> int | float:
    """Numeric composer counters; warn on missing key, return *default* when absent."""
    if key not in raw:
        warn_missing_raw_key(raw, key, model=model, entity_id=entity_id)
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        suffix = f" {entity_id}" if entity_id else ""
        _logger.warning(
            "Schema drift in %s%s: invalid type for %s (expected number, got %s)",
            model,
            suffix,
            key,
            type(value).__name__,
        )
        return default
    return value


def conversation_header_bubble_id(
    header: dict[str, Any],
    *,
    composer_id: str = "",
) -> str | None:
    """``bubbleId`` from a ``fullConversationHeadersOnly`` entry."""
    value = optional_raw_str(
        header,
        "bubbleId",
        model="ConversationHeader",
        entity_id=composer_id,
    )
    return value if value else None


def message_request_context_project_layouts(
    ctx: dict[str, Any],
    *,
    composer_id: str = "",
) -> list[Any] | None:
    """``projectLayouts`` from a messageRequestContext blob."""
    return optional_raw_list(
        ctx,
        "projectLayouts",
        model="MessageRequestContext",
        entity_id=composer_id,
    )


def composer_headers(
    data: Any,
    composer_id: str,
) -> list[dict[str, Any]]:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.full_conversation_headers_only
    headers = optional_raw_list(
        data,
        "fullConversationHeadersOnly",
        model="Composer",
        entity_id=composer_id,
    )
    return headers if headers is not None else []


def composer_newly_created_files(data: Any, composer_id: str) -> list[Any]:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.newly_created_files
    value = data.get("newlyCreatedFiles") if isinstance(data, dict) else None
    if value is None:
        return []
    if not isinstance(value, list):
        _logger.warning(
            "Schema drift in Composer %s: invalid type for newlyCreatedFiles (expected list, got %s)",
            composer_id,
            type(value).__name__,
        )
        return []
    return value


def composer_code_block_data(data: Any, composer_id: str) -> dict[str, Any] | None:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.code_block_data
    return optional_raw_dict(
        data, "codeBlockData", model="Composer", entity_id=composer_id
    )


def bubble_relevant_files(bubble: Any, bubble_id: str = "") -> list[Any]:
    from models.conversation import Bubble

    if isinstance(bubble, Bubble):
        return bubble.relevant_files
    if isinstance(bubble, dict):
        value = bubble.get("relevantFiles")
        if value is None:
            return []
        if isinstance(value, list):
            return value
    return []


def bubble_attached_file_uris(bubble: Any, bubble_id: str = "") -> list[Any]:
    from models.conversation import Bubble

    if isinstance(bubble, Bubble):
        return bubble.attached_file_code_chunks_uris
    if isinstance(bubble, dict):
        value = bubble.get("attachedFileCodeChunksUris")
        if value is None:
            return []
        if isinstance(value, list):
            return value
    return []


def bubble_context(bubble: Any, bubble_id: str = "") -> dict[str, Any]:
    from models.conversation import Bubble

    if isinstance(bubble, Bubble):
        return bubble.context or {}
    if isinstance(bubble, dict):
        ctx = bubble.get("context")
        if ctx is None:
            return {}
        if isinstance(ctx, dict):
            return ctx
    return {}
