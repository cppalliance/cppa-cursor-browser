"""Shared composer/bubble/workspace IDs used by both the pytest fixture
(`tests/conftest.py`) and the tests that introspect the seeded data.

Lives in a regular module rather than inside conftest because conftest is
special to pytest and is not guaranteed to be importable as `tests.conftest`
under non-default import modes (e.g. `--import-mode=importlib`)."""
from __future__ import annotations

HAPPY_COMPOSER_ID = "cmp-happy"
HAPPY_BUBBLE_ID = "bub-happy"
HAPPY_WORKSPACE_ID = "ws-happy"
