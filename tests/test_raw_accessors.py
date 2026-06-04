"""Tests for typed raw accessors and schema-drift logging."""

from __future__ import annotations

import logging
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models.conversation import Bubble, Composer
from models.raw_access import (
    composer_newly_created_files,
    conversation_header_bubble_id,
    message_request_context_project_layouts,
    optional_raw_list,
    warn_missing_raw_key,
)
from tests.test_models import GOOD_COMPOSER_RAW


class TestRawAccessorDriftLogging(unittest.TestCase):
    def test_composer_newly_created_files_empty_when_key_missing(self) -> None:
        raw = dict(GOOD_COMPOSER_RAW)
        raw.pop("newlyCreatedFiles", None)
        bare = Composer.from_dict(raw, composer_id="cid-2")
        with self.assertNoLogs("models.conversation", level="WARNING"):
            self.assertEqual(bare.newly_created_files, [])

    def test_composer_newly_created_files_warns_on_wrong_type(self) -> None:
        bad = Composer.from_dict(
            {**GOOD_COMPOSER_RAW, "newlyCreatedFiles": "not-a-list"},
            composer_id="cid-bad",
        )
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            self.assertEqual(bad.newly_created_files, [])
        self.assertTrue(any("newlyCreatedFiles" in m for m in logs.output), logs.output)

    def test_bubble_relevant_files_empty_when_key_missing(self) -> None:
        bubble = Bubble.from_dict({"type": "user", "text": "hi"}, bubble_id="b-1")
        with self.assertNoLogs("models.conversation", level="WARNING"):
            self.assertEqual(bubble.relevant_files, [])

    def test_project_layouts_warns_when_key_missing(self) -> None:
        with self.assertLogs("models.raw_access", level="WARNING") as logs:
            layouts = message_request_context_project_layouts({}, composer_id="cmp-1")
        self.assertIsNone(layouts)
        self.assertTrue(any("projectLayouts" in m for m in logs.output), logs.output)

    def test_conversation_header_bubble_id_warns_when_missing(self) -> None:
        with self.assertLogs("models.raw_access", level="WARNING") as logs:
            bid = conversation_header_bubble_id({"type": 1}, composer_id="cmp-1")
        self.assertIsNone(bid)
        self.assertTrue(any("bubbleId" in m for m in logs.output), logs.output)

    def test_dict_bridge_newly_created_files_matches_composer_property(self) -> None:
        data = {**GOOD_COMPOSER_RAW, "newlyCreatedFiles": [{"uri": {"path": "/a"}}]}
        composer = Composer.from_dict(data, composer_id="cid-bridge")
        self.assertEqual(
            composer.newly_created_files,
            composer_newly_created_files(composer, "cid-bridge"),
        )

    def test_optional_raw_list_no_warning_when_present(self) -> None:
        with self.assertNoLogs("models.raw_access", level="WARNING"):
            value = optional_raw_list(
                {"items": [1]},
                "items",
                model="Test",
                entity_id="e1",
            )
        self.assertEqual(value, [1])

    def test_warn_missing_raw_key_message_format(self) -> None:
        with self.assertLogs("models.raw_access", level="WARNING") as logs:
            warn_missing_raw_key(
                {},
                "sampleKey",
                model="SampleModel",
                entity_id="ent-9",
            )
        self.assertIn("SampleModel ent-9", logs.output[0])
        self.assertIn("sampleKey", logs.output[0])


if __name__ == "__main__":
    unittest.main()
