from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, cast

from models.conversation_types import (
    BubbleContextDict,
    BubbleMetadataDict,
    ContextWindowStatusDict,
    FileUriDict,
    ModelInfoDict,
    ThinkingDict,
    TokenCountDict,
    ToolFormerDataDict,
    ToolResultEntry,
)
from models.errors import SchemaError
from models.from_dict_validation import (
    require_dict,
    require_key,
    require_non_empty_str,
    require_non_empty_str_field,
    require_type,
)
from models.raw_access import (
    _optional_dict_absent_ok,
    _optional_dict_default_empty,
    _optional_list_absent_none,
    _optional_list_absent_ok,
    _optional_number_absent_ok,
)

_logger = logging.getLogger(__name__)


def _filter_str_list_elements(
    value: list[Any],
    *,
    model: str,
    record_id: str,
    field: str,
) -> list[str]:
    filtered: list[str] = []
    for item in value:
        if isinstance(item, str):
            filtered.append(item)
        else:
            _logger.warning(
                "Schema drift in %s %s: invalid %s element (expected str, got %s)",
                model,
                record_id,
                field,
                type(item).__name__,
            )
    return filtered


def _filter_dict_list_elements(
    value: list[Any],
    *,
    model: str,
    record_id: str,
    field: str,
) -> list[FileUriDict]:
    filtered: list[FileUriDict] = []
    for item in value:
        if isinstance(item, dict):
            filtered.append(cast(FileUriDict, item))
        else:
            _logger.warning(
                "Schema drift in %s %s: invalid %s element (expected dict, got %s)",
                model,
                record_id,
                field,
                type(item).__name__,
            )
    return filtered


@dataclass(frozen=True)
class Composer:
    """Cursor conversation row from globalStorage cursorDiskKV; requires fullConversationHeadersOnly + createdAt."""

    composer_id: str
    full_conversation_headers_only: list[dict[str, Any]]
    created_at: Any
    name: str | None = None
    last_updated_at: Any = None
    model_config: dict[str, Any] = field(default_factory=dict)
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, composer_id: str) -> "Composer":
        """Parse a global ``composerData`` row into a validated composer.

        Args:
            raw: Decoded JSON object from cursorDiskKV.
            composer_id: Composer UUID from the storage key.

        Returns:
            Validated :class:`Composer` with required headers and timestamps.

        Raises:
            SchemaError: When required fields are missing or malformed.
        """
        raw = require_dict(raw, model="Composer", field="composerData")
        require_non_empty_str(composer_id, model="Composer", field="composerId")
        require_key(raw, "fullConversationHeadersOnly", model="Composer")
        require_key(raw, "createdAt", model="Composer")

        created_at = raw.get("createdAt")
        # Numeric-only on purpose: a 2026-05 scan of 17/17 live composers on
        # disk stored createdAt as int milliseconds. If Cursor ever switches
        # to ISO strings, those rows would disappear from list/search via a
        # drift warning — relax the check at that point, don't silently coerce.
        if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
            raise SchemaError(
                "Composer",
                "createdAt",
                hint=f"expected timestamp number, got {type(created_at).__name__}",
            )

        headers_value = raw.get("fullConversationHeadersOnly")
        headers = require_type(
            headers_value,
            list,
            model="Composer",
            field="fullConversationHeadersOnly",
            hint=f"expected list, got {type(headers_value).__name__}",
        )

        model_config = raw.get("modelConfig") or {}
        if not isinstance(model_config, dict):
            model_config = {}

        return cls(
            composer_id=composer_id,
            full_conversation_headers_only=headers,
            created_at=created_at,
            name=raw.get("name"),
            last_updated_at=raw.get("lastUpdatedAt"),
            model_config=model_config,
            _raw=raw,
        )

    def cursor_storage_payload(self) -> dict[str, Any]:
        """Shallow copy of stored Cursor JSON for API passthrough.

        Prefer typed accessors for field reads.
        """
        return dict(self._raw)

    @property
    def newly_created_files(self) -> list[Any]:
        return _optional_list_absent_ok(
            self._raw,
            "newlyCreatedFiles",
            model="Composer",
            entity_id=self.composer_id,
        )

    @property
    def code_block_data(self) -> dict[str, Any] | None:
        return _optional_dict_absent_ok(
            self._raw,
            "codeBlockData",
            model="Composer",
            entity_id=self.composer_id,
        )

    @property
    def usage_data(self) -> dict[str, Any]:
        """Composer cost rollup; empty dict when absent (common)."""
        return _optional_dict_default_empty(
            self._raw,
            "usageData",
            model="Composer",
            entity_id=self.composer_id,
        )

    def _optional_counter(self, key: str) -> int | float:
        return _optional_number_absent_ok(
            self._raw,
            key,
            model="Composer",
            entity_id=self.composer_id,
        )

    @property
    def total_lines_added(self) -> int | float:
        return self._optional_counter("totalLinesAdded")

    @property
    def total_lines_removed(self) -> int | float:
        return self._optional_counter("totalLinesRemoved")

    @property
    def added_files(self) -> int | float:
        return self._optional_counter("addedFiles")

    @property
    def removed_files(self) -> int | float:
        return self._optional_counter("removedFiles")

    def model_name_from_config(self) -> str | None:
        name = self.model_config.get("modelName")
        return name if isinstance(name, str) and name else None


# Issue #100: Cursor persists conversations as ``composerData`` rows; ``Composer``
# is the validated domain type for a full conversation.
Conversation = Composer


@dataclass(frozen=True)
class WorkspaceLocalComposer:
    """Summary composer row from per-workspace state.vscdb ItemTable; only composerId is required."""

    composer_id: str
    last_updated_at: Any = None
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkspaceLocalComposer":
        """Parse one ``allComposers`` entry from per-workspace state.

        Args:
            raw: Composer summary dict from ``composer.composerData``.

        Returns:
            Validated local composer row.

        Raises:
            SchemaError: When ``composerId`` is missing or invalid.
        """
        raw = require_dict(raw, model="WorkspaceLocalComposer", field="composer")
        composer_id = require_non_empty_str_field(
            raw, "composerId", model="WorkspaceLocalComposer"
        )
        return cls(
            composer_id=composer_id,
            last_updated_at=raw.get("lastUpdatedAt"),
            _raw=raw,
        )

    def cursor_storage_payload(self) -> dict[str, Any]:
        """Shallow copy of stored Cursor JSON for API passthrough."""
        return dict(self._raw)


@dataclass(frozen=True)
class Bubble:
    """One message in a composer; bubble_id comes from the row key, not the JSON value.

    Rendered for UI/export as :class:`models.bubble_display.DisplayBubble`.
    """

    bubble_id: str
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, bubble_id: str) -> "Bubble":
        """Parse one ``bubbleId:*`` KV value into a validated bubble.

        Args:
            raw: Decoded bubble JSON (``bubble_id`` comes from the key, not value).
            bubble_id: Bubble UUID from the storage key suffix.

        Returns:
            Validated :class:`Bubble`.

        Raises:
            SchemaError: When the payload or *bubble_id* is invalid.
        """
        raw = require_dict(raw, model="Bubble", field="bubble")
        require_non_empty_str(bubble_id, model="Bubble", field="bubbleId")
        return cls(bubble_id=bubble_id, _raw=raw)

    def cursor_storage_payload(self) -> dict[str, Any]:
        """Shallow copy of stored Cursor JSON for API passthrough.

        Prefer typed accessors for field reads.
        """
        return dict(self._raw)

    @property
    def text(self) -> str | None:
        """Plain ``text`` field; richText is handled by :func:`extract_text_from_bubble`."""
        value = self._raw.get("text")
        return value if isinstance(value, str) else None

    @property
    def metadata(self) -> BubbleMetadataDict:
        return cast(
            BubbleMetadataDict,
            _optional_dict_default_empty(
                self._raw,
                "metadata",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
        )

    @property
    def relevant_files(self) -> list[str]:
        return _filter_str_list_elements(
            _optional_list_absent_ok(
                self._raw,
                "relevantFiles",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
            model="Bubble",
            record_id=self.bubble_id,
            field="relevantFiles",
        )

    @property
    def attached_file_code_chunks_uris(self) -> list[FileUriDict]:
        return _filter_dict_list_elements(
            _optional_list_absent_ok(
                self._raw,
                "attachedFileCodeChunksUris",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
            model="Bubble",
            record_id=self.bubble_id,
            field="attachedFileCodeChunksUris",
        )

    @property
    def context(self) -> BubbleContextDict:
        return cast(
            BubbleContextDict,
            _optional_dict_default_empty(
                self._raw,
                "context",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
        )

    @property
    def token_count(self) -> TokenCountDict | None:
        value = _optional_dict_absent_ok(
            self._raw,
            "tokenCount",
            model="Bubble",
            entity_id=self.bubble_id,
        )
        return cast(TokenCountDict, value) if value is not None else None

    @property
    def tool_former_data(self) -> ToolFormerDataDict | None:
        value = _optional_dict_absent_ok(
            self._raw,
            "toolFormerData",
            model="Bubble",
            entity_id=self.bubble_id,
        )
        return cast(ToolFormerDataDict, value) if value is not None else None

    @property
    def model_info(self) -> ModelInfoDict:
        return cast(
            ModelInfoDict,
            _optional_dict_default_empty(
                self._raw,
                "modelInfo",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
        )

    @property
    def thinking(self) -> str | ThinkingDict | None:
        # Inline: accepts str | dict; no raw_access helper for that union.
        value = self._raw.get("thinking")
        if value is None:
            return None
        if isinstance(value, (str, dict)):
            return cast(str | ThinkingDict, value)
        _logger.warning(
            "Schema drift in Bubble %s: invalid type for thinking (expected str or dict, got %s)",
            self.bubble_id,
            type(value).__name__,
        )
        return None

    @property
    def thinking_duration_ms(self) -> int | float | None:
        # Inline: absent -> None (not 0); bool guard before numeric check.
        value = self._raw.get("thinkingDurationMs")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for thinkingDurationMs (expected number, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return None
        return cast(int | float, value)

    @property
    def context_window_status_at_creation(self) -> ContextWindowStatusDict:
        return cast(
            ContextWindowStatusDict,
            _optional_dict_default_empty(
                self._raw,
                "contextWindowStatusAtCreation",
                model="Bubble",
                entity_id=self.bubble_id,
            ),
        )

    @property
    def tool_results(self) -> list[ToolResultEntry] | None:
        value = _optional_list_absent_none(
            self._raw,
            "toolResults",
            model="Bubble",
            entity_id=self.bubble_id,
        )
        return cast(list[ToolResultEntry], value) if value is not None else None

    def bubble_timestamp_ms(self) -> int | float | None:
        """``createdAt`` or ``timestamp`` in milliseconds when present."""
        for key in ("createdAt", "timestamp"):
            value = self._raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None
