from collections import OrderedDict

from bes_ocr.core.table_detection import (
    _convert_img2table,
    detect_tables_pdfplumber,
)


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


def _fake_extracted_table():
    """Собирает объект в форме img2table.ExtractedTable: 3x3 таблица, где
    верхняя строка объединена по всей ширине (img2table отмечает объединение
    повтором ОДНОГО И ТОГО ЖЕ bbox на всех позициях сетки, которые оно
    покрывает — ровно так же представляет объединения pdfplumber, см.
    detect_tables_pdfplumber выше).
    """
    from img2table.tables.extraction import BBox, ExtractedTable, TableCell

    header_bbox = BBox(0, 0, 300, 40)
    content = OrderedDict()
    content[0] = [
        TableCell(bbox=header_bbox, value="Заголовок таблицы"),
        TableCell(bbox=header_bbox, value="Заголовок таблицы"),
        TableCell(bbox=header_bbox, value="Заголовок таблицы"),
    ]
    content[1] = [
        TableCell(bbox=BBox(0, 40, 100, 80), value="Один"),
        TableCell(bbox=BBox(100, 40, 200, 80), value="Два"),
        TableCell(bbox=BBox(200, 40, 300, 80), value="Три"),
    ]
    content[2] = [
        TableCell(bbox=BBox(0, 80, 100, 120), value="Four"),
        TableCell(bbox=BBox(100, 80, 200, 120), value="Five"),
        TableCell(bbox=BBox(200, 80, 300, 120), value="Six"),
    ]
    return ExtractedTable(bbox=BBox(0, 0, 300, 120), title=None, content=content)


def test_convert_img2table_merged_header():
    """Юнит-тест конвертации img2table -> внутренняя модель Table: именно
    этот код (группировка ячеек по bbox в объединения, перевод пикселей в
    точки PDF) написан в проекте и должен тестироваться напрямую, а не
    через попытку заставить эвристики самой библиотеки img2table сработать
    строго определённым образом на синтетическом фикстурном изображении —
    качество самой детекции img2table подтверждено на реальном
    отсканированном документе (см. docs/RESEARCH.md), это внешняя,
    отдельно поддерживаемая и протестированная библиотека.
    """
    table = _convert_img2table(_fake_extracted_table(), px_to_pt=1.0)
    assert table is not None
    assert table.n_rows == 3
    assert table.n_cols == 3

    header_cells = table.rows[0].cells
    merged = [c for c in header_cells if c.is_merge_continuation]
    assert len(merged) == 2
    owner = [c for c in header_cells if not c.is_merge_continuation][0]
    assert owner.col_span == 3
    assert owner.text == "Заголовок таблицы"

    assert all(not c.is_merge_continuation for c in table.rows[1].cells)
    assert [c.text for c in table.rows[1].cells] == ["Один", "Два", "Три"]
    assert [c.text for c in table.rows[2].cells] == ["Four", "Five", "Six"]


def test_convert_img2table_empty_content_returns_none():
    from img2table.tables.extraction import BBox, ExtractedTable

    et = ExtractedTable(bbox=BBox(0, 0, 10, 10), title=None, content=OrderedDict())
    assert _convert_img2table(et, px_to_pt=1.0) is None
