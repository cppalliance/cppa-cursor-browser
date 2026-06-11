"""Rendered bubble shapes for UI, CLI, and markdown export.

Storage/KV rows (``bubbleId:*`` in ``cursorDiskKV``) are validated as
:class:`models.conversation.Bubble` at load time via
:func:`services.workspace_db.load_bubble_map`.  All user-facing paths emit
:class:`DisplayBubble` with optional :class:`BubbleMetadata`.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

BubbleRole = Literal["user", "ai"]


class BubbleMetadata(TypedDict, total=False):
    """Nested fields on a :class:`DisplayBubble` (tabs, CLI, export)."""

    modelName: str
    inputTokens: int
    outputTokens: int
    cachedTokens: int
    toolResultsCount: int
    toolResults: list[Any]
    toolCalls: list[dict[str, Any]]
    thinking: str
    thinkingDurationMs: int | float
    contextWindowPercent: float
    contextTokensUsed: int
    contextTokenLimit: int
    contextPctRemaining: float
    responseTimeMs: int
    cost: float


class _DisplayBubbleRequired(TypedDict):
    type: BubbleRole
    text: str
    timestamp: int


class _DisplayBubbleOptional(TypedDict, total=False):
    metadata: BubbleMetadata


class DisplayBubble(_DisplayBubbleRequired, _DisplayBubbleOptional):
    """One message bubble in the browser UI or an exported Markdown document."""
