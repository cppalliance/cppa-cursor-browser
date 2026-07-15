"""Tests for typed raw accessors and schema-drift logging."""

from __future__ import annotations

import logging
import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models.conversation import Bubble, Composer
from utils.display_bubble import bubble_display_timestamp_ms
from models.raw_access import (
    bubble_attached_file_uris,
    bubble_relevant_files,
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

    def test_bubble_relevant_files_skips_non_str_elements(self) -> None:
        bubble = Bubble.from_dict(
            {"relevantFiles": ["/good.py", 123, None, "/also.py"]},
            bubble_id="b-rel",
        )
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            self.assertEqual(bubble.relevant_files, ["/good.py", "/also.py"])
        self.assertTrue(any("relevantFiles" in m for m in logs.output), logs.output)

    def test_bubble_attached_file_uris_skips_non_dict_elements(self) -> None:
        bubble = Bubble.from_dict(
            {
                "attachedFileCodeChunksUris": [
                    {"path": "/a.py"},
                    "bad",
                    {"path": "/b.py"},
                ]
            },
            bubble_id="b-uri",
        )
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            uris = bubble.attached_file_code_chunks_uris
        self.assertEqual(uris, [{"path": "/a.py"}, {"path": "/b.py"}])
        self.assertTrue(
            any("attachedFileCodeChunksUris" in m for m in logs.output),
            logs.output,
        )

    def test_project_layouts_silent_when_key_missing(self) -> None:
        with self.assertNoLogs("models.raw_access", level="WARNING"):
            layouts = message_request_context_project_layouts({}, composer_id="cmp-1")
        self.assertIsNone(layouts)

    def test_project_layouts_warns_on_wrong_type(self) -> None:
        with self.assertLogs("models.raw_access", level="WARNING") as logs:
            layouts = message_request_context_project_layouts(
                {"projectLayouts": "not-a-list"},
                composer_id="cmp-1",
            )
        self.assertIsNone(layouts)
        self.assertTrue(any("projectLayouts" in m for m in logs.output), logs.output)

    def test_conversation_header_bubble_id_silent_when_missing(self) -> None:
        with self.assertNoLogs("models.raw_access", level="WARNING"):
            bid = conversation_header_bubble_id({"type": 1}, composer_id="cmp-1")
        self.assertIsNone(bid)

    def test_conversation_header_bubble_id_warns_on_wrong_type(self) -> None:
        with self.assertLogs("models.raw_access", level="WARNING") as logs:
            bid = conversation_header_bubble_id(
                {"bubbleId": 123, "type": 1},
                composer_id="cmp-1",
            )
        self.assertIsNone(bid)
        self.assertTrue(any("bubbleId" in m for m in logs.output), logs.output)

    def test_bubble_timestamp_ms_prefers_created_at_and_handles_zero(self) -> None:
        b = Bubble.from_dict(
            {"createdAt": 0, "timestamp": 99},
            bubble_id="b-ts",
        )
        self.assertEqual(b.bubble_timestamp_ms(), 0)

        no_ts = Bubble.from_dict({"text": "hi"}, bubble_id="b-none")
        self.assertIsNone(no_ts.bubble_timestamp_ms())

        bad_bool = Bubble.from_dict({"createdAt": True}, bubble_id="b-bool")
        with self.assertNoLogs("models.conversation", level="WARNING"):
            self.assertIsNone(bad_bool.bubble_timestamp_ms())

    def test_bubble_thinking_silent_when_missing(self) -> None:
        bubble = Bubble.from_dict({"text": "hi"}, bubble_id="b-1")
        with self.assertNoLogs("models.conversation", level="WARNING"):
            self.assertIsNone(bubble.thinking)

    def test_bubble_thinking_warns_on_wrong_type(self) -> None:
        bubble = Bubble.from_dict({"thinking": 42}, bubble_id="b-bad")
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            self.assertIsNone(bubble.thinking)
        self.assertTrue(any("thinking" in m for m in logs.output), logs.output)

    def test_bubble_thinking_accepts_str_or_dict(self) -> None:
        s = Bubble.from_dict({"thinking": "trace"}, bubble_id="b-str")
        self.assertEqual(s.thinking, "trace")
        d = Bubble.from_dict({"thinking": {"text": "nested"}}, bubble_id="b-dict")
        self.assertEqual(d.thinking, {"text": "nested"})

    def test_bubble_entry_timestamp_ms_preserves_epoch_zero(self) -> None:
        """Regression: falsy ``or now()`` must not replace a valid ``createdAt`` of 0."""
        bubble = Bubble.from_dict({"createdAt": 0}, bubble_id="b-zero")
        sentinel_now_ms = 9_999_000_000_000
        with patch(
            "utils.display_bubble.datetime",
        ) as mock_datetime:
            mock_datetime.now.return_value.timestamp.return_value = sentinel_now_ms / 1000
            ts = bubble_display_timestamp_ms(bubble)
        self.assertEqual(ts, 0)
        self.assertNotEqual(ts, sentinel_now_ms)

    def test_bubble_entry_timestamp_ms_falls_back_when_no_timestamp(self) -> None:
        bubble = Bubble.from_dict({"text": "hi"}, bubble_id="b-none")
        sentinel_now_ms = 1_700_000_000_000
        with patch(
            "utils.display_bubble.datetime",
        ) as mock_datetime:
            mock_datetime.now.return_value.timestamp.return_value = sentinel_now_ms / 1000
            ts = bubble_display_timestamp_ms(bubble)
        self.assertEqual(ts, sentinel_now_ms)

    def test_dict_bridge_newly_created_files_matches_composer_property(self) -> None:
        data = {**GOOD_COMPOSER_RAW, "newlyCreatedFiles": [{"uri": {"path": "/a"}}]}
        composer = Composer.from_dict(data, composer_id="cid-bridge")
        self.assertEqual(
            composer.newly_created_files,
            composer_newly_created_files(composer, "cid-bridge"),
        )

    def test_dict_bridge_relevant_files_skips_non_str_elements(self) -> None:
        raw = {"relevantFiles": ["/good.py", 123, "/also.py"]}
        bubble = Bubble.from_dict(raw, bubble_id="b-bridge")
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            self.assertEqual(
                bubble_relevant_files(raw, "b-bridge"),
                ["/good.py", "/also.py"],
            )
        self.assertEqual(bubble.relevant_files, bubble_relevant_files(bubble, "b-bridge"))
        self.assertTrue(any("relevantFiles" in m for m in logs.output), logs.output)

    def test_dict_bridge_attached_file_uris_skips_non_dict_elements(self) -> None:
        raw = {
            "attachedFileCodeChunksUris": [
                {"path": "/a.py"},
                "bad",
                {"path": "/b.py"},
            ]
        }
        bubble = Bubble.from_dict(raw, bubble_id="b-bridge-uri")
        with self.assertLogs("models.conversation", level="WARNING") as logs:
            self.assertEqual(
                bubble_attached_file_uris(raw, "b-bridge-uri"),
                [{"path": "/a.py"}, {"path": "/b.py"}],
            )
        self.assertEqual(
            bubble.attached_file_code_chunks_uris,
            bubble_attached_file_uris(bubble, "b-bridge-uri"),
        )
        self.assertTrue(
            any("attachedFileCodeChunksUris" in m for m in logs.output),
            logs.output,
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
