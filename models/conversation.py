from __future__ import annotations

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
