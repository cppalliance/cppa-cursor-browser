"""Content-Security-Policy header coverage for served HTML pages."""

from __future__ import annotations

import re

_CDNJS_ORIGIN = "https://cdnjs.cloudflare.com"
_EXPECTED_CSP_DIRECTIVES = {
    "default-src": ["'self'"],
    "script-src": ["'self'", _CDNJS_ORIGIN],
    "style-src": ["'self'", "'unsafe-inline'", _CDNJS_ORIGIN],
    "img-src": ["'self'", "data:"],
    "connect-src": ["'self'"],
    "font-src": ["'self'"],
    "object-src": ["'none'"],
    "form-action": ["'self'"],
    "base-uri": ["'self'"],
    "frame-ancestors": ["'none'"],
}


def _parse_csp_directives(csp: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        directives[name] = value.split()
    return directives


def _extract_csp_nonce(csp: str) -> str:
    match = re.search(r"'nonce-([^']+)'", csp)
    assert match is not None
    return match.group(1)


def _assert_expected_csp_directives(csp: str, nonce: str) -> None:
    directives = _parse_csp_directives(csp)
    assert set(directives) == set(_EXPECTED_CSP_DIRECTIVES)

    for name, expected_tokens in _EXPECTED_CSP_DIRECTIVES.items():
        actual_tokens = directives[name]
        if name == "script-src":
            nonce_tokens = [
                token for token in actual_tokens if token.startswith("'nonce-")
            ]
            non_nonce_tokens = [
                token for token in actual_tokens if not token.startswith("'nonce-")
            ]
            assert non_nonce_tokens == expected_tokens
            assert len(nonce_tokens) == 1
            assert nonce_tokens[0] == f"'nonce-{nonce}'"
        else:
            assert actual_tokens == expected_tokens


def _assert_inline_script_nonce(html: str, nonce: str) -> None:
    pattern = rf'<script\s+nonce="{re.escape(nonce)}"'
    assert re.search(pattern, html) is not None


def test_html_page_has_content_security_policy_header(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy")
    assert csp
    nonce = _extract_csp_nonce(csp)
    _assert_expected_csp_directives(csp, nonce)


def test_html_page_nonce_matches_inline_script(client) -> None:
    first = client.get("/search")
    second = client.get("/search")
    assert first.status_code == 200
    assert second.status_code == 200

    first_csp = first.headers.get("Content-Security-Policy", "")
    second_csp = second.headers.get("Content-Security-Policy", "")
    first_nonce = _extract_csp_nonce(first_csp)
    second_nonce = _extract_csp_nonce(second_csp)
    assert first_nonce != second_nonce

    html = first.get_data(as_text=True)
    _assert_expected_csp_directives(first_csp, first_nonce)
    _assert_inline_script_nonce(html, first_nonce)


def test_json_api_response_has_no_content_security_policy_header(client) -> None:
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy") is None


def test_workspace_page_has_content_security_policy_header(client) -> None:
    response = client.get("/workspace/global")
    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy")
