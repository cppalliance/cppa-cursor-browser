"""Unit tests for POST /api/generate-pdf (api/pdf.py). Closes #72."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

PDF_MAGIC = b"%PDF-"


def _post_pdf(
    client,
    *,
    markdown: str = "",
    title: str = "Chat",
    json_data: dict[str, Any] | None = None,
):
    if json_data is not None:
        return client.post(
            "/api/generate-pdf",
            json=json_data,
            content_type="application/json",
        )
    return client.post(
        "/api/generate-pdf",
        json={"markdown": markdown, "title": title},
        content_type="application/json",
    )


def _assert_pdf_response(response) -> None:
    assert response.status_code == 200
    assert response.content_type.startswith("application/pdf")
    data = response.data
    assert len(data) > 0
    assert data.startswith(PDF_MAGIC)
    # Trailing %%EOF is a minimal structural check (see tests/web-ui-qa-checklist.md).
    assert b"%%EOF" in data[-1024:]


class TestGeneratePdfHappyPath:
    def test_normal_conversation_markdown(self, pdf_client):
        md = """# Chat export

## User question

Please explain **recursion** in Python.

- Base case
- Recursive step

```python
def fact(n):
    return 1 if n < 2 else n * fact(n - 1)
```

---
"""
        response = _post_pdf(pdf_client, markdown=md, title="Happy conversation")
        _assert_pdf_response(response)
        assert (
            'attachment; filename="Happy conversation.pdf"'
            in response.headers.get("Content-Disposition", "")
        )

    def test_empty_json_body_uses_defaults(self, pdf_client):
        response = _post_pdf(pdf_client, json_data={})
        _assert_pdf_response(response)
        assert (
            'attachment; filename="Chat.pdf"'
            in response.headers.get("Content-Disposition", "")
        )

    def test_unsafe_title_characters_sanitized_in_filename(self, pdf_client):
        response = _post_pdf(
            pdf_client,
            markdown="Hello",
            title='bad<>:"/\\|?*name',
        )
        _assert_pdf_response(response)
        assert (
            'attachment; filename="bad_________name.pdf"'
            in response.headers.get("Content-Disposition", "")
        )


class TestGeneratePdfEdgeCases:
    def test_empty_markdown(self, pdf_client):
        response = _post_pdf(pdf_client, markdown="", title="Empty chat")
        _assert_pdf_response(response)

    def test_very_long_content(self, pdf_client):
        line = "This is a repeated paragraph for length testing. " * 20
        md = "\n".join(f"Line {i}: {line}" for i in range(500))
        response = _post_pdf(pdf_client, markdown=md, title="Long chat")
        _assert_pdf_response(response)

    def test_unicode_and_emoji_content(self, pdf_client):
        md = (
            "Smart quotes: “hello” and ’world’\n"
            "Emoji: 🚀🔥 should not break PDF\n"
            "Bullet • point\n"
        )
        response = _post_pdf(pdf_client, markdown=md, title="Unicode chat")
        _assert_pdf_response(response)


class TestGeneratePdfErrors:
    def test_pdf_engine_failure_returns_500(self, pdf_client):
        with patch(
            "fpdf.fpdf.FPDF.output",
            side_effect=RuntimeError("simulated failure"),
        ):
            response = _post_pdf(pdf_client, markdown="Hello", title="Fail")
        assert response.status_code == 500
        assert response.get_json() == {"error": "Failed to generate PDF"}

    def test_invalid_export_payload_returns_500(self, pdf_client):
        # Conversation IDs are resolved client-side (tabs API) before markdown is
        # POSTed here. A non-string markdown field mimics a corrupted export request.
        response = _post_pdf(
            pdf_client,
            json_data={"markdown": ["not", "a", "string"], "title": "Bad payload"},
        )
        assert response.status_code == 500
        assert response.get_json() == {"error": "Failed to generate PDF"}
