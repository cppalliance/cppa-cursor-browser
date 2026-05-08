"""
Regression tests for CLI argument parity between cursor-chat-browser-python and
claude-code-chat-browser.

Every flag/default documented here must stay in sync with claude-code-chat-browser
so that users switching between the two tools experience zero CLI friction.

Run:
    python -m unittest tests.test_cli_args -v
"""

import ast
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
    parser.add_argument("--debug", action="store_true")
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


# ---------------------------------------------------------------------------
# Werkzeug debugger gating (security): debug must be off by default,
# opt-in via --debug or FLASK_DEBUG=1. Regression for the Critical
# `debug=True` exposure that was hard-coded in app.py.
# ---------------------------------------------------------------------------

class TestDebugFlagGating(unittest.TestCase):

    # -- _resolve_debug_flag helper ------------------------------------------

    def setUp(self):
        # Import from the standalone utility module so the test does not pull
        # Flask into scope (the rest of this file deliberately avoids Flask).
        from utils.debug_flag import resolve_debug_flag
        self._resolve = resolve_debug_flag

    def test_debug_off_when_env_unset_and_no_cli(self):
        self.assertFalse(self._resolve(None, False))

    def test_debug_off_when_env_empty_string(self):
        self.assertFalse(self._resolve("", False))

    def test_debug_off_for_explicit_falsey_env_values(self):
        for v in ("0", "false", "False", "no", "off", "anything-not-truthy"):
            with self.subTest(env=v):
                self.assertFalse(self._resolve(v, False))

    def test_debug_on_for_truthy_env_values(self):
        for v in ("1", "true", "True", "TRUE", "yes", "YES", " 1 "):
            with self.subTest(env=v):
                self.assertTrue(self._resolve(v, False))

    def test_cli_flag_overrides_env(self):
        # Even with FLASK_DEBUG explicitly off, --debug should turn it on.
        self.assertTrue(self._resolve("0", True))
        self.assertTrue(self._resolve(None, True))

    # -- argparse: --debug flag ----------------------------------------------

    def test_app_parser_debug_default_false(self):
        opts = _build_app_parser().parse_args([])
        self.assertFalse(opts.debug)

    def test_app_parser_debug_explicit(self):
        opts = _build_app_parser().parse_args(["--debug"])
        self.assertTrue(opts.debug)

    # -- source-level guard: app.py must NOT carry a literal debug=True -------
    # AST-walk so cosmetic variations (`debug = True`, multi-line formatting,
    # leading whitespace, etc.) cannot bypass the guard. A regression that
    # reintroduces the literal in any form fails this test with the offending
    # line number(s).

    def test_app_py_does_not_hardcode_debug_true(self):
        app_path = os.path.join(REPO_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=app_path)

        offenders = _find_debug_true_offenders(tree)
        self.assertEqual(
            offenders, [],
            "Found a literal `debug=True` keyword argument in app.py at "
            "line(s) %s. The Werkzeug debugger must be opt-in via the "
            "--debug flag or FLASK_DEBUG env var (see issue #9), never "
            "hard-coded." % offenders,
        )


class FindDebugTrueOffendersTests(unittest.TestCase):
    """Unit tests for the AST-walk helper itself, so the regression guard
    above keeps catching what we expect across Python AST shape changes.

    Covers:
      - direct keyword `f(debug=True)` (ast.Constant on 3.8+, ast.NameConstant on 3.7)
      - dict-spread `f(**{"debug": True})` bypass
      - benign shapes that should NOT trip the guard (False, variable, attribute)
    """

    def _find(self, src):
        return _find_debug_true_offenders(ast.parse(src))

    def test_simple_keyword_literal(self):
        self.assertEqual(self._find("app.run(debug=True)"), [1])

    def test_keyword_false_not_flagged(self):
        self.assertEqual(self._find("app.run(debug=False)"), [])

    def test_keyword_variable_not_flagged(self):
        # Out of scope per PR review - only literals are tracked.
        self.assertEqual(self._find("flag = True\napp.run(debug=flag)"), [])

    def test_keyword_attribute_not_flagged(self):
        self.assertEqual(self._find("app.run(debug=cfg.debug_on)"), [])

    def test_dict_spread_literal(self):
        # Determined-bypass shape: kwargs come in via **dict literal.
        offenders = self._find("app.run(**{'debug': True})")
        self.assertEqual(len(offenders), 1)

    def test_dict_spread_false_not_flagged(self):
        self.assertEqual(self._find("app.run(**{'debug': False})"), [])

    def test_dict_spread_other_key_not_flagged(self):
        self.assertEqual(self._find("app.run(**{'foo': True})"), [])


# ---------------------------------------------------------------------------
# AST helper (module-level so it's testable in isolation)
# ---------------------------------------------------------------------------

def _find_debug_true_offenders(tree):
    """Return line numbers of any literal `debug=True` (or `**{"debug": True}`)
    on a Call node in the AST.

    Cross-version safe: works with both ast.Constant (3.8+) and the legacy
    ast.NameConstant shape (3.7) by reading `.value` attribute-style rather
    than narrowing to a specific node class. Only literal True is flagged;
    `debug=variable` and `debug=mod.attr` are out of scope.
    """
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            # Shape 1: direct keyword - f(debug=True)
            if kw.arg == "debug" and _is_literal_true(kw.value):
                offenders.append(kw.lineno)
                continue
            # Shape 2: dict-spread - f(**{"debug": True})
            if kw.arg is None and isinstance(kw.value, ast.Dict):
                for k, v in zip(kw.value.keys, kw.value.values):
                    if _is_str_literal(k, "debug") and _is_literal_true(v):
                        offenders.append(getattr(v, "lineno", kw.lineno))
    return offenders


def _is_literal_true(node):
    """True only when *node* is the literal True (ast.Constant on 3.8+,
    ast.NameConstant on 3.7). Excludes variables/attributes via the strict
    `is True` identity check on `.value`."""
    return getattr(node, "value", None) is True


def _is_str_literal(node, expected):
    """True when *node* is a string literal equal to *expected* (handles
    ast.Constant on 3.8+ and ast.Str on 3.7)."""
    val = getattr(node, "value", getattr(node, "s", None))
    return isinstance(val, str) and val == expected


if __name__ == "__main__":
    unittest.main()
