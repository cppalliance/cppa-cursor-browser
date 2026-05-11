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
        for required in ("log_id", "title", "workspace"):
            if required not in raw or raw[required] in (None, ""):
                raise SchemaError("ExportEntry", required)
        return cls(
            log_id=str(raw["log_id"]),
            title=str(raw["title"]),
            workspace=str(raw["workspace"]),
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            raw=raw,
        )
