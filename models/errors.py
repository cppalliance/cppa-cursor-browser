"""Exception types for the typed-model schema-validation layer."""

from __future__ import annotations


class SchemaError(ValueError):
    """Raised when a required Cursor schema field is missing or malformed.

    Inherits from ``ValueError`` so call sites that already catch generic
    deserialisation errors (e.g. ``json.JSONDecodeError`` is a subclass of
    ``ValueError``) also catch schema drift without needing a separate
    ``except`` clause. New code should catch ``SchemaError`` explicitly.
    """

    def __init__(self, model: str, field: str, *, hint: str | None = None) -> None:
        self.model = model
        self.field = field
        self.hint = hint
        message = f"{model}: missing required field '{field}'"
        if hint:
            message = f"{message} ({hint})"
        super().__init__(message)
