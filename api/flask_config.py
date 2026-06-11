"""Shared Flask request/config helpers for API blueprints."""

from __future__ import annotations

from typing import Any, cast, overload

from flask import Response, current_app, jsonify


def exclusion_rules() -> list[list[Any]]:
    """Return loaded exclusion rules from app config (empty list when unset)."""
    return current_app.config.get("EXCLUSION_RULES") or []


@overload
def json_response(data: Any) -> Response: ...


@overload
def json_response(data: Any, status: int) -> tuple[Response, int]: ...


def json_response(
    data: Any,
    status: int | None = None,
) -> Response | tuple[Response, int]:
    """Typed wrapper around :func:`flask.jsonify` for strict mypy (types-Flask)."""
    response = cast(Response, jsonify(data))
    if status is None:
        return response
    return response, status
