"""Regression tests for tab-level metadata aggregation edge cases."""

from __future__ import annotations

import unittest

from models import Composer
from services.workspace_tabs import _aggregate_tab_metadata


class TestAggregateTabMetadata(unittest.TestCase):
    def test_context_token_limit_only_produces_tab_metadata(self):
        """A positive contextTokenLimit alone must not be dropped by has_any."""
        bubbles = [{
            "type": "user",
            "text": "hello",
            "timestamp": 1,
            "metadata": {"contextTokenLimit": 128_000},
        }]
        composer = Composer(
            composer_id="composer-limit-only",
            full_conversation_headers_only=[],
            created_at=1,
        )

        tab_meta = _aggregate_tab_metadata(bubbles, composer)

        self.assertEqual(tab_meta, {"contextTokenLimit": 128_000})


if __name__ == "__main__":
    unittest.main()
