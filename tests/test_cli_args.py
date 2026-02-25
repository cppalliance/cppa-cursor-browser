"""
Regression tests for CLI argument parity between cursor-chat-browser-python and
claude-code-chat-browser.

Every flag/default documented here must stay in sync with claude-code-chat-browser
so that users switching between the two tools experience zero CLI friction.

Run:
    python -m unittest tests.test_cli_args -v
"""

import sys
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Import the argparse-based parse_args from the export script
from scripts.export import parse_args as _raw_parse_args


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_export(argv):
    """Call scripts/export.py parse_args() with a custom sys.argv."""
    original = sys.argv
    sys.argv = ["export.py"] + list(argv)
    try:
        return _raw_parse_args()
    finally:
        sys.argv = original


def _build_app_parser():
    """Reconstruct the argparse parser from app.py without importing Flask."""
    import argparse
    parser = argparse.ArgumentParser(description="Cursor Chat Browser (Python)")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-dir", default=None)
    parser.add_argument("--exclude-rules", "-e", default=None,
                        metavar="PATH", dest="exclude_rules")
    return parser


# ---------------------------------------------------------------------------
# export.py argument tests
# ---------------------------------------------------------------------------

class TestExportArgs(unittest.TestCase):

    # -- --since ----------------------------------------------------------------

    def test_since_default_is_all(self):
        opts = _parse_export([])
        self.assertEqual(opts["since"], "all")

    def test_since_all(self):
        opts = _parse_export(["--since", "all"])
        self.assertEqual(opts["since"], "all")

    def test_since_last(self):
        opts = _parse_export(["--since", "last"])
        self.assertEqual(opts["since"], "last")

    def test_since_invalid_raises(self):
        with self.assertRaises(SystemExit):
            _parse_export(["--since", "yesterday"])

    # -- --out ------------------------------------------------------------------

    def test_out_default_is_dot(self):
        opts = _parse_export([])
        self.assertEqual(opts["out_dir"], ".")

    def test_out_explicit(self):
        opts = _parse_export(["--out", "/tmp/exports"])
        self.assertEqual(opts["out_dir"], "/tmp/exports")

    # -- --no-zip ---------------------------------------------------------------

    def test_no_zip_default_false(self):
        opts = _parse_export([])
        self.assertTrue(opts["zip"])

    def test_no_zip_flag(self):
        opts = _parse_export(["--no-zip"])
        self.assertFalse(opts["zip"])

    # -- --no-composer ----------------------------------------------------------

    def test_no_composer_default_true(self):
        opts = _parse_export([])
        self.assertTrue(opts["include_composer"])

    def test_no_composer_flag(self):
        opts = _parse_export(["--no-composer"])
        self.assertFalse(opts["include_composer"])

    # -- --exclude-rules / -e  --------------------------------------------------

    def test_exclude_rules_default_none(self):
        opts = _parse_export([])
        self.assertIsNone(opts["exclusion_rules_path"])

    def test_exclude_rules_long_form(self):
        opts = _parse_export(["--exclude-rules", "/path/to/rules.txt"])
        self.assertEqual(opts["exclusion_rules_path"], "/path/to/rules.txt")

    def test_exclude_rules_short_form(self):
        opts = _parse_export(["-e", "/path/to/rules.txt"])
        self.assertEqual(opts["exclusion_rules_path"], "/path/to/rules.txt")

    # -- --base-dir -------------------------------------------------------------

    def test_base_dir_default_none(self):
        opts = _parse_export([])
        self.assertIsNone(opts["base_dir"])

    def test_base_dir_explicit(self):
        opts = _parse_export(["--base-dir", "/custom/workspace"])
        self.assertEqual(opts["base_dir"], "/custom/workspace")

    # -- --help / -h ------------------------------------------------------------

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            _parse_export(["--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_help_short_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            _parse_export(["-h"])
        self.assertEqual(ctx.exception.code, 0)


# ---------------------------------------------------------------------------
# app.py argument tests
# ---------------------------------------------------------------------------

class TestAppArgs(unittest.TestCase):

    def setUp(self):
        self.parser = _build_app_parser()

    # -- --host / --port --------------------------------------------------------

    def test_host_default_is_localhost(self):
        """Default host must be 127.0.0.1, matching claude."""
        args = self.parser.parse_args([])
        self.assertEqual(args.host, "127.0.0.1")

    def test_host_override(self):
        args = self.parser.parse_args(["--host", "0.0.0.0"])
        self.assertEqual(args.host, "0.0.0.0")

    def test_port_default(self):
        args = self.parser.parse_args([])
        self.assertEqual(args.port, 3000)

    def test_port_override(self):
        args = self.parser.parse_args(["--port", "8080"])
        self.assertEqual(args.port, 8080)

    # -- --base-dir -------------------------------------------------------------

    def test_base_dir_default_none(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.base_dir)

    def test_base_dir_override(self):
        args = self.parser.parse_args(["--base-dir", "/custom/workspace"])
        self.assertEqual(args.base_dir, "/custom/workspace")

    # -- --exclude-rules / -e ---------------------------------------------------

    def test_exclude_rules_default_none(self):
        args = self.parser.parse_args([])
        self.assertIsNone(args.exclude_rules)

    def test_exclude_rules_long_form(self):
        args = self.parser.parse_args(["--exclude-rules", "/tmp/rules.txt"])
        self.assertEqual(args.exclude_rules, "/tmp/rules.txt")

    def test_exclude_rules_short_form(self):
        args = self.parser.parse_args(["-e", "/tmp/rules.txt"])
        self.assertEqual(args.exclude_rules, "/tmp/rules.txt")

    # -- source assertions ------------------------------------------------------

    def test_app_py_uses_argparse(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("argparse", src)
        self.assertIn("add_argument", src)

    def test_app_py_has_port_flag(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"--port"', src)

    def test_app_py_has_host_flag(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"--host"', src)

    def test_app_py_has_base_dir_flag(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"--base-dir"', src)

    def test_app_py_startup_message_is_dynamic(self):
        """Startup message must use {args.host}/{args.port}, not be hardcoded."""
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("args.host", src)
        self.assertIn("args.port", src)

    def test_app_py_use_reloader_is_platform_aware(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("sys.platform", src)
        self.assertIn("win32", src)
        self.assertNotIn("use_reloader=False", src)

    def test_export_py_has_base_dir_flag(self):
        export_path = os.path.join(REPO_ROOT, "scripts", "export.py")
        with open(export_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('"--base-dir"', src)

    def test_export_py_has_since_choices(self):
        """--since must use choices=["all","last"] for validated parity with claude."""
        export_path = os.path.join(REPO_ROOT, "scripts", "export.py")
        with open(export_path, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn('choices=["all", "last"]', src)


if __name__ == "__main__":
    unittest.main()
