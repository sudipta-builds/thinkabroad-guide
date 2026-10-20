from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path

from docx import Document
from docx.shared import Pt
from fpdf import FPDF

logger = logging.getLogger(__name__)

_MEDIA = Path("data/media")


def export_cv_files(markdown: str, *, stem: str | None = None) -> dict[str, Path]:
    """Write ATS-friendly DOCX + PDF from markdown CV. Returns local paths."""
    _MEDIA.mkdir(parents=True, exist_ok=True)
    base = stem or uuid.uuid4().hex
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", base).strip("-")[:40] or "cv"
    docx_path = _MEDIA / f"{safe}-{uuid.uuid4().hex[:8]}.docx"
    pdf_path = _MEDIA / f"{safe}-{uuid.uuid4().hex[:8]}.pdf"

    lines = _md_to_lines(markdown)
    _write_docx(docx_path, lines)
    _write_pdf(pdf_path, lines)
    return {"docx": docx_path, "pdf": pdf_path}


def _md_to_lines(markdown: str) -> list[tuple[str, str]]:
    """Return list of (kind, text) where kind is h1|h2|body|bullet."""
    out: list[tuple[str, str]] = []
    for raw in (markdown or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            out.append(("blank", ""))
            continue
        if line.startswith("# "):
            out.append(("h1", line[2:].strip()))
        elif line.startswith("## "):
            out.append(("h2", line[3:].strip()))
        elif line.startswith("### "):
            out.append(("h2", line[4:].strip()))
        elif re.match(r"^[-*]\s+", line):
            out.append(("bullet", re.sub(r"^[-*]\s+", "", line).strip()))
        else:
            text = re.sub(r"\*\*([^*]+)\*\*", r"\1", line)
            text = re.sub(r"\*([^*]+)\*", r"\1", text)
            out.append(("body", text.strip()))
    return out


def _write_docx(path: Path, lines: list[tuple[str, str]]) -> None:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    for kind, text in lines:
        if kind == "blank":
            doc.add_paragraph("")
        elif kind == "h1":
            p = doc.add_heading(text, level=1)
            for run in p.runs:
                run.font.size = Pt(18)
        elif kind == "h2":
            doc.add_heading(text, level=2)
        elif kind == "bullet":
            doc.add_paragraph(text, style="List Bullet")
        else:
            doc.add_paragraph(text)
    doc.save(str(path))


def _write_pdf(path: Path, lines: list[tuple[str, str]]) -> None:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(15, 15, 15)

    for kind, text in lines:
        # FPDF latin-1 safe
        safe = _latin1(text)
        if kind == "blank":
            pdf.ln(4)
        elif kind == "h1":
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(0, 8, safe)
            pdf.ln(2)
        elif kind == "h2":
            pdf.set_font("Helvetica", "B", 12)
            pdf.ln(2)
            pdf.multi_cell(0, 7, safe)
            pdf.ln(1)
        elif kind == "bullet":
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, f"- {safe}")
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, safe)
    pdf.output(str(path))


def _latin1(text: str) -> str:
    return (text or "").encode("latin-1", "replace").decode("latin-1")
