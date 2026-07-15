"""Optional-key reads from Cursor JSON blobs with schema-drift logging."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from models.conversation import Bubble, Composer
    from models.conversation_types import BubbleContextDict, FileUriDict

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
    warn_if_missing: bool = True,
) -> Any | None:
    """Return ``raw[key]`` when present and typed; log drift and return ``None`` otherwise."""
    if key not in raw:
        if warn_if_missing:
            warn_missing_raw_key(raw, key, model=model, entity_id=entity_id)
        return None
    value = raw[key]
    if expected_type is not None:
        if isinstance(value, bool) and expected_type in ((int, float), int, float):
            suffix = f" {entity_id}" if entity_id else ""
            _logger.warning(
                "Schema drift in %s%s: invalid type for %s (expected number, got bool)",
                model,
                suffix,
                key,
            )
            return None
        if not isinstance(value, expected_type):
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
    warn_if_missing: bool = True,
) -> list[Any] | None:
    return optional_raw_value(
        raw,
        key,
        model=model,
        entity_id=entity_id,
        expected_type=list,
        warn_if_missing=warn_if_missing,
    )


def optional_raw_dict(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
    warn_if_missing: bool = True,
) -> dict[str, Any] | None:
    return optional_raw_value(
        raw,
        key,
        model=model,
        entity_id=entity_id,
        expected_type=dict,
        warn_if_missing=warn_if_missing,
    )


def _optional_list_absent_ok(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> list[Any]:
    """List field often omitted by Cursor; warn only on wrong type."""
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        suffix = f" {entity_id}" if entity_id else ""
        _logger.warning(
            "Schema drift in %s%s: invalid type for %s (expected list, got %s)",
            model,
            suffix,
            key,
            type(value).__name__,
        )
        return []
    return value


def _optional_dict_absent_ok(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> dict[str, Any] | None:
    """Dict field often omitted; warn only on wrong type; ``None`` when absent."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        suffix = f" {entity_id}" if entity_id else ""
        _logger.warning(
            "Schema drift in %s%s: invalid type for %s (expected dict, got %s)",
            model,
            suffix,
            key,
            type(value).__name__,
        )
        return None
    return value


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


def _optional_str_absent_ok(
    raw: dict[str, Any],
    key: str,
    *,
    model: str,
    entity_id: str = "",
) -> str | None:
    """String field often omitted on headers; warn only on wrong type."""
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        if key in raw:
            suffix = f" {entity_id}" if entity_id else ""
            _logger.warning(
                "Schema drift in %s%s: invalid type for %s (expected non-empty str, got %s)",
                model,
                suffix,
                key,
                type(value).__name__,
            )
        return None
    return value


def conversation_header_bubble_id(
    header: dict[str, Any],
    *,
    composer_id: str = "",
) -> str | None:
    """``bubbleId`` from a ``fullConversationHeadersOnly`` entry."""
    return _optional_str_absent_ok(
        header,
        "bubbleId",
        model="ConversationHeader",
        entity_id=composer_id,
    )


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
        warn_if_missing=False,
    )


def composer_headers(
    data: Composer | dict[str, Any],
    composer_id: str,
) -> list[dict[str, Any]]:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.full_conversation_headers_only
    return _optional_list_absent_ok(
        data,
        "fullConversationHeadersOnly",
        model="Composer",
        entity_id=composer_id,
    )


def composer_newly_created_files(
    data: Composer | dict[str, Any], composer_id: str
) -> list[Any]:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.newly_created_files
    return _optional_list_absent_ok(
        data,
        "newlyCreatedFiles",
        model="Composer",
        entity_id=composer_id,
    )


def composer_code_block_data(
    data: Composer | dict[str, Any], composer_id: str
) -> dict[str, Any] | None:
    from models.conversation import Composer

    if isinstance(data, Composer):
        return data.code_block_data
    return _optional_dict_absent_ok(
        data,
        "codeBlockData",
        model="Composer",
        entity_id=composer_id,
    )


def bubble_relevant_files(
    bubble: Bubble | dict[str, Any], bubble_id: str = ""
) -> list[str]:
    from models.conversation import Bubble, _filter_str_list_elements

    if isinstance(bubble, Bubble):
        return bubble.relevant_files
    return _filter_str_list_elements(
        _optional_list_absent_ok(
            bubble,
            "relevantFiles",
            model="Bubble",
            entity_id=bubble_id,
        ),
        model="Bubble",
        record_id=bubble_id,
        field="relevantFiles",
    )


def bubble_attached_file_uris(
    bubble: Bubble | dict[str, Any], bubble_id: str = ""
) -> list[FileUriDict]:
    from models.conversation import Bubble, _filter_dict_list_elements

    if isinstance(bubble, Bubble):
        return bubble.attached_file_code_chunks_uris
    return _filter_dict_list_elements(
        _optional_list_absent_ok(
            bubble,
            "attachedFileCodeChunksUris",
            model="Bubble",
            entity_id=bubble_id,
        ),
        model="Bubble",
        record_id=bubble_id,
        field="attachedFileCodeChunksUris",
    )


def bubble_context(bubble: Bubble | dict[str, Any], bubble_id: str = "") -> BubbleContextDict:
    from models.conversation import Bubble

    if isinstance(bubble, Bubble):
        return bubble.context
    ctx = _optional_dict_absent_ok(
        bubble,
        "context",
        model="Bubble",
        entity_id=bubble_id,
    )
    return cast(BubbleContextDict, ctx if ctx is not None else {})
