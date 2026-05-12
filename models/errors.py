from __future__ import annotations


class SchemaError(ValueError):
    """Raised when a required Cursor schema field is missing or malformed."""

    def __init__(self, model: str, field: str, *, hint: str | None = None) -> None:
        self.model = model
        self.field = field
        self.hint = hint
        message = f"{model}: missing required field '{field}'"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)
