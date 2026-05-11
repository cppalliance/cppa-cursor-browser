"""Composer (conversation) and Bubble (message) typed models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class Composer:
    """A Cursor conversation (a.k.a. "composer") row.

    Required fields per the schema-validation contract (issue #24):
      - ``fullConversationHeadersOnly`` — without this, a composer cannot be
        rendered (no message order is recoverable).
      - ``createdAt`` — Cursor writes this on every composer (verified
        17/17 against a live workspaceStorage). A missing value is the
        kind of drift this layer exists to surface.

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
        if not isinstance(raw, dict):
            raise SchemaError(
                "Composer",
                "composerData",
                hint=f"expected object, got {type(raw).__name__}",
            )
        if not composer_id:
            raise SchemaError("Composer", "composerId", hint="empty composer ID")
        if "fullConversationHeadersOnly" not in raw:
            raise SchemaError("Composer", "fullConversationHeadersOnly")
        if "createdAt" not in raw:
            raise SchemaError("Composer", "createdAt")

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
            created_at=raw.get("createdAt"),
            name=raw.get("name"),
            last_updated_at=raw.get("lastUpdatedAt"),
            model_config=model_config,
            raw=raw,
        )


@dataclass(frozen=True)
class WorkspaceLocalComposer:
    """A composer entry from ``composer.composerData`` ItemTable rows.

    These are summary records that live in each per-workspace ``state.vscdb``.
    They share ``composerId`` and ``lastUpdatedAt`` with the global composer
    schema, but they do **not** carry ``fullConversationHeadersOnly`` or
    ``createdAt`` — those only exist on the global ``cursorDiskKV`` rows that
    ``Composer.from_dict`` validates. Treating both shapes through the same
    model would reject every workspace-local entry, so this slim model
    exists to keep schema-drift detection at the boundary without conflating
    the two storage paths.
    """

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
        if not isinstance(raw, dict):
            raise SchemaError(
                "Bubble",
                "bubble",
                hint=f"expected object, got {type(raw).__name__}",
            )
        if not bubble_id:
            raise SchemaError("Bubble", "bubbleId", hint="empty bubble ID")
        return cls(bubble_id=bubble_id, raw=raw)
