"""Build and read :class:`models.DisplayBubble` from storage :class:`models.Bubble`."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from models import Bubble, BubbleMetadata, BubbleRole, DisplayBubble
from utils.path_helpers import to_epoch_ms
from utils.text_extract import extract_text_from_bubble
from utils.tool_parser import parse_tool_call


def bubble_display_timestamp_ms(bubble: Bubble) -> int:
    """Epoch-ms timestamp for a storage bubble; falls back to now when absent."""
    raw_ts = bubble.bubble_timestamp_ms()
    if raw_ts is not None:
        return to_epoch_ms(raw_ts)
    return int(datetime.now().timestamp() * 1000)


def extract_thinking_text(
    bubble: Bubble,
) -> tuple[str | None, int | float | None]:
    """Return ``(thinking_text, thinking_duration_ms)`` from a storage bubble."""
    thinking_raw = bubble.thinking
    if not thinking_raw:
        return None, bubble.thinking_duration_ms
    if isinstance(thinking_raw, str):
        return thinking_raw, bubble.thinking_duration_ms
    if isinstance(thinking_raw, dict):
        return thinking_raw.get("text"), bubble.thinking_duration_ms
    return None, bubble.thinking_duration_ms


def build_storage_bubble_metadata(
    bubble: Bubble,
    role: BubbleRole,
) -> dict[str, Any] | None:
    """Metadata dict for tabs/export — tool calls, tokens, thinking, context."""
    model_info = bubble.model_info
    model_name = model_info.get("modelName")
    if model_name == "default":
        model_name = None

    ctx_window = bubble.context_window_status_at_creation
    ctx_pct: float | None = None
    if ctx_window:
        if ctx_window.get("percentageRemainingFloat") is not None:
            ctx_pct = ctx_window.get("percentageRemainingFloat")
        elif ctx_window.get("percentageRemaining") is not None:
            ctx_pct = ctx_window.get("percentageRemaining")

    meta: dict[str, Any] = {}
    if model_name:
        meta["modelName"] = model_name
    if ctx_pct is not None:
        meta["contextWindowPercent"] = ctx_pct

    if role == "ai":
        token_count = bubble.token_count or {}
        tool_results = bubble.tool_results
        tfd = bubble.tool_former_data
        if isinstance(tfd, dict):
            tool_call = parse_tool_call(tfd)
            if isinstance(tool_call, dict):
                meta["toolCalls"] = [tool_call]

        thinking, thinking_duration_ms = extract_thinking_text(bubble)
        if thinking:
            meta["thinking"] = thinking
        if thinking_duration_ms is not None:
            meta["thinkingDurationMs"] = thinking_duration_ms

        in_tok = token_count.get("inputTokens") or 0
        out_tok = token_count.get("outputTokens") or 0
        cached_tok = token_count.get("cachedTokens") or 0
        if in_tok > 0:
            meta["inputTokens"] = in_tok
        if out_tok > 0:
            meta["outputTokens"] = out_tok
        if cached_tok > 0:
            meta["cachedTokens"] = cached_tok
        tool_calls = meta.get("toolCalls")
        tr_count = (len(tool_calls) if tool_calls else 0) or (
            len(tool_results) if tool_results else 0
        )
        if tr_count > 0:
            meta["toolResultsCount"] = tr_count
        if tool_results:
            meta["toolResults"] = tool_results
    elif ctx_window:
        tokens_used = ctx_window.get("tokensUsed", 0)
        token_limit = ctx_window.get("tokenLimit", 0)
        if tokens_used > 0:
            meta["contextTokensUsed"] = tokens_used
        if token_limit > 0:
            meta["contextTokenLimit"] = token_limit

    return meta or None


def build_display_bubble_from_storage(
    bubble: Bubble,
    role: BubbleRole,
    *,
    display_text: str | None = None,
) -> DisplayBubble | None:
    """Render a storage bubble as a :class:`DisplayBubble` for UI or export."""
    text = display_text if display_text is not None else extract_text_from_bubble(bubble)
    tfd = bubble.tool_former_data
    thinking, _ = extract_thinking_text(bubble)
    has_tool = tfd is not None
    has_thinking = bool(thinking)
    if not text.strip() and not has_tool and not has_thinking:
        return None

    if not text.strip() and has_tool and tfd is not None:
        text = f"**Tool: {tfd.get('name', 'unknown')}**"

    entry: DisplayBubble = {
        "type": role,
        "text": text.strip() or text,
        "timestamp": bubble_display_timestamp_ms(bubble),
    }
    metadata = build_storage_bubble_metadata(bubble, role)
    if metadata:
        entry["metadata"] = cast(BubbleMetadata, metadata)
    return entry


def display_bubble_metadata(bubble: DisplayBubble) -> BubbleMetadata:
    return bubble.get("metadata") or {}


def display_bubble_tool_calls(bubble: DisplayBubble) -> list[dict[str, Any]]:
    return list(display_bubble_metadata(bubble).get("toolCalls") or [])


def annotate_response_times(bubbles: list[DisplayBubble]) -> None:
    """Set ``metadata.responseTimeMs`` on AI bubbles following a user message."""
    last_user_ts: int | None = None
    for bub in bubbles:
        if bub["type"] == "user":
            last_user_ts = bub.get("timestamp")
            continue
        if bub["type"] != "ai" or last_user_ts is None:
            continue
        bts = bub.get("timestamp")
        if bts and bts > last_user_ts:
            meta = dict(display_bubble_metadata(bub))
            meta["responseTimeMs"] = bts - last_user_ts
            bub["metadata"] = cast(BubbleMetadata, meta)
