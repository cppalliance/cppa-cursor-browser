"""Regression tests for issue #24 — typed models + schema validation.

Three fixture-based tests, per the issue acceptance criteria:
  1. known-good Composer schema parses cleanly
  2. missing-field Composer schema raises SchemaError
  3. CliSessionMeta + ``_extract_blob_refs`` together exercise the binary
     blob path (``0x0a 0x20`` marker)

Run:
    python -m unittest tests.test_models -v
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models import (
    Bubble,
    CliSessionMeta,
    Composer,
    ExportEntry,
    SchemaError,
    Workspace,
)
from utils.cli_chat_reader import _extract_blob_refs


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

GOOD_COMPOSER_RAW: dict = {
    "name": "Refactor api/workspaces.py",
    "createdAt": 1_715_000_000_000,
    "lastUpdatedAt": 1_715_000_500_000,
    "fullConversationHeadersOnly": [
        {"bubbleId": "abc123", "role": "user"},
        {"bubbleId": "def456", "role": "assistant"},
    ],
    "modelConfig": {"modelName": "claude-opus-4-7"},
}


def _make_blob_chain(*ref_hashes: str) -> bytes:
    """Build a binary chain blob: tag 0x0a + length 0x20 + 32-byte refs."""
    out = bytearray()
    for h in ref_hashes:
        if len(h) != 64:
            raise ValueError(f"hash must be 64 hex chars, got {len(h)}")
        out.append(0x0A)
        out.append(0x20)
        out.extend(bytes.fromhex(h))
    return bytes(out)


# ---------------------------------------------------------------------------
# 1. Known-good schema
# ---------------------------------------------------------------------------


class ComposerKnownGoodSchema(unittest.TestCase):
    def test_parses_required_and_optional_fields(self) -> None:
        composer = Composer.from_dict(GOOD_COMPOSER_RAW, composer_id="cid-001")

        self.assertEqual(composer.composer_id, "cid-001")
        self.assertEqual(composer.name, "Refactor api/workspaces.py")
        self.assertEqual(composer.created_at, 1_715_000_000_000)
        self.assertEqual(composer.last_updated_at, 1_715_000_500_000)
        self.assertEqual(len(composer.full_conversation_headers_only), 2)
        self.assertEqual(composer.model_config.get("modelName"), "claude-opus-4-7")
        self.assertIs(composer.raw, GOOD_COMPOSER_RAW)

    def test_workspace_parses_with_optional_folder(self) -> None:
        ws = Workspace.from_dict({"folder": "/home/zilin/projects/x"}, workspace_id="ws-1")
        self.assertEqual(ws.workspace_id, "ws-1")
        self.assertEqual(ws.folder, "/home/zilin/projects/x")

        ws_no_folder = Workspace.from_dict({}, workspace_id="cli-only")
        self.assertEqual(ws_no_folder.folder, None)

    def test_export_entry_parses(self) -> None:
        entry = ExportEntry.from_dict({
            "log_id": "L1",
            "title": "Refactor",
            "workspace": "ws-1",
            "created_at": 1_715_000_000_000,
        })
        self.assertEqual(entry.log_id, "L1")
        self.assertEqual(entry.title, "Refactor")
        self.assertEqual(entry.workspace, "ws-1")


# ---------------------------------------------------------------------------
# 2. Missing-field schema → SchemaError
# ---------------------------------------------------------------------------


class ComposerMissingFieldSchema(unittest.TestCase):
    def test_missing_full_conversation_headers_only_raises(self) -> None:
        bad = {k: v for k, v in GOOD_COMPOSER_RAW.items() if k != "fullConversationHeadersOnly"}
        with self.assertRaises(SchemaError) as cm:
            Composer.from_dict(bad, composer_id="cid-001")
        self.assertEqual(cm.exception.model, "Composer")
        self.assertEqual(cm.exception.field, "fullConversationHeadersOnly")

    def test_composer_missing_created_at_is_tolerated(self) -> None:
        # Real Cursor data legitimately omits createdAt for older composers;
        # call sites already fall back to lastUpdatedAt and then to epoch zero.
        bad = {k: v for k, v in GOOD_COMPOSER_RAW.items() if k != "createdAt"}
        composer = Composer.from_dict(bad, composer_id="cid-001")
        self.assertIsNone(composer.created_at)
        self.assertEqual(composer.last_updated_at, 1_715_000_500_000)

    def test_empty_composer_id_raises(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            Composer.from_dict(GOOD_COMPOSER_RAW, composer_id="")
        self.assertEqual(cm.exception.field, "composerId")

    def test_headers_wrong_type_raises(self) -> None:
        bad = dict(GOOD_COMPOSER_RAW, fullConversationHeadersOnly={"not": "a list"})
        with self.assertRaises(SchemaError) as cm:
            Composer.from_dict(bad, composer_id="cid-001")
        self.assertIn("expected list", str(cm.exception))

    def test_bubble_empty_id_raises(self) -> None:
        with self.assertRaises(SchemaError):
            Bubble.from_dict({"text": "hi"}, bubble_id="")

    def test_export_entry_missing_required_raises(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            ExportEntry.from_dict({"title": "x", "workspace": "w"})
        self.assertEqual(cm.exception.field, "log_id")

    def test_schema_error_inherits_value_error(self) -> None:
        # call sites that catch ValueError still trap SchemaError (back-compat)
        try:
            Composer.from_dict({}, composer_id="cid-001")
        except ValueError:
            return
        self.fail("SchemaError did not propagate as ValueError")


# ---------------------------------------------------------------------------
# 3. _extract_blob_refs binary blob path (0x0a 0x20 marker)
# ---------------------------------------------------------------------------


class CliSessionMetaAndBlobChain(unittest.TestCase):
    def test_meta_missing_latest_root_blob_id_raises(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            CliSessionMeta.from_dict({"agentId": "a", "name": "n"})
        self.assertEqual(cm.exception.model, "CliSessionMeta")
        self.assertEqual(cm.exception.field, "latestRootBlobId")

    def test_meta_wrong_type_raises(self) -> None:
        with self.assertRaises(SchemaError):
            CliSessionMeta.from_dict({"latestRootBlobId": 12345})

    def test_meta_parses_then_blob_chain_extracts_refs(self) -> None:
        # Realistic flow: the meta blob points at a root chain blob, whose
        # 0x0a 0x20-prefixed runs are SHA-256 references to JSON message blobs.
        ref1 = "a" * 64
        ref2 = "b" * 64
        ref3 = "c" * 64

        meta = CliSessionMeta.from_dict({
            "agentId": "agent-1",
            "name": "session",
            "latestRootBlobId": ref1,
            "createdAt": 1_715_000_000_000,
        })
        self.assertEqual(meta.latest_root_blob_id, ref1)

        chain_blob = _make_blob_chain(ref1, ref2, ref3)
        refs = _extract_blob_refs(chain_blob)
        self.assertEqual(refs, [ref1, ref2, ref3])

    def test_blob_chain_skips_non_marker_bytes(self) -> None:
        # Garbage prefix + valid run + garbage suffix — only the run extracts.
        ref = "f" * 64
        garbage_before = b"\x01\x02\x03"
        garbage_after = b"\xff\xfe"
        raw = garbage_before + bytes([0x0A, 0x20]) + bytes.fromhex(ref) + garbage_after

        self.assertEqual(_extract_blob_refs(raw), [ref])

    def test_blob_chain_empty_returns_empty_list(self) -> None:
        self.assertEqual(_extract_blob_refs(b""), [])


if __name__ == "__main__":
    unittest.main()
