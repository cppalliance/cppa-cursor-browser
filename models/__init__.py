"""Typed domain models for Cursor schema (closes #24).

Cursor's on-disk JSON shapes are not versioned, so silent renames of fields
like ``composerData`` or ``latestRootBlobId`` would otherwise pass through
``dict.get(...)`` with a fallback default and produce empty conversations
with no error raised. The models here add a schema-validation boundary at
database read sites: ``from_dict`` classmethods raise ``SchemaError`` when
critical fields are missing, so drift becomes loud instead of silent.
"""

from models.cli_session import CliSessionMeta
from models.conversation import Bubble, Composer, WorkspaceLocalComposer
from models.errors import SchemaError
from models.export import ExportEntry
from models.workspace import Workspace

__all__ = [
    "Bubble",
    "CliSessionMeta",
    "Composer",
    "ExportEntry",
    "SchemaError",
    "Workspace",
    "WorkspaceLocalComposer",
]
