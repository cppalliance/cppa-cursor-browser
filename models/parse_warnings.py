from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParseWarningCollector:
    """Accumulates parse failures skipped during bubble/composer processing."""

    composers_skipped: int = 0
    bubbles_skipped: int = 0
    composers_processing_failed: int = 0
    source_failures: list[dict] = field(default_factory=list)

    def record_composer_skipped(self, count: int = 1) -> None:
        if count > 0:
            self.composers_skipped += count

    def record_bubble_skipped(self, count: int = 1) -> None:
        if count > 0:
            self.bubbles_skipped += count

    def record_composer_processing_failure(self, count: int = 1) -> None:
        """Post-parse assembly failed; not a JSON/schema parse skip."""
        if count > 0:
            self.composers_processing_failed += count

    def record_source_failure(self, exc: BaseException, source: str) -> None:
        """Record a whole-source failure (e.g. the global storage DB is unreadable).

        Distinct from per-item parse skips: signals that an entire data source
        could not be searched so the API can warn callers that results may be
        incomplete.

        The raw exception is intentionally not stored — it is logged server-side
        by the caller (``_logger.exception``) before this method is invoked.
        Only the source identifier is retained so ``to_api_list`` can produce a
        safe client message without leaking file paths or Python internals.
        """
        self.source_failures.append({"source": source})

    @property
    def has_warnings(self) -> bool:
        return (
            self.composers_skipped > 0
            or self.bubbles_skipped > 0
            or self.composers_processing_failed > 0
            or bool(self.source_failures)
        )

    def to_api_list(self) -> list[dict[str, Any]]:
        """Structured warnings for JSON API responses (issue #67)."""
        warnings: list[dict[str, Any]] = []
        if self.composers_skipped:
            n = self.composers_skipped
            noun = "conversation" if n == 1 else "conversations"
            warnings.append({
                "type": "parse_error",
                "count": n,
                "detail": (
                    f"{n} {noun} could not be loaded due to schema or JSON parse errors"
                ),
            })
        if self.bubbles_skipped:
            n = self.bubbles_skipped
            noun = "message" if n == 1 else "messages"
            warnings.append({
                "type": "parse_error",
                "count": n,
                "detail": (
                    f"{n} {noun} could not be loaded due to schema or JSON parse errors"
                ),
            })
        if self.composers_processing_failed:
            n = self.composers_processing_failed
            noun = "conversation" if n == 1 else "conversations"
            warnings.append({
                "type": "processing_error",
                "count": n,
                "detail": (
                    f"{n} {noun} could not be fully assembled after parsing"
                ),
            })
        for sf in self.source_failures:
            warnings.append({
                "type": "source_failure",
                "source": sf["source"],
                "detail": f"Search source '{sf['source']}' could not be queried; results may be incomplete",
            })
        return warnings

    def attach_to(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add ``warnings`` to a dict response when any failures were recorded."""
        if self.has_warnings:
            payload = {**payload, "warnings": self.to_api_list()}
        return payload
