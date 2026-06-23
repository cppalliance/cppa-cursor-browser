"""Unit tests for services.export_engine orchestration."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from services.export_engine import (  # noqa: E402
    GlobalDbExportData,
    WorkspaceOrchestration,
    collect_export_entries,
)


class _TempExportPathsMixin:
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_ws = os.path.join(self._tmp.name, "ws")
        self.tmp_out = os.path.join(self._tmp.name, "out")
        os.makedirs(self.tmp_ws, exist_ok=True)
        os.makedirs(self.tmp_out, exist_ok=True)


class TestCollectExportEntriesNocache(_TempExportPathsMixin, unittest.TestCase):
    def test_nocache_env_passed_to_prepare_workspace_orchestration(self):
        with patch.dict(os.environ, {"CURSOR_CHAT_BROWSER_NOCACHE": "1"}):
            with patch(
                "services.export_engine.prepare_workspace_orchestration",
            ) as mock_prepare:
                mock_prepare.return_value = MagicMock(spec=WorkspaceOrchestration)
                with patch(
                    "services.export_engine.load_global_db_export_data",
                    return_value=None,
                ):
                    collect_export_entries(
                        workspace_path=self.tmp_ws,
                        exclusion_rules=[],
                        since="all",
                        last_export_ms=0,
                        out_dir=self.tmp_out,
                        include_composer=False,
                        include_cli=False,
                    )
        mock_prepare.assert_called_once()
        self.assertTrue(mock_prepare.call_args.kwargs["nocache"])


class TestCollectExportEntriesCorruptComposer(
    _TempExportPathsMixin,
    unittest.TestCase,
):
    def test_non_dict_composer_row_is_skipped(self):
        ctx = MagicMock()
        ctx.project_name_to_workspace_id = {}
        ctx.workspace_path_to_id = {}
        ctx.composer_id_to_workspace_id = {}
        ctx.invalid_workspace_ids = set()
        orch = WorkspaceOrchestration(
            workspace_path=self.tmp_ws,
            workspace_entries=[],
            fingerprint={},
            ctx=ctx,
            workspace_id_to_display_name={},
            workspace_id_to_slug={},
        )

        class FakeRow:
            def __getitem__(self, key: str) -> str:
                if key == "key":
                    return "composerData:bad-row"
                return "[]"

        db_data = GlobalDbExportData(
            project_layouts_map={},
            bubble_map={},
            code_block_diff_map={},
            ide_composer_rows=[FakeRow()],
            invalid_workspace_aliases={},
        )
        with patch(
            "services.export_engine.prepare_workspace_orchestration",
            return_value=orch,
        ):
            with patch(
                "services.export_engine.load_global_db_export_data",
                return_value=db_data,
            ):
                exported = collect_export_entries(
                    workspace_path=self.tmp_ws,
                    exclusion_rules=[],
                    since="all",
                    last_export_ms=0,
                    out_dir=self.tmp_out,
                    include_cli=False,
                )
        self.assertEqual(exported, [])


if __name__ == "__main__":
    unittest.main()
