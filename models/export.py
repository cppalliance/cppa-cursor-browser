from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TypedDict

from models.from_dict_validation import require_dict, require_non_empty_str_fields


class CollectedExportEntry(TypedDict):
    """One exportable conversation with rendered markdown (engine/CLI collection)."""

    id: str
    rel_path: str
    content: str
    out_path: str
    updatedAt: int
    title: str
    workspace: str


@dataclass(frozen=True)
class ExportEntry:
    """One line of manifest.jsonl; log_id / title / workspace required, timestamps optional."""

    log_id: str
    title: str
    workspace: str
    created_at: Any = None
    updated_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ExportEntry":
        raw = require_dict(raw, model="ExportEntry", field="entry")
        require_non_empty_str_fields(
            raw,
            ("log_id", "title", "workspace"),
            model="ExportEntry",
        )
        return cls(
            log_id=raw["log_id"],
            title=raw["title"],
            workspace=raw["workspace"],
            created_at=raw.get("created_at"),
            updated_at=raw.get("updated_at"),
            raw=raw,
        )
