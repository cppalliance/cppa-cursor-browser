from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError
from models.from_dict_validation import (
    require_dict,
    require_key,
    require_non_empty_str,
    require_non_empty_str_field,
    require_type,
)

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Composer:
    """Cursor conversation row from globalStorage cursorDiskKV; requires fullConversationHeadersOnly + createdAt."""

    composer_id: str
    full_conversation_headers_only: list[dict[str, Any]]
    created_at: Any
    name: str | None = None
    last_updated_at: Any = None
    model_config: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, composer_id: str) -> "Composer":
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
            raw=raw,
        )

    @property
    def newly_created_files(self) -> list[Any]:
        value = self.raw.get("newlyCreatedFiles")
        if value is None:
            return []
        if not isinstance(value, list):
            _logger.warning(
                "Schema drift in Composer %s: invalid type for newlyCreatedFiles (expected list, got %s)",
                self.composer_id,
                type(value).__name__,
            )
            return []
        return value

    @property
    def code_block_data(self) -> dict[str, Any] | None:
        value = self.raw.get("codeBlockData")
        if value is None:
            return None
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Composer %s: invalid type for codeBlockData (expected dict, got %s)",
                self.composer_id,
                type(value).__name__,
            )
            return None
        return value

    @property
    def usage_data(self) -> dict[str, Any]:
        """Composer cost rollup; empty dict when absent (common)."""
        value = self.raw.get("usageData")
        if value is None:
            return {}
        if not isinstance(value, dict):
            suffix = f" {self.composer_id}" if self.composer_id else ""
            _logger.warning(
                "Schema drift in Composer%s: invalid type for usageData (expected dict, got %s)",
                suffix,
                type(value).__name__,
            )
            return {}
        return value

    def _optional_counter(self, key: str) -> int | float:
        value = self.raw.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            if key in self.raw:
                suffix = f" {self.composer_id}" if self.composer_id else ""
                _logger.warning(
                    "Schema drift in Composer%s: invalid type for %s (expected number, got %s)",
                    suffix,
                    key,
                    type(value).__name__,
                )
            return 0
        return value

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


@dataclass(frozen=True)
class WorkspaceLocalComposer:
    """Summary composer row from per-workspace state.vscdb ItemTable; only composerId is required."""

    composer_id: str
    last_updated_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "WorkspaceLocalComposer":
        raw = require_dict(raw, model="WorkspaceLocalComposer", field="composer")
        composer_id = require_non_empty_str_field(
            raw, "composerId", model="WorkspaceLocalComposer"
        )
        return cls(
            composer_id=composer_id,
            last_updated_at=raw.get("lastUpdatedAt"),
            raw=raw,
        )


@dataclass(frozen=True)
class Bubble:
    """One message in a composer; bubble_id comes from the row key, not the JSON value."""

    bubble_id: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, bubble_id: str) -> "Bubble":
        raw = require_dict(raw, model="Bubble", field="bubble")
        require_non_empty_str(bubble_id, model="Bubble", field="bubbleId")
        return cls(bubble_id=bubble_id, raw=raw)

    @property
    def text(self) -> str | None:
        """Plain ``text`` field; richText is handled by :func:`extract_text_from_bubble`."""
        value = self.raw.get("text")
        return value if isinstance(value, str) else None

    @property
    def metadata(self) -> dict[str, Any]:
        value = self.raw.get("metadata")
        if value is None:
            return {}
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for metadata (expected dict, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return {}
        return value

    @property
    def relevant_files(self) -> list[Any]:
        value = self.raw.get("relevantFiles")
        if value is None:
            return []
        if not isinstance(value, list):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for relevantFiles (expected list, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return []
        return value

    @property
    def attached_file_code_chunks_uris(self) -> list[Any]:
        value = self.raw.get("attachedFileCodeChunksUris")
        if value is None:
            return []
        if not isinstance(value, list):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for attachedFileCodeChunksUris (expected list, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return []
        return value

    @property
    def context(self) -> dict[str, Any]:
        value = self.raw.get("context")
        if value is None:
            return {}
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for context (expected dict, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return {}
        return value

    @property
    def token_count(self) -> Any | None:
        return self.raw.get("tokenCount")

    @property
    def tool_former_data(self) -> dict[str, Any] | None:
        value = self.raw.get("toolFormerData")
        if value is None:
            return None
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for toolFormerData (expected dict, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return None
        return value

    @property
    def model_info(self) -> dict[str, Any]:
        value = self.raw.get("modelInfo")
        if value is None:
            return {}
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for modelInfo (expected dict, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return {}
        return value

    @property
    def thinking(self) -> Any | None:
        return self.raw.get("thinking")

    @property
    def thinking_duration_ms(self) -> Any | None:
        return self.raw.get("thinkingDurationMs")

    @property
    def context_window_status_at_creation(self) -> dict[str, Any]:
        value = self.raw.get("contextWindowStatusAtCreation")
        if value is None:
            return {}
        if not isinstance(value, dict):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for contextWindowStatusAtCreation (expected dict, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return {}
        return value

    @property
    def tool_results(self) -> list[Any] | None:
        value = self.raw.get("toolResults")
        if value is None:
            return None
        if not isinstance(value, list):
            _logger.warning(
                "Schema drift in Bubble %s: invalid type for toolResults (expected list, got %s)",
                self.bubble_id,
                type(value).__name__,
            )
            return None
        return value

    def bubble_timestamp_ms(self) -> int | float | None:
        """``createdAt`` or ``timestamp`` in milliseconds when present."""
        for key in ("createdAt", "timestamp"):
            value = self.raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None
