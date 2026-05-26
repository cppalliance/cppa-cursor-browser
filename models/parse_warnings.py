from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ParseWarningCollector:
    """Accumulates parse failures skipped during bubble/composer processing."""

    composers_skipped: int = 0
    bubbles_skipped: int = 0
    composers_processing_failed: int = 0

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

    @property
    def has_warnings(self) -> bool:
        return (
            self.composers_skipped > 0
            or self.bubbles_skipped > 0
            or self.composers_processing_failed > 0
        )

    def to_api_list(self) -> list[dict]:
        """Structured warnings for JSON API responses (issue #67)."""
        warnings: list[dict] = []
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
        return warnings

    def attach_to(self, payload: dict) -> dict:
        """Add ``warnings`` to a dict response when any failures were recorded."""
        if self.has_warnings:
            payload = {**payload, "warnings": self.to_api_list()}
        return payload
