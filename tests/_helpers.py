"""Shared test helpers that must not live in conftest.

pytest treats conftest specially and it is not guaranteed to be importable as
``tests.conftest`` under non-default import modes (e.g. ``--import-mode=importlib``).
"""
from __future__ import annotations

from flask.testing import FlaskClient

from app import create_app
from utils.exclusion_rules import tokenize_rule


def client_with_rules(rule_lines: list[str]) -> FlaskClient:
    """Flask test client with EXCLUSION_RULES parsed from the given lines.

    Requires WORKSPACE_PATH / CLI_CHATS_PATH to already be set (e.g. by
    ``workspace_storage`` fixture).
    """
    parsed = [tokenize_rule(line) for line in rule_lines]
    app = create_app()
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = [r for r in parsed if r]
    return app.test_client()
