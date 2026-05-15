from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class Workspace:
    """A Cursor workspace folder; folder is None for CLI-only workspaces (not a schema error)."""

    workspace_id: str
    folder: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, workspace_id: str) -> "Workspace":
        if not isinstance(raw, dict):
            raise SchemaError(
                "Workspace",
                "workspace.json",
                hint=f"expected object, got {type(raw).__name__}",
            )
        if not isinstance(workspace_id, str) or not workspace_id:
            raise SchemaError(
                "Workspace",
                "workspaceId",
                hint=f"expected non-empty str, got {type(workspace_id).__name__}",
            )
        folder = raw.get("folder")
        if folder is not None and not isinstance(folder, str):
            raise SchemaError(
                "Workspace",
                "folder",
                hint=f"expected str or None, got {type(folder).__name__}",
            )
        return cls(workspace_id=workspace_id, folder=folder, raw=raw)
