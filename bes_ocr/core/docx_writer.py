"""Генерация .docx из внутренней модели Document.

Не знает ничего о PDF или OCR — только о модели `core.models`. Ключевое
требование проекта: таблицы должны быть настоящими редактируемыми таблицами
Word (`table.cell(...).merge(...)`), а не изображениями.
"""
from __future__ import annotations

import io
import logging

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from PIL import Image as PILImage

from .errors import DocxWriteError
from .models import (
    Alignment,
    Document,
    Heading,
    ImageBlock,
    ListItem,
    Paragraph,
    Table,
)

logger = logging.getLogger("bes_ocr")

_ALIGN_MAP = {
    Alignment.LEFT: WD_ALIGN_PARAGRAPH.LEFT,
    Alignment.CENTER: WD_ALIGN_PARAGRAPH.CENTER,
    Alignment.RIGHT: WD_ALIGN_PARAGRAPH.RIGHT,
    Alignment.JUSTIFY: WD_ALIGN_PARAGRAPH.JUSTIFY,
}

PAGE_WIDTH_USABLE_IN = 6.3  # A4 минус стандартные поля


def _add_field(paragraph, field_code: str) -> None:
    run = paragraph.add_run()
    fld_begin = run._r.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "begin"})
    instr = run._r.makeelement(qn("w:instrText"), {})
    instr.set(qn("xml:space"), "preserve")
    instr.text = field_code
    fld_end = run._r.makeelement(qn("w:fldChar"), {qn("w:fldCharType"): "end"})
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_end)


def _write_paragraph(doc: DocxDocument, block: Paragraph, style: str | None = None):
    p = doc.add_paragraph(style=style)
    p.alignment = _ALIGN_MAP.get(block.alignment, WD_ALIGN_PARAGRAPH.LEFT)
    if block.indent_pt:
        p.paragraph_format.left_indent = Pt(min(block.indent_pt, 300))
    for run_data in block.runs:
        if not run_data.text:
            continue
        r = p.add_run(run_data.text)
        r.bold = run_data.bold
        r.italic = run_data.italic
        r.font.size = Pt(max(6.0, min(run_data.size_pt, 48.0)))
    return p


def _write_table(doc: DocxDocument, table: Table) -> None:
    n_rows = table.n_rows
    n_cols = table.n_cols
    if n_rows == 0 or n_cols == 0:
        return

    docx_table = doc.add_table(rows=n_rows, cols=n_cols)
    docx_table.style = "Table Grid"

    merges: list[tuple[int, int, int, int]] = []
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            if cell.is_merge_continuation:
                continue
            if cell.row_span > 1 or cell.col_span > 1:
                merges.append((r, c, r + cell.row_span - 1, c + cell.col_span - 1))
            docx_cell = docx_table.cell(r, c)
            docx_cell.text = ""
            para = docx_cell.paragraphs[0]
            text = cell.text
            if text:
                para.add_run(text)

    for r0, c0, r1, c1 in merges:
        try:
            docx_table.cell(r0, c0).merge(docx_table.cell(r1, c1))
        except IndexError:
            logger.warning("Не удалось объединить ячейки таблицы (%d,%d)-(%d,%d)", r0, c0, r1, c1)

    doc.add_paragraph()  # визуальный отступ после таблицы


def _write_image(doc: DocxDocument, block: ImageBlock) -> None:
    try:
        pil_img = PILImage.open(io.BytesIO(block.data))
        width_px, height_px = pil_img.size
    except Exception:  # noqa: BLE001
        width_px, height_px = block.width_px or 100, block.height_px or 100

    aspect = height_px / width_px if width_px else 1.0
    width_in = PAGE_WIDTH_USABLE_IN
    stream = io.BytesIO(block.data)
    try:
        doc.add_picture(stream, width=Inches(width_in))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Не удалось вставить изображение: %s", exc)
        return
    if block.caption:
        cap = doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = cap.add_run(block.caption)
        run.italic = True
        run.font.size = Pt(9)


def build_docx(document: Document, output_path: str) -> None:
    try:
        doc = DocxDocument()

        if document.footer and document.footer.text:
            section = doc.sections[0]
            footer_p = section.footer.paragraphs[0]
            footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            footer_p.add_run(document.footer.text + "  ")
            _add_field(footer_p, "PAGE")

        if document.header and document.header.text:
            section = doc.sections[0]
            header_p = section.header.paragraphs[0]
            header_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            header_p.add_run(document.header.text)

        for page_index, page in enumerate(document.pages):
            for block in page.blocks:
                if isinstance(block, Heading):
                    style = f"Heading {min(max(block.level, 1), 4)}"
                    _write_paragraph(doc, block, style=style)
                elif isinstance(block, ListItem):
                    style = "List Number" if block.ordered else "List Bullet"
                    _write_paragraph(doc, block, style=style)
                elif isinstance(block, Table):
                    _write_table(doc, block)
                elif isinstance(block, ImageBlock):
                    _write_image(doc, block)
                elif isinstance(block, Paragraph):
                    if block.text.strip():
                        _write_paragraph(doc, block)

            if page_index < len(document.pages) - 1:
                doc.add_page_break()

        doc.save(output_path)
    except Exception as exc:  # noqa: BLE001
        raise DocxWriteError(f"Не удалось сохранить DOCX: {exc}") from exc
