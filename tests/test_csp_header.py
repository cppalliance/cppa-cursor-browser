"""Content-Security-Policy header coverage for served HTML pages."""

from __future__ import annotations

import re

from app import build_content_security_policy


def _extract_csp_nonce(csp: str) -> str:
    match = re.search(r"'nonce-([^']+)'", csp)
    assert match is not None
    return match.group(1)


def test_html_page_has_content_security_policy_header(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy")
    assert csp
    assert "default-src 'self'" in csp
    assert "script-src 'self' https://cdnjs.cloudflare.com" in csp
    assert "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com" in csp
    assert "'nonce-" in csp


def test_html_page_nonce_matches_inline_script(client) -> None:
    response = client.get("/search")
    assert response.status_code == 200
    csp = response.headers.get("Content-Security-Policy", "")
    nonce = _extract_csp_nonce(csp)
    html = response.get_data(as_text=True)
    assert f'nonce="{nonce}"' in html
    assert build_content_security_policy(nonce) == csp


def test_json_api_response_has_no_content_security_policy_header(client) -> None:
    response = client.get("/api/workspaces")
    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy") is None


def test_workspace_page_has_content_security_policy_header(client) -> None:
    response = client.get("/workspace/global")
    assert response.status_code == 200
    assert response.headers.get("Content-Security-Policy")
