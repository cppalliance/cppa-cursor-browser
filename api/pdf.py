"""
API route for PDF generation — mirrors src/app/api/generate-pdf/route.ts
POST /api/generate-pdf
"""

import io
import logging
import re
from typing import Any

from flask import Blueprint, Response, jsonify, request

bp = Blueprint("pdf", __name__)
_logger = logging.getLogger(__name__)


def _safe_text(text: str) -> str:
    """Replace characters that fpdf2's built-in fonts cannot render."""
    if not text:
        return ""
    # Replace common problematic Unicode characters with ASCII equivalents
    replacements = {
        "\u2018": "'", "\u2019": "'",  # smart quotes
        "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--",
        "\u2026": "...",
        "\u2022": "*",  # bullet
        "\u00a0": " ",  # non-breaking space
        "\u200b": "",   # zero-width space
        "\u2003": " ",  # em space
        "\ufeff": "",   # BOM
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    # Remove any remaining non-latin1 characters
    result = []
    for ch in text:
        try:
            ch.encode("latin-1")
            result.append(ch)
        except UnicodeEncodeError:
            result.append("?")
    return "".join(result)


@bp.route("/api/generate-pdf", methods=["POST"])
def generate_pdf() -> tuple[Response, int] | Response:
    try:
        body = request.get_json(silent=True) or {}
        markdown_text = body.get("markdown", "")
        title = body.get("title", "Chat")

        from fpdf import FPDF

        class PDFDoc(FPDF):
            def header(self) -> None:
                pass

            def footer(self) -> None:
                self.set_y(-15)
                self.set_font("Helvetica", "I", 8)
                self.cell(0, 10, f"Page {self.page_no()}/{{nb}}", align="C")

        pdf = PDFDoc()
        pdf.alias_nb_pages()
        pdf.set_auto_page_break(auto=True, margin=25)
        pdf.add_page()
        pdf.set_margins(20, 20, 20)

        # Title
        pdf.set_font("Helvetica", "B", 20)
        pdf.cell(0, 12, _safe_text(title), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(8)

        # Parse markdown line by line
        lines = markdown_text.split("\n")
        in_code_block = False
        code_lines: list[str] = []

        for line in lines:
            try:
                # Code block toggle
                if line.strip().startswith("```"):
                    if in_code_block:
                        # End code block -> render collected lines
                        _render_code_block(pdf, "\n".join(code_lines))
                        code_lines = []
                        in_code_block = False
                    else:
                        in_code_block = True
                    continue

                if in_code_block:
                    code_lines.append(line)
                    continue

                # Headings
                if line.startswith("### "):
                    pdf.ln(4)
                    pdf.set_font("Helvetica", "B", 13)
                    pdf.set_text_color(96, 165, 250)
                    pdf.cell(0, 8, _safe_text(line[4:]), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)
                    continue

                if line.startswith("## "):
                    pdf.ln(4)
                    pdf.set_font("Helvetica", "B", 15)
                    pdf.set_text_color(59, 130, 246)
                    pdf.cell(0, 9, _safe_text(line[3:]), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)
                    continue

                if line.startswith("# "):
                    pdf.ln(4)
                    pdf.set_font("Helvetica", "B", 18)
                    pdf.set_text_color(37, 99, 235)
                    pdf.cell(0, 10, _safe_text(line[2:]), new_x="LMARGIN", new_y="NEXT")
                    pdf.set_text_color(0, 0, 0)
                    continue

                # Horizontal rule
                if line.strip() == "---":
                    pdf.ln(3)
                    y = pdf.get_y()
                    pdf.set_draw_color(180, 180, 180)
                    pdf.line(20, y, pdf.w - 20, y)
                    pdf.ln(3)
                    continue

                # Italic line (metadata)
                if line.startswith("_") and line.endswith("_"):
                    pdf.set_font("Helvetica", "I", 9)
                    pdf.set_text_color(120, 120, 120)
                    pdf.multi_cell(0, 5, _safe_text(line.strip("_")))
                    pdf.set_text_color(0, 0, 0)
                    continue

                # List items
                if line.startswith("- "):
                    pdf.set_font("Helvetica", "", 10)
                    pdf.cell(5, 5, "*")  # bullet (using safe ASCII)
                    pdf.multi_cell(0, 5, _safe_text(line[2:]))
                    continue

                # Normal paragraph
                if line.strip():
                    pdf.set_font("Helvetica", "", 10)
                    # Strip markdown bold/italic for PDF
                    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", line)
                    clean = re.sub(r"\*(.+?)\*", r"\1", clean)
                    clean = re.sub(r"`(.+?)`", r"\1", clean)
                    pdf.multi_cell(0, 5, _safe_text(clean))
                else:
                    pdf.ln(3)
            except Exception:
                # Skip lines that cause rendering errors
                continue

        buf = io.BytesIO()
        pdf.output(buf)
        buf.seek(0)

        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
        return Response(
            buf.getvalue(),
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.pdf"',
            },
        )

    except Exception as e:
        _logger.error(
            "Failed to generate PDF: %s (%s)",
            e,
            type(e).__name__,
            exc_info=True,
        )
        return jsonify({"error": "Failed to generate PDF"}), 500


def _render_code_block(pdf: Any, code_text: str) -> None:
    """Render a code block with a dark background."""
    pdf.ln(3)
    pdf.set_font("Courier", "", 8)
    pdf.set_fill_color(30, 30, 30)
    pdf.set_text_color(212, 212, 212)

    # Calculate needed height
    lines = code_text.split("\n")
    line_height = 4
    block_height = len(lines) * line_height + 6  # padding

    # Cap the block height to avoid overflow
    max_block_height = pdf.h - 50
    if block_height > max_block_height:
        block_height = max_block_height

    # Check if we need a page break
    if pdf.get_y() + min(block_height, 60) > pdf.h - 25:
        pdf.add_page()

    x = pdf.get_x()
    y = pdf.get_y()
    w = pdf.w - 40  # margins

    # Background rectangle
    pdf.rect(x, y, w, block_height, "F")

    # Text
    pdf.set_xy(x + 3, y + 3)
    for i, line in enumerate(lines):
        if pdf.get_y() > pdf.h - 25:
            pdf.add_page()
            pdf.set_font("Courier", "", 8)
            pdf.set_fill_color(30, 30, 30)
            pdf.set_text_color(212, 212, 212)
        try:
            pdf.cell(0, line_height, _safe_text(line), new_x="LMARGIN", new_y="NEXT")
        except Exception:
            pdf.cell(0, line_height, "[encoding error]", new_x="LMARGIN", new_y="NEXT")
        if i < len(lines) - 1:
            pdf.set_x(x + 3)

    pdf.set_y(y + block_height + 2)
    pdf.set_text_color(0, 0, 0)
    pdf.set_font("Helvetica", "", 10)
    pdf.ln(3)
