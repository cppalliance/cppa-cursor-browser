from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class CliSessionMeta:
    """CLI session meta blob; latestRootBlobId is the conversation entry point and the only required field."""

    latest_root_blob_id: str
    created_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CliSessionMeta":
        if not isinstance(raw, dict):
            raise SchemaError(
                "CliSessionMeta",
                "meta",
                hint=f"expected object, got {type(raw).__name__}",
            )
        latest = raw.get("latestRootBlobId")
        if not latest:
            raise SchemaError("CliSessionMeta", "latestRootBlobId")
        if not isinstance(latest, str):
            raise SchemaError(
                "CliSessionMeta",
                "latestRootBlobId",
                hint=f"expected str, got {type(latest).__name__}",
            )
        return cls(
            latest_root_blob_id=latest,
            created_at=raw.get("createdAt"),
            raw=raw,
        )
