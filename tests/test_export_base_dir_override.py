"""Regression tests for issue #4 — --base-dir must not mutate WORKSPACE_PATH."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from scripts import export as export_script  # noqa: E402


class TestExportBaseDirOverride(unittest.TestCase):
    def test_main_passes_base_dir_as_resolve_override(self):
        opts = {
            "since": "all",
            "out_dir": ".",
            "include_composer": False,
            "zip": True,
            "exclusion_rules_path": None,
            "base_dir": "/custom/workspace",
        }
        with (
            patch.object(export_script, "parse_args", return_value=opts),
            patch.object(
                export_script,
                "collect_export_entries",
                return_value=[],
            ) as mock_collect,
            patch.object(
                export_script,
                "resolve_workspace_path",
                return_value="/resolved/workspace",
            ) as mock_resolve,
            self.assertRaises(SystemExit) as ctx,
        ):
            export_script.main()
        self.assertEqual(ctx.exception.code, 0)
        mock_resolve.assert_called_once_with(override="/custom/workspace")
        mock_collect.assert_called_once()
        self.assertEqual(
            mock_collect.call_args.kwargs["workspace_path"],
            "/resolved/workspace",
        )

    def test_base_dir_does_not_mutate_workspace_path_env(self):
        opts = {
            "since": "all",
            "out_dir": ".",
            "include_composer": False,
            "zip": True,
            "exclusion_rules_path": None,
            "base_dir": "/custom/workspace",
        }
        sentinel = "/original/env/workspace"
        prior = os.environ.get("WORKSPACE_PATH")
        os.environ["WORKSPACE_PATH"] = sentinel
        try:
            with (
                patch.object(export_script, "parse_args", return_value=opts),
                patch.object(export_script, "collect_export_entries", return_value=[]),
                patch.object(
                    export_script,
                    "resolve_workspace_path",
                    return_value="/resolved/workspace",
                ),
                self.assertRaises(SystemExit),
            ):
                export_script.main()
            self.assertEqual(os.environ.get("WORKSPACE_PATH"), sentinel)
        finally:
            if prior is None:
                os.environ.pop("WORKSPACE_PATH", None)
            else:
                os.environ["WORKSPACE_PATH"] = prior


if __name__ == "__main__":
    unittest.main()
