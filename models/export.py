"""ExportEntry — typed model for an export manifest record (JSONL line)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class ExportEntry:
    """A single record in the export manifest (one line in ``manifest.jsonl``).

    Required fields are the YAML-frontmatter keys that downstream tooling
    indexes against: a missing ``log_id`` makes the entry unaddressable, and
    a missing ``title`` produces unreadable output. Timestamps are optional —
    not every Cursor conversation has both a creation and update time.
    """

    log_id: str
    title: str
    workspace: str
    created_at: Any = None
    updated_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExportEntry":
        if not isinstance(raw, dict):
            raise SchemaError(
                "ExportEntry",
                "entry",
                hint=f"expected object, got {type(raw).__name__}",
            )
        for required in ("log_id", "title", "workspace"):
            value = raw.get(required)
            if not isinstance(value, str) or value == "":
                raise SchemaError(
                    "ExportEntry",
                    required,
                    hint=f"expected non-empty str, got {type(value).__name__}",
                )
        return cls(
            log_id=raw["log_id"],
            title=raw["title"],
            workspace=raw["workspace"],
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            raw=raw,
        )
