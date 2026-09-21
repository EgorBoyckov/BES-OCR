"""Обнаружение и восстановление таблиц.

Два независимых пути, оба дают единую модель `Table`/`TableCell` с
row_span/col_span для объединённых ячеек:

* `detect_tables_pdfplumber` — для страниц с пригодным текстовым слоем:
  границы ячеек и точный текст берутся напрямую из PDF.
* `detect_tables_img2table` — для сканов: обнаружение сетки и OCR ячеек
  через библиотеку img2table (см. ниже, почему не собственная реализация
  на OpenCV).

## Почему img2table, а не собственный детектор на OpenCV

Первая версия этого модуля строила сетку таблицы сама (морфология +
преобразование Хафа для устойчивости к перекосу скана). На синтетических
тестовых PDF это работало, но на реальном сканированном документе
(скреплённый/сшитый бланк, типичная лёгкая "волнистость" строк от кривизны
разворота при сканировании) давало катастрофически неверный результат:
таблицы либо не находились вовсе, либо соседние строки ошибочно
склеивались в одну ячейку, смешивая текст разных студентов/полей в одну
кашу — то есть именно то, что проект должен был предотвратить в первую
очередь (см. CLAUDE.md: таблицы — главный приоритет).

img2table (MIT, github.com/xavctn/img2table) — зрелая, специально для этой
задачи написанная библиотека: определение сетки по линиям и по
выравниванию (в т.ч. безграничных таблиц), устойчивое сопоставление
OCR-текста ячейкам, поддержка объединённых ячеек "из коробки", работает
локально на CPU (не требует нейросетей/GPU — совместимо с офлайн-требованием
проекта и Astra Linux), взаимодействует с тем же Tesseract. Проверка на
реальном документе показала кардинально более точный результат (корректная
сетка вместо развала таблицы) и заметно быстрее собственной реализации.
Итог: не имеет смысла поддерживать свой менее надёжный детектор, когда
специализированная библиотека решает эту же задачу лучше — заменяем
полностью, а не оставляем как fallback.
"""
from __future__ import annotations

import logging

import pdfplumber

from ..config.settings import Settings
from .models import BBox, Paragraph, Run, Table, TableCell, TableRow

logger = logging.getLogger("bes_ocr")


def _round_bbox(bbox, ndigits: int = 1):
    return tuple(round(v, ndigits) for v in bbox)


def detect_tables_pdfplumber(pdf_path: str, page_number: int) -> list[Table]:
    """page_number — 0-based индекс страницы."""
    tables: list[Table] = []
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[page_number]
        found = page.find_tables(
            table_settings={
                "vertical_strategy": "lines_strict",
                "horizontal_strategy": "lines_strict",
                "snap_tolerance": 3,
                "join_tolerance": 3,
            }
        )
        if not found:
            found = page.find_tables()  # запасная эвристика по выравниванию текста

        for pt in found:
            grid_bbox = [[None] * len(row.cells) for row in pt.rows]
            for r, row in enumerate(pt.rows):
                for c, cell_bbox in enumerate(row.cells):
                    grid_bbox[r][c] = _round_bbox(cell_bbox) if cell_bbox else None

            text_matrix = pt.extract()
            n_rows = len(grid_bbox)
            n_cols = max((len(r) for r in grid_bbox), default=0)

            # pdfplumber отмечает ячейки, поглощённые объединением, значением
            # None (а не повторением bbox родительской ячейки). Восстанавливаем
            # принадлежность: сначала пробуем ячейку слева (горизонтальное
            # объединение), затем ячейку сверху (вертикальное), иначе — это
            # самостоятельная пустая ячейка.
            key_grid: list[list[tuple]] = [[None] * len(grid_bbox[r]) for r in range(n_rows)]
            for r in range(n_rows):
                for c in range(len(grid_bbox[r])):
                    bbox = grid_bbox[r][c]
                    if bbox is not None:
                        key_grid[r][c] = bbox
                    elif c > 0 and key_grid[r][c - 1] is not None:
                        key_grid[r][c] = key_grid[r][c - 1]
                    elif r > 0 and c < len(key_grid[r - 1]) and key_grid[r - 1][c] is not None:
                        key_grid[r][c] = key_grid[r - 1][c]
                    else:
                        key_grid[r][c] = (f"solo-{r}-{c}",)

            groups: dict[tuple, list[tuple[int, int]]] = {}
            for r in range(n_rows):
                for c in range(len(grid_bbox[r])):
                    groups.setdefault(key_grid[r][c], []).append((r, c))

            cell_owner: dict[tuple[int, int], tuple[int, int]] = {}
            spans: dict[tuple[int, int], tuple[int, int]] = {}
            for key, positions in groups.items():
                positions.sort()
                top_left = positions[0]
                rows_covered = sorted({p[0] for p in positions})
                cols_covered = sorted({p[1] for p in positions})
                spans[top_left] = (len(rows_covered), len(cols_covered))
                for p in positions:
                    cell_owner[p] = top_left

            table = Table(bbox=BBox(*pt.bbox), source="text")
            for r in range(n_rows):
                row_obj = TableRow()
                for c in range(len(grid_bbox[r])):
                    owner = cell_owner.get((r, c), (r, c))
                    if owner != (r, c):
                        row_obj.cells.append(TableCell(is_merge_continuation=True, row_span=0, col_span=0))
                        continue
                    row_span, col_span = spans.get((r, c), (1, 1))
                    text = ""
                    if r < len(text_matrix) and c < len(text_matrix[r]):
                        text = (text_matrix[r][c] or "").strip()
                    para = Paragraph(runs=[Run(text=text)]) if text else Paragraph(runs=[Run(text="")])
                    row_obj.cells.append(TableCell(blocks=[para], row_span=row_span, col_span=col_span))
                table.rows.append(row_obj)
            tables.append(table)
    return tables


def detect_tables_img2table(image_bgr, settings: Settings, px_to_pt: float) -> list[Table]:
    """Обнаруживает таблицы на растре скана через img2table + Tesseract.

    image_bgr — уже отрендеренная страница (см. page_processor), px_to_pt —
    коэффициент перевода пиксельных координат рендера в точки PDF (для
    единообразия с координатами слов текстового слоя/OCR на этой же
    странице, см. page_processor._filter_words_outside_tables).
    """
    import cv2

    try:
        from img2table.document import Image as Img2TableImage
        from img2table.ocr import TesseractOCR
    except ImportError:
        logger.warning("img2table не установлен — таблицы на сканах не будут обнаружены")
        return []

    ok, buf = cv2.imencode(".png", image_bgr)
    if not ok:
        return []

    ocr = TesseractOCR(lang=settings.ocr_languages)
    doc = Img2TableImage(src=buf.tobytes(), detect_rotation=True)
    try:
        extracted = doc.extract_tables(
            ocr=ocr, implicit_rows=False, borderless_tables=False, min_confidence=50
        )
    except Exception as exc:  # noqa: BLE001 - библиотека может кидать разные типы на "мусорных" страницах
        logger.warning("img2table: не удалось обработать страницу (%s)", exc)
        return []

    tables: list[Table] = []
    for et in extracted:
        table = _convert_img2table(et, px_to_pt)
        if table is not None:
            tables.append(table)
    return tables


def _convert_img2table(extracted_table, px_to_pt: float) -> Table | None:
    rows = list(extracted_table.content.values())
    if not rows:
        return None

    # img2table представляет объединённую ячейку повтором ОДНОГО И ТОГО ЖЕ
    # bbox на всех позициях сетки (по строкам и/или столбцам), которые она
    # покрывает — группируем по bbox, как и для pdfplumber выше.
    key_grid: list[list[tuple]] = [
        [(c.bbox.x1, c.bbox.y1, c.bbox.x2, c.bbox.y2) for c in row] for row in rows
    ]

    groups: dict[tuple, list[tuple[int, int]]] = {}
    for r, row_keys in enumerate(key_grid):
        for c, key in enumerate(row_keys):
            groups.setdefault(key, []).append((r, c))

    cell_owner: dict[tuple[int, int], tuple[int, int]] = {}
    spans: dict[tuple[int, int], tuple[int, int]] = {}
    for key, positions in groups.items():
        positions.sort()
        top_left = positions[0]
        rows_covered = sorted({p[0] for p in positions})
        cols_covered = sorted({p[1] for p in positions})
        spans[top_left] = (len(rows_covered), len(cols_covered))
        for p in positions:
            cell_owner[p] = top_left

    b = extracted_table.bbox
    table = Table(
        bbox=BBox(b.x1 * px_to_pt, b.y1 * px_to_pt, b.x2 * px_to_pt, b.y2 * px_to_pt),
        source="ocr",
    )
    for r, row in enumerate(rows):
        row_obj = TableRow()
        for c, cell in enumerate(row):
            owner = cell_owner.get((r, c), (r, c))
            if owner != (r, c):
                row_obj.cells.append(TableCell(is_merge_continuation=True, row_span=0, col_span=0))
                continue
            row_span, col_span = spans.get((r, c), (1, 1))
            text = (cell.value or "").strip()
            para = Paragraph(runs=[Run(text=text)])
            cb = cell.bbox
            row_obj.cells.append(
                TableCell(
                    blocks=[para],
                    row_span=row_span,
                    col_span=col_span,
                    bbox=BBox(cb.x1 * px_to_pt, cb.y1 * px_to_pt, cb.x2 * px_to_pt, cb.y2 * px_to_pt),
                )
            )
        table.rows.append(row_obj)
    return table
