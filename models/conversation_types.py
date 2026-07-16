"""Typed shapes for Cursor JSON fields on :class:`models.conversation.Bubble`."""

from __future__ import annotations

from typing import Any, TypedDict


class FileUriDict(TypedDict, total=False):
    """URI object on attached-file or context entries."""

    path: str


class BubbleMetadataDict(TypedDict, total=False):
    """Storage ``metadata`` blob on a bubble row (subset we read here).

    Rich display metadata (tool calls, token counts, thinking) is assembled in
    :func:`utils.display_bubble.build_storage_bubble_metadata` from typed
    bubble fields, not from this dict alone.
    """

    modelName: str


class TokenCountDict(TypedDict, total=False):
    inputTokens: int
    outputTokens: int
    cachedTokens: int


class ModelInfoDict(TypedDict, total=False):
    modelName: str


class ContextWindowStatusDict(TypedDict, total=False):
    percentageRemainingFloat: float
    percentageRemaining: float
    tokensUsed: int
    tokenLimit: int


class FileSelectionDict(TypedDict, total=False):
    uri: FileUriDict


class BubbleContextDict(TypedDict, total=False):
    fileSelections: list[FileSelectionDict]


class ToolFormerDataDict(TypedDict, total=False):
    name: str
    status: str
    params: str | dict[str, Any]
    rawArgs: str
    result: str


class ThinkingDict(TypedDict, total=False):
    text: str


class ToolResultEntry(TypedDict, total=False):
    """One element of a bubble ``toolResults`` list."""

    toolName: str
    result: str | dict[str, Any]
