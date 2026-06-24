from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from models.from_dict_validation import require_dict, require_non_empty_str, require_optional_str


@dataclass(frozen=True)
class Workspace:
    """A Cursor workspace folder; folder is None for CLI-only workspaces (not a schema error)."""

    workspace_id: str
    folder: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any], *, workspace_id: str) -> "Workspace":
        """Parse ``workspace.json`` into a validated workspace descriptor.

        Args:
            raw: Decoded workspace.json object.
            workspace_id: Workspace storage folder name.

        Returns:
            Validated :class:`Workspace` (``folder`` may be ``None`` for CLI-only).

        Raises:
            SchemaError: When required fields are missing or malformed.
        """
        raw = require_dict(raw, model="Workspace", field="workspace.json")
        require_non_empty_str(workspace_id, model="Workspace", field="workspaceId")
        folder = require_optional_str(raw.get("folder"), model="Workspace", field="folder")
        return cls(workspace_id=workspace_id, folder=folder, raw=raw)
