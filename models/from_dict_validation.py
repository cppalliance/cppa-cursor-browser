from __future__ import annotations

from typing import Any

from models.errors import SchemaError


def require_dict(raw: Any, *, model: str, field: str) -> dict[str, Any]:
    """Raise SchemaError when raw is not a dict; return raw for chaining."""
    if not isinstance(raw, dict):
        raise SchemaError(
            model,
            field,
            hint=f"expected object, got {type(raw).__name__}",
        )
    return raw


def require_key(raw: dict[str, Any], key: str, *, model: str) -> None:
    """Raise SchemaError when a required key is absent."""
    if key not in raw:
        raise SchemaError(model, key)


def require_non_empty_str(value: Any, *, model: str, field: str) -> str:
    """Validate a caller-supplied id (workspace_id, composer_id, bubble_id)."""
    if not isinstance(value, str) or not value:
        raise SchemaError(
            model,
            field,
            hint=f"expected non-empty str, got {type(value).__name__}",
        )
    return value


def require_non_empty_str_field(raw: dict[str, Any], key: str, *, model: str) -> str:
    """Validate a non-empty string field read from raw."""
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise SchemaError(
            model,
            key,
            hint=f"expected non-empty str, got {type(value).__name__}",
        )
    return value


def require_non_empty_str_fields(
    raw: dict[str, Any],
    keys: tuple[str, ...],
    *,
    model: str,
) -> None:
    """Validate multiple non-empty string fields in raw (ExportEntry pattern)."""
    for key in keys:
        value = raw.get(key)
        if not isinstance(value, str) or value == "":
            raise SchemaError(
                model,
                key,
                hint=f"expected non-empty str, got {type(value).__name__}",
            )


def require_truthy(value: Any, *, model: str, field: str) -> Any:
    """Raise missing-field SchemaError when value is falsy (absent or empty)."""
    if not value:
        raise SchemaError(model, field)
    return value


def require_type(
    value: Any,
    expected: type[Any] | tuple[type[Any], ...],
    *,
    model: str,
    field: str,
    hint: str | None = None,
) -> Any:
    if not isinstance(value, expected):
        raise SchemaError(
            model,
            field,
            hint=hint or f"expected {expected!r}, got {type(value).__name__}",
        )
    return value


def require_optional_str(value: Any, *, model: str, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise SchemaError(
            model,
            field,
            hint=f"expected str or None, got {type(value).__name__}",
        )
    return value
