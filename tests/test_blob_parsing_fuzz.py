"""Property-based fuzz tests for blob / bubble parsing (issue #71).

Run:
  python -m unittest tests.test_blob_parsing_fuzz -v
  python -m pytest tests/test_blob_parsing_fuzz.py -v
"""

from __future__ import annotations

import json
import os
import sys
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import Bubble, SchemaError
from utils.cli_chat_reader import _extract_blob_refs, messages_to_bubbles
from utils.text_extract import extract_text_from_bubble

# Bounded strategies: fast enough for CI (<30s total with default example counts).
_JSON_VALUES = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=200),
    st.lists(st.text(max_size=80), max_size=8),
)

_BUBBLE_RAW = st.dictionaries(
    st.text(min_size=0, max_size=40),
    _JSON_VALUES,
    max_size=12,
)

_BUBBLE_ID = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=80,
)

@st.composite
def _cli_message(draw) -> dict:
    role = draw(st.sampled_from(["user", "assistant", "system", "tool", ""]))
    content = draw(
        st.one_of(
            st.text(max_size=500),
            st.lists(
                st.dictionaries(
                    st.sampled_from(
                        ["type", "text", "toolName", "args", "toolCallId", "result"]
                    ),
                    st.one_of(st.text(max_size=120), st.integers(), st.none()),
                    max_size=6,
                ),
                max_size=8,
            ),
            st.none(),
        )
    )
    return {"role": role, "content": content}

_BUBBLE_LIKE = st.dictionaries(
    st.sampled_from(["text", "richText", "codeBlocks", "type", "metadata"]),
    st.one_of(
        st.text(max_size=300),
        st.none(),
        st.lists(
            st.dictionaries(
                st.text(max_size=20),
                st.one_of(st.text(max_size=100), st.integers()),
                max_size=5,
            ),
            max_size=4,
        ),
        st.dictionaries(st.text(max_size=20), _JSON_VALUES, max_size=5),
    ),
    max_size=6,
)


def _classify_blob_bytes(data: bytes) -> None:
    """Mirror traverse_blobs blob classification without SQLite."""
    try:
        msg = json.loads(data.decode("utf-8"))
        if isinstance(msg, dict) and "role" in msg:
            return
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        pass
    _extract_blob_refs(data)


class TestBubbleFromDictFuzz(unittest.TestCase):
    @given(raw=_BUBBLE_RAW, bubble_id=_BUBBLE_ID)
    @settings(max_examples=80, deadline=None)
    def test_never_raises_unhandled(self, raw: dict, bubble_id: str) -> None:
        try:
            bubble = Bubble.from_dict(raw, bubble_id=bubble_id)
        except SchemaError:
            return
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        self.assertEqual(bubble.bubble_id, bubble_id)
        self.assertIs(bubble.raw, raw)

    @given(raw=_BUBBLE_RAW, bubble_id=_BUBBLE_ID)
    @settings(max_examples=80, deadline=None)
    def test_parsing_is_idempotent(self, raw: dict, bubble_id: str) -> None:
        try:
            first = Bubble.from_dict(raw, bubble_id=bubble_id)
            second = Bubble.from_dict(raw, bubble_id=bubble_id)
        except SchemaError:
            return
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        self.assertEqual(first, second)


class TestBlobChainParsingFuzz(unittest.TestCase):
    @given(data=st.binary(max_size=4096))
    @settings(max_examples=120, deadline=None)
    def test_extract_blob_refs_never_raises(self, data: bytes) -> None:
        try:
            refs = _extract_blob_refs(data)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        self.assertIsInstance(refs, list)
        for ref in refs:
            self.assertIsInstance(ref, str)
            self.assertEqual(len(ref), 64)

    @given(data=st.binary(max_size=4096))
    @settings(max_examples=80, deadline=None)
    def test_extract_blob_refs_is_idempotent(self, data: bytes) -> None:
        self.assertEqual(_extract_blob_refs(data), _extract_blob_refs(data))

    @given(data=st.binary(max_size=4096))
    @settings(max_examples=80, deadline=None)
    def test_blob_classification_never_raises(self, data: bytes) -> None:
        try:
            _classify_blob_bytes(data)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")


class TestTextExtractionFuzz(unittest.TestCase):
    @given(bubble=_BUBBLE_LIKE)
    @settings(max_examples=100, deadline=None)
    def test_extract_text_from_bubble_never_raises(self, bubble: dict) -> None:
        try:
            extract_text_from_bubble(bubble)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")

    @given(bubble=_BUBBLE_LIKE)
    @settings(max_examples=80, deadline=None)
    def test_extract_text_is_idempotent(self, bubble: dict) -> None:
        self.assertEqual(
            extract_text_from_bubble(bubble),
            extract_text_from_bubble(bubble),
        )

    @given(
        messages=st.lists(_cli_message(), max_size=12),
        created_at=st.integers(min_value=0, max_value=2_000_000_000_000),
    )
    @settings(max_examples=80, deadline=None)
    def test_messages_to_bubbles_then_extract_never_raises(
        self, messages: list[dict], created_at: int
    ) -> None:
        try:
            bubbles = messages_to_bubbles(messages, created_at)
        except Exception as exc:
            self.fail(f"messages_to_bubbles raised {type(exc).__name__}: {exc}")
        self.assertIsInstance(bubbles, list)
        for bubble in bubbles:
            try:
                extract_text_from_bubble(bubble)
            except Exception as exc:
                self.fail(f"extract_text_from_bubble raised {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    unittest.main()
