"""
Headless-browser XSS regression tests (issue #11 / sprint item #3).

Exercises the production render path: Marked.js + DOMPurify via
renderMarkdownSafe() in static/js/app.js, then DOM insertion via innerHTML
(the same pattern as templates/workspace.html).

Pages are served with the same CSP as production; inline event handlers are
blocked by the header, so these tests assert dangerous markup is stripped from
the sink (and use a probe where execution is still observable).

Static source greps live in tests/test_xss_sanitization.py.

Run:
    playwright install chromium
    pytest -q tests/test_xss_browser.py
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any, Generator

import pytest
from werkzeug.serving import make_server

from app import create_app

if TYPE_CHECKING:
    from playwright.sync_api import Page

# Representative vectors from the sprint issue.
XSS_VECTORS: list[tuple[str, str]] = [
    ("img_onerror", '<img src=x onerror="window.__xssProbe=1">'),
    ("script_tag", "<script>window.__xssProbe=1</script>"),
    ("javascript_uri", "[x](javascript:window.__xssProbe=1)"),
    (
        "data_uri",
        "[x](data:text/html,<script>window.__xssProbe=1</script>)",
    ),
    (
        "svg_onload",
        '<svg xmlns="http://www.w3.org/2000/svg" onload="window.__xssProbe=1"></svg>',
    ),
]

_PROBE_SETTLE_MS = 150

_INSPECT_XSS_SINK = f"""
async ({{ payload, useSafeRender }}) => {{
  window.__xssProbe = 0;
  const host = document.getElementById('xss-browser-test-host');
  if (host) {{
    host.remove();
  }}
  const el = document.createElement('div');
  el.id = 'xss-browser-test-host';
  document.body.appendChild(el);
  let sanitizeCalls = 0;
  let restoreSanitize = null;
  if (useSafeRender) {{
    const originalSanitize = DOMPurify.sanitize.bind(DOMPurify);
    DOMPurify.sanitize = (...args) => {{
      sanitizeCalls += 1;
      return originalSanitize(...args);
    }};
    restoreSanitize = () => {{
      DOMPurify.sanitize = originalSanitize;
    }};
  }}
  try {{
    if (useSafeRender) {{
      if (typeof renderMarkdownSafe !== 'function') {{
        throw new Error('renderMarkdownSafe is not defined — is app.js loaded?');
      }}
      el.innerHTML = renderMarkdownSafe(payload);
    }} else {{
      const html = marked.parse(payload, {{ breaks: true, gfm: true }});
      el.innerHTML = html;
    }}
    await new Promise((resolve) => setTimeout(resolve, {_PROBE_SETTLE_MS}));
    const result = {{
      probe: window.__xssProbe || 0,
      onerrorAttr: el.querySelector('[onerror]') !== null,
      scriptTag: el.querySelector('script') !== null,
      jsHref: el.querySelector('[href^="javascript:"]') !== null,
      dataHref: el.querySelector('[href^="data:"]') !== null,
      svgOnload: el.querySelector('svg[onload]') !== null,
      sanitizeCalls: useSafeRender ? sanitizeCalls : 0,
    }};
    if (!useSafeRender) {{
      result.html = el.innerHTML;
    }}
    return result;
  }} finally {{
    if (restoreSanitize) {{
      restoreSanitize();
    }}
  }}
}}
"""


def _assert_sink_neutralized(result: dict[str, Any], vector_name: str) -> None:
    assert result["probe"] == 0, (
        f"XSS probe fired for vector {vector_name!r}; "
        "renderMarkdownSafe must neutralize this payload"
    )
    assert not result["onerrorAttr"], (
        f"onerror attribute survived sanitization for {vector_name!r}"
    )
    assert not result["scriptTag"], (
        f"<script> survived sanitization for {vector_name!r}"
    )
    assert not result["jsHref"], (
        f"javascript: URI survived sanitization for {vector_name!r}"
    )
    assert not result["dataHref"], (
        f"data: URI survived sanitization for {vector_name!r}"
    )
    assert not result["svgOnload"], (
        f"svg onload survived sanitization for {vector_name!r}"
    )
    assert result.get("sanitizeCalls", 0) >= 1, (
        f"DOMPurify.sanitize was not called for {vector_name!r}; "
        "renderMarkdownSafe must not take the escapeHtml-only path for these payloads"
    )


@pytest.fixture(scope="module")
def playwright_browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def browser_page(playwright_browser) -> Generator["Page", None, None]:
    page = playwright_browser.new_page()
    try:
        yield page
    finally:
        page.close()


@pytest.fixture
def live_server_url(workspace_storage: str) -> Generator[str, None, None]:
    app = create_app()
    app.config["TESTING"] = True
    app.config["EXCLUSION_RULES"] = []
    server = make_server("127.0.0.1", 0, app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def app_page(browser_page: "Page", live_server_url: str) -> "Page":
    """Any HTML page that loads base.html scripts (marked, DOMPurify, app.js)."""
    response = browser_page.goto(f"{live_server_url}/", wait_until="networkidle")
    assert response is not None and response.ok
    browser_page.wait_for_function(
        "() => typeof renderMarkdownSafe === 'function' && typeof DOMPurify !== 'undefined'"
    )
    return browser_page


@pytest.mark.browser
@pytest.mark.parametrize("vector_name,payload", XSS_VECTORS, ids=[v[0] for v in XSS_VECTORS])
def test_render_markdown_safe_neutralizes_xss_vector(
    app_page: "Page", vector_name: str, payload: str
) -> None:
    result = app_page.evaluate(
        _INSPECT_XSS_SINK, {"payload": payload, "useSafeRender": True}
    )
    _assert_sink_neutralized(result, vector_name)


@pytest.mark.browser
def test_bare_marked_parse_leaves_dangerous_markup_negative_control(
    app_page: "Page",
) -> None:
    """Without DOMPurify.sanitize, marked output still carries exploitable markup."""
    payload = XSS_VECTORS[0][1]
    result = app_page.evaluate(
        _INSPECT_XSS_SINK, {"payload": payload, "useSafeRender": False}
    )
    assert result["onerrorAttr"] or result["scriptTag"], (
        "negative control: bare marked.parse should leave at least one dangerous "
        "node/attribute in the DOM sink"
    )
