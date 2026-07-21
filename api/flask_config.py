"""Shared Flask request/config helpers for API blueprints."""

from __future__ import annotations

from typing import Any, cast, overload

from flask import Response, current_app, jsonify

from utils.exclusion_rules import RuleTokens


def exclusion_rules() -> list[RuleTokens]:
    """Return loaded exclusion rules from app config (empty list when unset)."""
    return cast(list[RuleTokens], current_app.config.get("EXCLUSION_RULES") or [])


@overload
def json_response(data: Any) -> Response: ...


@overload
def json_response(data: Any, status: int) -> tuple[Response, int]: ...


def json_response(
    data: Any,
    status: int | None = None,
) -> Response | tuple[Response, int]:
    """Typed wrapper around :func:`flask.jsonify` for strict mypy."""
    response = jsonify(data)
    assert isinstance(response, Response)
    if status is None:
        return response
    return response, status


def api_error(
    message: str,
    code: str,
    status: int,
) -> tuple[Response, int]:
    """Return a structured ``{"error", "code"}`` JSON body with *status*."""
    return json_response({"error": message, "code": code}, status)
