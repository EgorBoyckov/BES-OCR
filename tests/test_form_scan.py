"""Сквозной тест на скане типового бланка: вёрстка должна совпадать с
оригиналом — единый кегль, выравнивание, переносы строк, колонки, печать."""
import os
from collections import Counter

from docx import Document as DocxDocument

from bes_ocr.config.settings import Settings
from bes_ocr.core.docx_writer import build_docx
from bes_ocr.core.models import Alignment, Heading, ImageBlock, ListItem, Paragraph, Table
from bes_ocr.core.pipeline import process_document


def _all_paragraphs(blocks):
    for b in blocks:
        if isinstance(b, Table):
            for row in b.rows:
                for cell in row.cells:
                    yield from _all_paragraphs(cell.blocks)
        elif isinstance(b, Paragraph):
            yield b


def test_official_form_scan_layout(official_form_scan_pdf, tmp_path):
    document = process_document(official_form_scan_pdf, Settings())
    page = document.pages[0]
    assert page.used_ocr

    # Кегль: весь текст набран 12pt — никаких "скачущих" размеров от слова
    # к слову (исходная жалоба пользователя).
    sizes = Counter()
    for p in _all_paragraphs(page.blocks):
        for r in p.runs:
            sizes[r.size_pt] += len(r.text.strip())
    assert sizes.most_common(1)[0][0] == 12.0
    assert sizes[12.0] / sum(sizes.values()) > 0.95

    text_blocks = [b for b in page.blocks if isinstance(b, Paragraph)]
    header = text_blocks[0]
    assert header.alignment == Alignment.RIGHT
    assert header.text.startswith("Приложение 1\nк договору")

    title = text_blocks[1]
    assert not isinstance(title, Heading)  # тот же кегль, что у текста — не "заголовок" Word
    assert title.alignment == Alignment.CENTER
    assert all(r.bold for r in title.runs if r.text.strip())
    assert not any(r.bold for r in header.runs)

    items = [b for b in page.blocks if isinstance(b, ListItem)]
    assert len(items) == 3 and all(i.ordered for i in items)

    layout = [b for b in page.blocks if isinstance(b, Table) and b.borderless]
    assert len(layout) == 1
    left_cell, right_cell = layout[0].rows[0].cells
    assert "Университет" in left_cell.text and "Профильная" not in left_cell.text
    assert "Профильная организация" in right_cell.text

    stamps = [b for b in page.blocks if isinstance(b, ImageBlock) and b.floating]
    assert stamps, "синяя печать должна стать плавающим изображением"
    all_text = " ".join(p.text for p in _all_paragraphs(page.blocks))
    assert "ПЕЧАТЬ" not in all_text  # текст печати не попал в поток OCR

    out = os.path.join(tmp_path, "form.docx")
    build_docx(document, out)
    reopened = DocxDocument(out)
    assert reopened.element.body.xpath(".//wp:anchor")
    fonts = {r.font.name for p in reopened.paragraphs for r in p.runs if r.text.strip()}
    assert fonts == {"Times New Roman"}
