"""Property-based fuzz tests for blob / bubble parsing (issue #71).

Run:
  python -m unittest tests.test_blob_parsing_fuzz -v
  python -m pytest tests/test_blob_parsing_fuzz.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import Bubble, SchemaError
from utils.cli_chat_reader import (
    classify_blob_data,
    messages_to_bubbles,
    traverse_blobs,
    _extract_blob_refs,  # internal helper; covered directly alongside classify_blob_data
)
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

_BUBBLE_RAW_ANY = st.one_of(
    _BUBBLE_RAW,
    st.none(),
    st.integers(),
    st.lists(st.text(max_size=40), max_size=5),
    st.text(max_size=200),
)

_BUBBLE_ID = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"),
    min_size=1,
    max_size=80,
)

_BUBBLE_ID_ANY = st.one_of(
    _BUBBLE_ID,
    st.just(""),
    st.none(),
    st.integers(min_value=0, max_value=9999),
    st.binary(min_size=0, max_size=8),
)

_BLOB_ID_HEX = st.text(
    alphabet="abcdef0123456789",
    min_size=64,
    max_size=64,
)


@st.composite
def _cli_message(draw):
    # Empty role is intentional adversarial input (unknown / missing role).
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

_KV_VALUE = st.one_of(
    st.none(),
    _BUBBLE_RAW,
    st.text(max_size=400),
    st.binary(max_size=256),
    st.integers(),
)


def _make_meta_value(meta: dict) -> str:
    return json.dumps(meta).encode("utf-8").hex()


def _build_store_db_raw(path: str, meta: dict, blobs: dict[str, bytes]) -> None:
    """Minimal store.db with arbitrary blob payloads (for traverse_blobs fuzz)."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    conn.execute("INSERT INTO meta VALUES ('0', ?)", (_make_meta_value(meta),))
    for blob_id, data in blobs.items():
        conn.execute("INSERT INTO blobs VALUES (?, ?)", (blob_id, data))
    conn.commit()
    conn.close()


def _assemble_workspace_bubble(bubble_id: object, value: object) -> dict | None:
    """Mirror workspace_tabs KV bubble load (json.loads → Bubble.from_dict).

    Matches ``services/workspace_tabs.py`` (bubbleId loop): ``json.loads(row["value"])``
    with no type branching — same exceptions as production. Rows with ``value IS NULL``
    are not selected in production; ``None`` here returns ``None`` for fuzz only.

    Intentionally omits ``_loads_kv_value_logged`` (logging / payload hashing).
    """
    if value is None:
        return None
    try:
        parsed = json.loads(value)  # type: ignore[arg-type]
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    try:
        return Bubble.from_dict(parsed, bubble_id=bubble_id).raw  # type: ignore[arg-type]
    except SchemaError:
        return None


def _parse_bubble_from_dict(raw: object, bubble_id: object) -> Bubble | None:
    """Call Bubble.from_dict; return None on SchemaError, propagate nothing else."""
    try:
        return Bubble.from_dict(raw, bubble_id=bubble_id)  # type: ignore[arg-type]
    except SchemaError:
        return None


class TestBubbleFromDictFuzz(unittest.TestCase):
    @given(raw=_BUBBLE_RAW, bubble_id=_BUBBLE_ID)
    @settings(max_examples=80, deadline=None)
    def test_never_raises_unhandled(self, raw: dict, bubble_id: str) -> None:
        bubble = _parse_bubble_from_dict(raw, bubble_id)
        if bubble is None:
            return
        self.assertEqual(bubble.bubble_id, bubble_id)
        self.assertIs(bubble.raw, raw)

    @given(raw=_BUBBLE_RAW_ANY, bubble_id=_BUBBLE_ID_ANY)
    @settings(max_examples=80, deadline=None)
    def test_adversarial_inputs_only_schema_error_or_success(
        self, raw: object, bubble_id: object
    ) -> None:
        try:
            _parse_bubble_from_dict(raw, bubble_id)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")

    @given(raw=_BUBBLE_RAW, bubble_id=_BUBBLE_ID)
    @settings(max_examples=80, deadline=None)
    def test_parsing_is_idempotent(self, raw: dict, bubble_id: str) -> None:
        first = _parse_bubble_from_dict(raw, bubble_id)
        second = _parse_bubble_from_dict(raw, bubble_id)
        self.assertEqual(first, second)


class TestWorkspaceTabsAssemblyFuzz(unittest.TestCase):
    @given(bubble_id=_BUBBLE_ID_ANY, value=_KV_VALUE)
    @settings(max_examples=100, deadline=None)
    def test_assemble_workspace_bubble_never_raises(
        self, bubble_id: object, value: object
    ) -> None:
        try:
            result = _assemble_workspace_bubble(bubble_id, value)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        if result is not None:
            self.assertIsInstance(result, dict)


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
    def test_classify_blob_data_never_raises(self, data: bytes) -> None:
        try:
            msg, refs = classify_blob_data(data)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        if msg is not None:
            self.assertIsInstance(msg, dict)
            self.assertEqual(refs, [])
        else:
            self.assertIsInstance(refs, list)

    @given(
        root_id=_BLOB_ID_HEX,
        extra_ids=st.lists(_BLOB_ID_HEX, max_size=6, unique=True),
        payloads=st.lists(st.binary(max_size=1024), min_size=1, max_size=8),
    )
    @settings(
        max_examples=40,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    def test_traverse_blobs_never_raises(
        self, root_id: str, extra_ids: list[str], payloads: list[bytes]
    ) -> None:
        # CliSessionMeta only requires latestRootBlobId (str); BFS runs after meta parse.
        meta = {"latestRootBlobId": root_id, "createdAt": 1_700_000_000_000}
        blobs: dict[str, bytes] = {root_id: payloads[0]}
        for i, bid in enumerate(extra_ids):
            if bid not in blobs:
                blobs[bid] = payloads[(i + 1) % len(payloads)]
        with tempfile.TemporaryDirectory() as td:
            db_path = os.path.join(td, "store.db")
            _build_store_db_raw(db_path, meta, blobs)
            try:
                messages = traverse_blobs(db_path)
            except Exception as exc:
                self.fail(f"traverse_blobs raised {type(exc).__name__}: {exc}")
            self.assertIsInstance(messages, list)


class TestTextExtractionFuzz(unittest.TestCase):
    @given(bubble=_BUBBLE_LIKE)
    @settings(max_examples=100, deadline=None)
    def test_extract_text_from_bubble_never_raises(self, bubble: dict) -> None:
        try:
            text = extract_text_from_bubble(bubble)
        except Exception as exc:
            self.fail(f"unexpected {type(exc).__name__}: {exc}")
        self.assertIsInstance(text, str)

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
                text = extract_text_from_bubble(bubble)
            except Exception as exc:
                self.fail(f"extract_text_from_bubble raised {type(exc).__name__}: {exc}")
            self.assertIsInstance(text, str)

    @given(
        messages=st.lists(_cli_message(), max_size=12),
        created_at=st.integers(min_value=0, max_value=2_000_000_000_000),
    )
    @settings(max_examples=80, deadline=None)
    def test_messages_to_bubbles_is_idempotent(
        self, messages: list[dict], created_at: int
    ) -> None:
        self.assertEqual(
            messages_to_bubbles(messages, created_at),
            messages_to_bubbles(messages, created_at),
        )


if __name__ == "__main__":
    unittest.main()
