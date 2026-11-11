"""Turns email attachments into something the AI can read.

- PDF with text   -> text pulled out on your computer (free), with page markers
- Scanned PDF     -> sent to the AI as a file so it can "see" the pages
- Images          -> sent to the AI as images
- Word / Excel / CSV / TXT -> text pulled out on your computer
- Anything else   -> skipped (and reported)
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from .gmail_fetch import Attachment

IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MIN_CHARS_PER_PAGE = 60   # less than this per page = probably a scan


@dataclass
class ReadResult:
    texts: list[tuple[str, str]] = field(default_factory=list)          # (filename, text)
    files: list[tuple[str, str, bytes]] = field(default_factory=list)   # (filename, mime, bytes) for the AI to view
    skipped: list[str] = field(default_factory=list)


def read_attachments(atts: list[Attachment]) -> ReadResult:
    res = ReadResult()
    for a in atts:
        name = a.filename
        low = name.lower()
        mime = (a.mime or "").lower()
        try:
            if low.endswith(".pdf") or mime == "application/pdf":
                text, pages = _pdf_text(a.data)
                if pages and len(text.strip()) >= MIN_CHARS_PER_PAGE * pages:
                    res.texts.append((name, text))
                else:
                    res.files.append((name, "application/pdf", a.data))
            elif mime in IMAGE_TYPES or low.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                res.files.append((name, _image_mime(low, mime), a.data))
            elif low.endswith(".docx"):
                res.texts.append((name, _docx_text(a.data)))
            elif low.endswith((".xlsx", ".xlsm")):
                res.texts.append((name, _xlsx_text(a.data)))
            elif low.endswith(".csv") or mime == "text/csv":
                res.texts.append((name, _csv_text(a.data)))
            elif low.endswith(".txt") or mime == "text/plain":
                res.texts.append((name, a.data.decode("utf-8", errors="replace")))
            else:
                res.skipped.append(name)
        except Exception as e:  # a broken file should never stop the bot
            res.skipped.append(f"{name} (could not open: {e.__class__.__name__})")
    return res


def _pdf_text(data: bytes) -> tuple[str, int]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    out = []
    for i, page in enumerate(reader.pages, start=1):
        out.append(f"--- page {i} ---\n{page.extract_text() or ''}")
    return "\n".join(out), len(reader.pages)


def _docx_text(data: bytes) -> str:
    import docx

    d = docx.Document(io.BytesIO(data))
    lines = [p.text for p in d.paragraphs if p.text.strip()]
    for table in d.tables:
        for row in table.rows:
            lines.append(" | ".join(c.text.strip() for c in row.cells))
    return "\n".join(lines)


def _xlsx_text(data: bytes) -> str:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    lines = []
    for ws in wb.worksheets:
        lines.append(f"--- sheet {ws.title} ---")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else str(v) for v in row]
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines)


def _csv_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return "\n".join(" | ".join(r) for r in csv.reader(io.StringIO(text)))


def _image_mime(name: str, mime: str) -> str:
    if mime in IMAGE_TYPES:
        return mime
    if name.endswith(".png"):
        return "image/png"
    if name.endswith(".webp"):
        return "image/webp"
    if name.endswith(".gif"):
        return "image/gif"
    return "image/jpeg"
