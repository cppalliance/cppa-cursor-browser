from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError
from models.from_dict_validation import require_dict, require_truthy


@dataclass(frozen=True)
class CliSessionMeta:
    """CLI session meta blob; latestRootBlobId is the conversation entry point and the only required field."""

    latest_root_blob_id: str
    created_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CliSessionMeta":
        """Parse CLI session ``meta`` JSON into a validated descriptor.

        Args:
            raw: Decoded meta object from a CLI chat session.

        Returns:
            Validated :class:`CliSessionMeta`.

        Raises:
            SchemaError: When ``latestRootBlobId`` is missing or not a string.
        """
        raw = require_dict(raw, model="CliSessionMeta", field="meta")
        latest = require_truthy(
            raw.get("latestRootBlobId"),
            model="CliSessionMeta",
            field="latestRootBlobId",
        )
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
