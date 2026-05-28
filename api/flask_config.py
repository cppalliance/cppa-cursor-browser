"""Shared Flask request/config helpers for API blueprints."""

from __future__ import annotations

from flask import current_app


def exclusion_rules() -> list:
    """Return loaded exclusion rules from app config (empty list when unset)."""
    return current_app.config.get("EXCLUSION_RULES") or []
