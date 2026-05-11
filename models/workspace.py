"""Workspace — typed model for a single Cursor workspace folder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.errors import SchemaError


@dataclass(frozen=True)
class Workspace:
    """A Cursor workspace entry.

    The workspace ID is the directory name on disk (Cursor uses random
    short hashes as workspace IDs) and is passed in explicitly. ``folder``
    is the absolute path of the project the workspace targets, read from
    ``workspace.json``; it may legitimately be ``None`` for a CLI-only
    workspace, so missing-folder is not a schema error.
    """

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
        if not workspace_id:
            raise SchemaError("Workspace", "workspaceId", hint="empty workspace ID")
        folder = raw.get("folder")
        if folder is not None and not isinstance(folder, str):
            raise SchemaError(
                "Workspace",
                "folder",
                hint=f"expected str or None, got {type(folder).__name__}",
            )
        return cls(workspace_id=workspace_id, folder=folder, raw=raw)
