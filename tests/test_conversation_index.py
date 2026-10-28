"""Tests for Cursor 3.x composerConvHeaders and colon-bearing bubble ids."""

import json
import sqlite3
import unittest

from services.workspace_db import load_bubbles_for_composer
from utils.conversation_index import (
    bubble_id_from_kv_key,
    composer_data_with_headers,
    load_conv_header_map,
)


class TestBubbleIdFromKvKey(unittest.TestCase):
    def test_plain_uuid(self) -> None:
        key = "bubbleId:composer-1:11111111-1111-1111-1111-111111111111"
        self.assertEqual(
            bubble_id_from_kv_key(key),
            "11111111-1111-1111-1111-111111111111",
        )

    def test_sub_bubble_keeps_colon(self) -> None:
        bubble_id = "11111111-1111-1111-1111-111111111111/s:0"
        key = f"bubbleId:composer-1:{bubble_id}"
        self.assertEqual(bubble_id_from_kv_key(key), bubble_id)

    def test_tool_bubble_keeps_newline(self) -> None:
        bubble_id = "11111111-1111-1111-1111-111111111111/t:call-1\nfc_abc"
        key = f"bubbleId:composer-1:{bubble_id}"
        self.assertEqual(bubble_id_from_kv_key(key), bubble_id)

    def test_missing_bubble_id(self) -> None:
        self.assertIsNone(bubble_id_from_kv_key("bubbleId:composer-1"))
        self.assertIsNone(bubble_id_from_kv_key("composerData:composer-1"))


class TestConvHeaderMap(unittest.TestCase):
    def test_chunks_merge_in_suffix_order(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        composer_id = "cmp-new"
        second = [{"bubbleId": "parent/s:0", "type": 2}]
        first = [{"bubbleId": "parent", "type": 1}]
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (f"composerConvHeaders:{composer_id}:000001", json.dumps(second)),
        )
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (f"composerConvHeaders:{composer_id}:000000", json.dumps(first)),
        )
        conn.commit()

        merged = load_conv_header_map(conn)
        self.assertEqual(
            [item["bubbleId"] for item in merged[composer_id]],
            ["parent", "parent/s:0"],
        )

    def test_inline_headers_win(self) -> None:
        inline = [{"bubbleId": "old", "type": 1}]
        resolved = composer_data_with_headers(
            {"fullConversationHeadersOnly": inline},
            "cmp-1",
            {"cmp-1": [{"bubbleId": "chunk", "type": 2}]},
        )
        self.assertEqual(resolved["fullConversationHeadersOnly"], inline)

    def test_empty_inline_uses_chunks(self) -> None:
        chunks = [{"bubbleId": "parent/s:0", "type": 2}]
        original = {"fullConversationHeadersOnly": [], "name": "Chat"}
        resolved = composer_data_with_headers(original, "cmp-1", {"cmp-1": chunks})
        self.assertEqual(resolved["fullConversationHeadersOnly"], chunks)
        self.assertEqual(original["fullConversationHeadersOnly"], [])

    def test_load_bubbles_keeps_sub_bubble_id(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE cursorDiskKV ([key] TEXT PRIMARY KEY, value TEXT)")
        composer_id = "cmp-new"
        bubble_id = "11111111-1111-1111-1111-111111111111/s:0"
        conn.execute(
            "INSERT INTO cursorDiskKV ([key], value) VALUES (?, ?)",
            (
                f"bubbleId:{composer_id}:{bubble_id}",
                json.dumps({"text": "assistant turn"}),
            ),
        )
        conn.commit()
        bubbles = load_bubbles_for_composer(conn, composer_id)
        self.assertIn(bubble_id, bubbles)
        self.assertEqual(bubbles[bubble_id].text, "assistant turn")


if __name__ == "__main__":
    unittest.main()
