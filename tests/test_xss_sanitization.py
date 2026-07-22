"""
Regression tests for issue #11 — XSS via unsanitised Marked.js output.

The frontend must:
  1. Load DOMPurify alongside Marked.js in base.html.
  2. Provide a `renderMarkdownSafe(text)` helper in static/js/app.js that
     wraps marked.parse(...) with DOMPurify.sanitize(...).
  3. Use that helper at every site where markdown HTML reaches the DOM
     (workspace.html → innerHTML) or a downloadable HTML blob (download.js).
  4. Never call marked.parse(...) without a DOMPurify.sanitize(...) wrap.

These checks are static-source assertions — a future regression that
re-introduces a bare marked.parse call would slip past the headless browser
suite if nobody exercised that path. Source-grep guards are the cheap backstop;
authoritative execution checks live in tests/test_xss_browser.py (Playwright).

Run:
    python -m unittest tests.test_xss_sanitization -v
    playwright install chromium
    pytest -q tests/test_xss_browser.py
"""

import glob
import os
import re
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), "r", encoding="utf-8") as f:
        return f.read()


def _discover_frontend_source_files():
    """All templates/*.html and static/js/*.js - catches new files without
    updating a fixed list (PR review hardening).
    """
    out = []
    for pattern in (
        os.path.join(REPO_ROOT, "templates", "*.html"),
        os.path.join(REPO_ROOT, "static", "js", "*.js"),
    ):
        for full in sorted(glob.glob(pattern)):
            rel = os.path.relpath(full, REPO_ROOT).replace("\\", "/")
            out.append(rel)
    return out


class TestDOMPurifyLoaded(unittest.TestCase):

    def test_base_html_includes_dompurify_cdn(self):
        src = _read("templates/base.html")
        self.assertIn("dompurify", src.lower(),
                      "templates/base.html must load DOMPurify before any page-level script")

    def test_base_html_loads_dompurify_after_marked(self):
        # Order matters: DOMPurify must be loaded before any script that calls
        # renderMarkdownSafe(). Loading it after Marked.js but before app.js
        # is the conventional spot.
        src = _read("templates/base.html")
        marked_pos = src.lower().find("marked.min.js")
        purify_pos = src.lower().find("purify.min.js")
        app_js_pos = src.find("/static/js/app.js")
        self.assertGreater(marked_pos, 0, "Marked.js must be loaded")
        self.assertGreater(purify_pos, 0, "DOMPurify must be loaded")
        self.assertGreater(app_js_pos, 0, "app.js must be loaded")
        self.assertLess(marked_pos, purify_pos,
                        "DOMPurify must load after Marked.js (matches the test name + comment)")
        self.assertLess(purify_pos, app_js_pos,
                        "DOMPurify must load before app.js so renderMarkdownSafe can use it")


class TestRenderMarkdownSafeHelper(unittest.TestCase):

    def test_app_js_defines_render_markdown_safe(self):
        src = _read("static/js/app.js")
        self.assertIn("renderMarkdownSafe", src,
                      "static/js/app.js must define renderMarkdownSafe()")

    def test_render_markdown_safe_invokes_dompurify(self):
        src = _read("static/js/app.js")
        # Look for the function body — must call DOMPurify.sanitize.
        self.assertIn("DOMPurify.sanitize", src,
                      "renderMarkdownSafe() must invoke DOMPurify.sanitize(...)")

    def test_render_markdown_safe_falls_back_safely(self):
        """If DOMPurify or marked is unavailable, the helper must NOT call
        marked.parse alone. It must fall back to escapeHtml or similar."""
        src = _read("static/js/app.js")
        self.assertIn("escapeHtml", src,
                      "renderMarkdownSafe() must fall back to escapeHtml when libs are missing")


class TestCallSitesUseSafeHelper(unittest.TestCase):

    def test_workspace_html_uses_safe_helper(self):
        src = _read("templates/workspace.html")
        # Either the helper is called, or DOMPurify.sanitize is inlined.
        self.assertTrue(
            "renderMarkdownSafe" in src or "DOMPurify.sanitize" in src,
            "templates/workspace.html must sanitise markdown before innerHTML"
        )

    def test_download_js_uses_safe_helper(self):
        src = _read("static/js/download.js")
        self.assertTrue(
            "renderMarkdownSafe" in src or "DOMPurify.sanitize" in src,
            "static/js/download.js must sanitise markdown before writing to download blob"
        )


class TestNoBareMarkedParse(unittest.TestCase):
    """The class of bug we're fixing: a bare `marked.parse(...)` whose return
    value is then injected into innerHTML or a download blob. If a future edit
    reintroduces the pattern, this test fails.

    A `marked.parse(...)` IS allowed inside renderMarkdownSafe (because that
    function then sanitises). We allow at most one such call across the
    frontend — the one inside the helper itself."""

    def test_marked_parse_appears_only_inside_safe_helper(self):
        marked_call = re.compile(r"marked\.parse\s*\(")
        per_file = {}
        for rel in _discover_frontend_source_files():
            full = os.path.join(REPO_ROOT, rel)
            if not os.path.exists(full):
                continue
            with open(full, "r", encoding="utf-8") as f:
                src = f.read()
            n = len(marked_call.findall(src))
            per_file[rel] = n
        # Exactly one allowed — the call inside renderMarkdownSafe in app.js.
        self.assertEqual(per_file.get("static/js/app.js", 0), 1,
                         "static/js/app.js should contain marked.parse exactly once "
                         "(inside renderMarkdownSafe). per_file=%s" % per_file)
        # All other frontend files must have ZERO bare marked.parse calls.
        for rel, n in per_file.items():
            if rel == "static/js/app.js":
                continue
            self.assertEqual(
                n, 0,
                "%s contains a bare marked.parse(...) call - wrap it via "
                "renderMarkdownSafe() instead. per_file=%s" % (rel, per_file)
            )


if __name__ == "__main__":
    unittest.main()
