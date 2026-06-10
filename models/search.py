"""Typed shapes for search API results and composer summary metadata."""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class ConversationSummary(TypedDict, total=False):
    """Cursor ``conversationSummary`` blob on composer rows (schema varies by version)."""

    summary: str
    title: str
    bullets: list[str]
    raw: dict[str, Any]


class SearchResult(TypedDict):
    """One hit returned by ``/api/search`` and the search service helpers."""

    workspaceId: str
    workspaceFolder: str | None
    chatId: str
    chatTitle: str
    timestamp: int | str
    matchingText: str
    type: str  # "composer" | "chat" | "cli_agent"
    source: NotRequired[str]  # "cli" for CLI agent sessions
