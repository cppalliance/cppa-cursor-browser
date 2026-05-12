from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


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
        if not isinstance(raw, dict):
            raise SchemaError(
                "Composer",
                "composerData",
                hint=f"expected object, got {type(raw).__name__}",
            )
        if not isinstance(composer_id, str) or not composer_id:
            raise SchemaError(
                "Composer",
                "composerId",
                hint=f"expected non-empty str, got {type(composer_id).__name__}",
            )
        if "fullConversationHeadersOnly" not in raw:
            raise SchemaError("Composer", "fullConversationHeadersOnly")
        if "createdAt" not in raw:
            raise SchemaError("Composer", "createdAt")

        created_at = raw.get("createdAt")
        if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
            raise SchemaError(
                "Composer",
                "createdAt",
                hint=f"expected timestamp number, got {type(created_at).__name__}",
            )

        headers = raw.get("fullConversationHeadersOnly")
        if not isinstance(headers, list):
            raise SchemaError(
                "Composer",
                "fullConversationHeadersOnly",
                hint=f"expected list, got {type(headers).__name__}",
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
        if not isinstance(raw, dict):
            raise SchemaError(
                "WorkspaceLocalComposer",
                "composer",
                hint=f"expected object, got {type(raw).__name__}",
            )
        composer_id = raw.get("composerId")
        if not isinstance(composer_id, str) or not composer_id:
            raise SchemaError(
                "WorkspaceLocalComposer",
                "composerId",
                hint=f"expected non-empty str, got {type(composer_id).__name__}",
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
        if not isinstance(raw, dict):
            raise SchemaError(
                "Bubble",
                "bubble",
                hint=f"expected object, got {type(raw).__name__}",
            )
        if not isinstance(bubble_id, str) or not bubble_id:
            raise SchemaError(
                "Bubble",
                "bubbleId",
                hint=f"expected non-empty str, got {type(bubble_id).__name__}",
            )
        return cls(bubble_id=bubble_id, raw=raw)
