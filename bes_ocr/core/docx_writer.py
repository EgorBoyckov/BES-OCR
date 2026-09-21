"""Генерация .docx из внутренней модели Document.

Не знает ничего о PDF или OCR — только о модели `core.models`. Ключевое
требование проекта: таблицы должны быть настоящими редактируемыми таблицами
Word (`table.cell(...).merge(...)`), а не изображениями.

Для визуального сходства с оригиналом (п.20 ТЗ) страница DOCX должна иметь
тот же размер и поля, что и исходный PDF — иначе весь перенос строк,
пропорции таблиц и отступы систематически расходятся с оригиналом, даже
если каждый блок распознан правильно по отдельности. См. `_compute_page_geometry`.
"""
from __future__ import annotations

import io
import logging
import statistics

from docx import Document as DocxDocument
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt
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

# Запасные поля (ГОСТ 7.32 — стандартные для русскоязычных официальных
# документов), используются только когда их не удалось оценить по
# содержимому конкретного документа.
_FALLBACK_MARGIN_LEFT_PT = 85.0
_FALLBACK_MARGIN_RIGHT_PT = 28.0
_FALLBACK_MARGIN_TOP_PT = 57.0
_FALLBACK_MARGIN_BOTTOM_PT = 57.0
_DEFAULT_PAGE_WIDTH_PT = 595.0
_DEFAULT_PAGE_HEIGHT_PT = 842.0


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


class PageGeometry:
    def __init__(self, width_pt: float, height_pt: float, margin_left_pt: float, margin_right_pt: float,
                 margin_top_pt: float, margin_bottom_pt: float):
        self.width_pt = width_pt
        self.height_pt = height_pt
        self.margin_left_pt = margin_left_pt
        self.margin_right_pt = margin_right_pt
        self.margin_top_pt = margin_top_pt
        self.margin_bottom_pt = margin_bottom_pt

    @property
    def usable_width_pt(self) -> float:
        return max(72.0, self.width_pt - self.margin_left_pt - self.margin_right_pt)


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def _compute_page_geometry(document: Document) -> PageGeometry:
    """Определяет размер страницы и поля DOCX-документа по исходному PDF.

    Без этого DOCX получает поля/размер страницы по умолчанию (обычно
    Letter, 1 дюйм со всех сторон) независимо от реального документа — из-за
    этого систематически расходятся перенос строк, пропорции таблиц и
    отступы, даже если каждый блок распознан правильно по отдельности.
    Поля оцениваются по фактическому содержимому (10-й/90-й процентиль
    левых/правых краёв текстовых блоков), а не берутся "как есть" —
    единичный выброс (например, широкая таблица у самого края) не должен
    задавать поля для всей страницы.
    """
    if not document.pages:
        return PageGeometry(
            _DEFAULT_PAGE_WIDTH_PT, _DEFAULT_PAGE_HEIGHT_PT,
            _FALLBACK_MARGIN_LEFT_PT, _FALLBACK_MARGIN_RIGHT_PT,
            _FALLBACK_MARGIN_TOP_PT, _FALLBACK_MARGIN_BOTTOM_PT,
        )

    width_pt = statistics.median(p.width_pt for p in document.pages) or _DEFAULT_PAGE_WIDTH_PT
    height_pt = statistics.median(p.height_pt for p in document.pages) or _DEFAULT_PAGE_HEIGHT_PT

    lefts: list[float] = []
    rights: list[float] = []
    tops: list[float] = []
    bottoms: list[float] = []
    for page in document.pages:
        text_blocks = [b for b in page.blocks if isinstance(b, (Paragraph, Heading, ListItem)) and b.bbox]
        if text_blocks:
            tops.append(min(b.bbox.y0 for b in text_blocks))
            bottoms.append(max(b.bbox.y1 for b in text_blocks))
        for b in text_blocks:
            lefts.append(b.bbox.x0)
            rights.append(b.bbox.x1)

    margin_left = _percentile(lefts, 0.1) if lefts else _FALLBACK_MARGIN_LEFT_PT
    margin_right = (width_pt - _percentile(rights, 0.9)) if rights else _FALLBACK_MARGIN_RIGHT_PT
    margin_top = min(tops) if tops else _FALLBACK_MARGIN_TOP_PT
    margin_bottom = (height_pt - max(bottoms)) if bottoms else _FALLBACK_MARGIN_BOTTOM_PT

    def clamp(v: float, lo: float, hi: float, fallback: float) -> float:
        return v if lo <= v <= hi else fallback

    return PageGeometry(
        width_pt=width_pt,
        height_pt=height_pt,
        margin_left_pt=clamp(margin_left, 14.0, 140.0, _FALLBACK_MARGIN_LEFT_PT),
        margin_right_pt=clamp(margin_right, 14.0, 140.0, _FALLBACK_MARGIN_RIGHT_PT),
        margin_top_pt=clamp(margin_top, 10.0, 120.0, _FALLBACK_MARGIN_TOP_PT),
        margin_bottom_pt=clamp(margin_bottom, 10.0, 120.0, _FALLBACK_MARGIN_BOTTOM_PT),
    )


def _write_paragraph(doc: DocxDocument, block: Paragraph, geometry: PageGeometry, style: str | None = None):
    p = doc.add_paragraph(style=style)
    p.alignment = _ALIGN_MAP.get(block.alignment, WD_ALIGN_PARAGRAPH.LEFT)
    # indent_pt — абсолютная координата на исходной странице; поле left_indent
    # в DOCX откладывается ДОПОЛНИТЕЛЬНО от margin_left секции, поэтому нужна
    # именно разница между отступом блока и обычным полем, а не сырое
    # значение — иначе типичный абзац съезжает на всю ширину поля вправо ещё
    # раз поверх уже выставленного margin_left.
    extra_indent = block.indent_pt - geometry.margin_left_pt
    if extra_indent > 3.0:
        p.paragraph_format.left_indent = Pt(min(extra_indent, geometry.usable_width_pt * 0.6))
    for run_data in block.runs:
        if not run_data.text:
            continue
        r = p.add_run(run_data.text)
        r.bold = run_data.bold
        r.italic = run_data.italic
        r.font.size = Pt(max(6.0, min(run_data.size_pt, 48.0)))
    return p


def _write_table(doc: DocxDocument, table: Table, geometry: PageGeometry) -> None:
    n_rows = table.n_rows
    n_cols = table.n_cols
    if n_rows == 0 or n_cols == 0:
        return

    docx_table = doc.add_table(rows=n_rows, cols=n_cols)
    docx_table.style = "Table Grid"
    docx_table.autofit = False

    col_widths_pt = _fitted_column_widths(table.col_widths_pt, n_cols, geometry.usable_width_pt)

    merges: list[tuple[int, int, int, int]] = []
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells):
            if cell.is_merge_continuation:
                continue
            if cell.row_span > 1 or cell.col_span > 1:
                merges.append((r, c, r + cell.row_span - 1, c + cell.col_span - 1))
            docx_cell = docx_table.cell(r, c)
            docx_cell.text = ""
            docx_cell.width = Pt(col_widths_pt[c])
            para = docx_cell.paragraphs[0]
            text = cell.text
            if text:
                para.add_run(text)

    for r0, c0, r1, c1 in merges:
        try:
            docx_table.cell(r0, c0).merge(docx_table.cell(r1, c1))
        except IndexError:
            logger.warning("Не удалось объединить ячейки таблицы (%d,%d)-(%d,%d)", r0, c0, r1, c1)

    for c in range(n_cols):
        docx_table.columns[c].width = Pt(col_widths_pt[c])

    doc.add_paragraph()  # визуальный отступ после таблицы


def _fitted_column_widths(col_widths_pt: list[float], n_cols: int, usable_width_pt: float) -> list[float]:
    """Пропорционально вписывает известные ширины столбцов в печатную
    область страницы (после того как страница DOCX приведена к размеру
    оригинала, они обычно и так укладываются, но подстраховываемся)."""
    if not col_widths_pt or len(col_widths_pt) != n_cols:
        even = usable_width_pt / n_cols
        return [even] * n_cols
    total = sum(col_widths_pt) or 1.0
    if total <= usable_width_pt:
        return list(col_widths_pt)
    scale = usable_width_pt / total
    return [w * scale for w in col_widths_pt]


def _write_image(doc: DocxDocument, block: ImageBlock, geometry: PageGeometry) -> None:
    try:
        pil_img = PILImage.open(io.BytesIO(block.data))
        width_px, height_px = pil_img.size
    except Exception:  # noqa: BLE001
        width_px, height_px = block.width_px or 100, block.height_px or 100

    # Реальный размер изображения на исходной странице (если известен из
    # bbox) — не растягиваем логотип/печать на всю ширину страницы только
    # потому, что так проще; иначе маленькая картинка становится нелепо
    # большой и разваливает окружающую вёрстку.
    if block.bbox is not None and (block.bbox.x1 - block.bbox.x0) > 1:
        width_pt = min(block.bbox.x1 - block.bbox.x0, geometry.usable_width_pt)
    else:
        px_per_pt = 96.0 / 72.0  # обычный экранный масштаб как разумная оценка при отсутствии bbox
        width_pt = min(width_px / px_per_pt, geometry.usable_width_pt)
    width_pt = max(width_pt, 36.0)

    stream = io.BytesIO(block.data)
    try:
        # width= достаточно: python-docx сохраняет пропорции изображения
        # автоматически, когда передан только один из width/height.
        doc.add_picture(stream, width=Pt(width_pt))
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
        geometry = _compute_page_geometry(document)

        section = doc.sections[0]
        section.page_width = Pt(geometry.width_pt)
        section.page_height = Pt(geometry.height_pt)
        section.left_margin = Pt(geometry.margin_left_pt)
        section.right_margin = Pt(geometry.margin_right_pt)
        section.top_margin = Pt(geometry.margin_top_pt)
        section.bottom_margin = Pt(geometry.margin_bottom_pt)

        if document.footer and document.footer.text:
            footer_p = section.footer.paragraphs[0]
            footer_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            footer_p.add_run(document.footer.text + "  ")
            _add_field(footer_p, "PAGE")

        if document.header and document.header.text:
            header_p = section.header.paragraphs[0]
            header_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            header_p.add_run(document.header.text)

        for page_index, page in enumerate(document.pages):
            for block in page.blocks:
                if isinstance(block, Heading):
                    style = f"Heading {min(max(block.level, 1), 4)}"
                    _write_paragraph(doc, block, geometry, style=style)
                elif isinstance(block, ListItem):
                    style = "List Number" if block.ordered else "List Bullet"
                    _write_paragraph(doc, block, geometry, style=style)
                elif isinstance(block, Table):
                    _write_table(doc, block, geometry)
                elif isinstance(block, ImageBlock):
                    _write_image(doc, block, geometry)
                elif isinstance(block, Paragraph):
                    if block.text.strip():
                        _write_paragraph(doc, block, geometry)

            if page_index < len(document.pages) - 1:
                doc.add_page_break()

        doc.save(output_path)
    except Exception as exc:  # noqa: BLE001
        raise DocxWriteError(f"Не удалось сохранить DOCX: {exc}") from exc
