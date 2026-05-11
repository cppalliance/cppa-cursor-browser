"""Composer (conversation) and Bubble (message) typed models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class Composer:
    """A Cursor conversation (a.k.a. "composer") row.

    Required fields per the schema-validation contract:
      - ``fullConversationHeadersOnly`` — without this, a composer cannot be
        rendered (no message order is recoverable). This is the only hard
        requirement: real Cursor data legitimately omits ``createdAt`` for
        older composers (the existing call sites already fall back to
        ``lastUpdatedAt`` and then to epoch zero), so it is captured but
        not gated on.

    The composer ID is intentionally passed in as a constructor argument
    rather than read from ``raw`` because Cursor stores it in the row key
    (``composerData:<id>``) rather than in the JSON value.
    """

    composer_id: str
    full_conversation_headers_only: list[dict[str, Any]]
    created_at: Any
    name: str | None = None
    last_updated_at: Any = None
    model_config: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, composer_id: str) -> "Composer":
        if not composer_id:
            raise SchemaError("Composer", "composerId", hint="empty composer ID")
        if "fullConversationHeadersOnly" not in raw:
            raise SchemaError("Composer", "fullConversationHeadersOnly")

        headers = raw.get("fullConversationHeadersOnly") or []
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
            created_at=raw.get("createdAt"),
            name=raw.get("name"),
            last_updated_at=raw.get("lastUpdatedAt"),
            model_config=model_config,
            raw=raw,
        )


@dataclass(frozen=True)
class Bubble:
    """A single message bubble within a composer.

    The bubble ID lives in the row key (``bubbleId:<composer_id>:<bubble_id>``)
    rather than the JSON value, so it is passed in explicitly. The raw dict
    is preserved to keep downstream rendering code (which still walks the
    untyped shape) working without modification.
    """

    bubble_id: str
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, bubble_id: str) -> "Bubble":
        if not bubble_id:
            raise SchemaError("Bubble", "bubbleId", hint="empty bubble ID")
        return cls(bubble_id=bubble_id, raw=raw)
