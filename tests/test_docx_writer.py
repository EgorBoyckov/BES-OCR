import os

from docx import Document as DocxDocument

from bes_ocr.core.docx_writer import build_docx
from bes_ocr.core.models import (
    Document,
    Heading,
    ImageBlock,
    Page,
    Paragraph,
    Run,
    Table,
    TableCell,
    TableRow,
)


def _sample_document() -> Document:
    doc = Document()
    page = Page(number=1)
    page.blocks.append(Heading(runs=[Run(text="Title")], level=1))
    page.blocks.append(Paragraph(runs=[Run(text="Hello world")]))

    table = Table()
    row0 = TableRow(cells=[TableCell(blocks=[Paragraph(runs=[Run(text="Merged")])], row_span=1, col_span=2), TableCell(is_merge_continuation=True, row_span=0, col_span=0)])
    row1 = TableRow(cells=[TableCell(blocks=[Paragraph(runs=[Run(text="A")])]), TableCell(blocks=[Paragraph(runs=[Run(text="B")])])])
    table.rows = [row0, row1]
    page.blocks.append(table)

    doc.pages.append(page)
    return doc


def test_build_docx_creates_valid_file(tmp_path):
    document = _sample_document()
    out_path = os.path.join(tmp_path, "out.docx")
    build_docx(document, out_path)
    assert os.path.isfile(out_path)

    reopened = DocxDocument(out_path)
    assert any(p.text == "Title" and p.style.name.startswith("Heading") for p in reopened.paragraphs)
    assert any(p.text == "Hello world" for p in reopened.paragraphs)
    assert len(reopened.tables) == 1
    t = reopened.tables[0]
    assert len(t.rows) == 2
    assert t.cell(0, 0).text == "Merged"
    assert t.cell(0, 1).text == "Merged"  # объединённая ячейка
    assert t.cell(1, 0).text == "A"
    assert t.cell(1, 1).text == "B"


def test_build_docx_with_image(tmp_path):
    from PIL import Image
    import io

    img = Image.new("RGB", (50, 50), "red")
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    document = Document()
    page = Page(number=1)
    page.blocks.append(ImageBlock(data=buf.getvalue(), width_px=50, height_px=50, caption="A caption"))
    document.pages.append(page)

    out_path = os.path.join(tmp_path, "img.docx")
    build_docx(document, out_path)

    reopened = DocxDocument(out_path)
    assert len(reopened.inline_shapes) == 1
