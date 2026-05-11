"""CliSessionMeta — typed model for the Cursor CLI ``meta`` blob."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class CliSessionMeta:
    """The ``meta`` blob at the head of a Cursor CLI ``store.db`` blob graph.

    ``latestRootBlobId`` is the entry point for the conversation reconstruction
    BFS in ``utils/cli_chat_reader.traverse_blobs``; without it, the entire
    conversation is unreachable. ``createdAt`` is documented as part of the
    meta-blob schema (see ``utils/cli_chat_reader`` module docstring) and is
    captured here, but it is not gated on — only ``latestRootBlobId`` is the
    hard requirement, since that is the only field whose absence prevents
    conversation reconstruction.
    """

    latest_root_blob_id: str
    created_at: Any = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CliSessionMeta":
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
