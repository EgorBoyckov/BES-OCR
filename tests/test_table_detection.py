from bes_ocr.config.settings import Settings
from bes_ocr.core.ocr_engine import OcrEngine
from bes_ocr.core.pdf_source import PdfSource
from bes_ocr.core.table_detection import detect_tables_opencv, detect_tables_pdfplumber


def test_pdfplumber_simple_table(text_pdf):
    tables = detect_tables_pdfplumber(text_pdf, 0)
    assert len(tables) == 1
    t = tables[0]
    assert t.n_rows == 3
    assert t.n_cols == 3
    assert t.rows[0].cells[0].text == "A1"
    assert t.rows[2].cells[2].text == "C3"


def test_pdfplumber_merged_header(table_merged_cells_pdf):
    tables = detect_tables_pdfplumber(table_merged_cells_pdf, 0)
    assert len(tables) == 1
    t = tables[0]
    header_cells = t.rows[0].cells
    merged = [c for c in header_cells if c.is_merge_continuation]
    assert len(merged) >= 1
    owner = [c for c in header_cells if not c.is_merge_continuation][0]
    assert owner.col_span >= 2


def test_pdfplumber_multipage_table(multipage_table_pdf):
    t1 = detect_tables_pdfplumber(multipage_table_pdf, 0)
    t2 = detect_tables_pdfplumber(multipage_table_pdf, 1)
    assert len(t1) == 1 and len(t2) == 1
    assert t1[0].n_rows == 8
    assert t2[0].n_rows == 8


def test_opencv_table_with_merged_cells(russian_scanned_pdf):
    settings = Settings()
    engine = OcrEngine(settings)
    with PdfSource(russian_scanned_pdf) as pdf:
        image, _ = pdf.render_page_image(0, dpi=settings.render_dpi)
    tables = detect_tables_opencv(image, engine, settings)
    assert len(tables) == 1
    t = tables[0]
    assert t.n_rows == 3
    assert t.n_cols == 3
    header_cells = t.rows[0].cells
    merged = [c for c in header_cells if c.is_merge_continuation]
    assert len(merged) == 2
    owner = [c for c in header_cells if not c.is_merge_continuation][0]
    assert owner.col_span == 3
    # вторая строка не объединена
    assert all(not c.is_merge_continuation for c in t.rows[1].cells)
