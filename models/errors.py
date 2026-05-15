from __future__ import annotations


class SchemaError(ValueError):
    """Raised when a required Cursor schema field is missing or malformed."""

    def __init__(self, model: str, field: str, *, hint: str | None = None) -> None:
        self.model = model
        self.field = field
        self.hint = hint
        # Distinguish "absent" from "present-but-wrong-shape" so log grepping can
        # tell missing-key drift apart from type-mismatch drift. Hint is only
        # populated for shape mismatches (e.g. "expected list, got dict"), so its
        # presence is the signal.
        if hint:
            message = f"{model}: invalid field '{field}' ({hint})"
        else:
            message = f"{model}: missing required field '{field}'"
        super().__init__(message)
