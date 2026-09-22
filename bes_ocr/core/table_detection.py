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
import re
import statistics
from typing import Optional

import pdfplumber

from ..config.settings import Settings
from .layout_analysis import LayoutWord, build_cell_blocks, words_to_lines
from .models import BBox, Paragraph, Run, Table, TableCell, TableRow

logger = logging.getLogger("bes_ocr")


def _round_bbox(bbox, ndigits: int = 1):
    return tuple(round(v, ndigits) for v in bbox)


def detect_tables_pdfplumber(pdf_path: str, page_number: int, font_size_pt: float = 9.0) -> list[Table]:
    """page_number — 0-based индекс страницы.

    font_size_pt — размер шрифта тела документа (медиана по словам страницы,
    см. page_processor), применяется к тексту ячеек таблицы. Без этого текст
    ячеек получал зашитый по умолчанию в модели `Run.size_pt` (11pt), почти
    всегда крупнее настоящего шрифта плотных таблиц бланков — из-за этого
    ширина столбцов (взятая из реальной геометрии PDF/скана) не вмещала
    текст на той же высоте строки, что и в оригинале, и таблица переносила
    гораздо больше строк, раздувая документ на лишние страницы (измерено на
    реальном документе: 2 страницы оригинала → 3 страницы результата)."""
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
                    para = Paragraph(runs=[Run(text=text, size_pt=font_size_pt)])
                    cell_bbox = _merged_cell_bbox(grid_bbox, r, c, row_span, col_span)
                    row_obj.cells.append(
                        TableCell(blocks=[para], row_span=row_span, col_span=col_span, bbox=cell_bbox)
                    )
                row_bbox = getattr(pt.rows[r], "bbox", None)
                if row_bbox:
                    row_obj.height_pt = row_bbox[3] - row_bbox[1]
                table.rows.append(row_obj)
            table.col_widths_pt = _column_widths_from_spans(grid_bbox, spans, n_cols)
            tables.append(table)
    return tables


def _merged_cell_bbox(grid_bbox, r: int, c: int, row_span: int, col_span: int) -> BBox | None:
    boxes = [
        grid_bbox[rr][cc]
        for rr in range(r, min(r + row_span, len(grid_bbox)))
        for cc in range(c, min(c + col_span, len(grid_bbox[rr])))
        if grid_bbox[rr][cc] is not None
    ]
    if not boxes:
        return None
    return BBox(min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def _column_widths_from_spans(
    grid_bbox: list[list[tuple | None]], spans: dict[tuple[int, int], tuple[int, int]], n_cols: int
) -> list[float]:
    """Ширина каждого столбца — по ячейкам без горизонтального объединения
    (col_span == 1), иначе ширина объединённой на несколько столбцов ячейки
    ошибочно приписалась бы каждому из них. Нужна, чтобы таблица в DOCX
    сохраняла пропорции исходной, а не получала одинаковые по ширине
    столбцы по умолчанию."""
    widths = [0.0] * n_cols
    for (r, c), (_, col_span) in spans.items():
        if col_span != 1 or r >= len(grid_bbox) or c >= len(grid_bbox[r]):
            continue
        bbox = grid_bbox[r][c]
        if bbox is None:
            continue
        x0, _, x1, _ = bbox
        widths[c] = max(widths[c], x1 - x0)
    return [w if w > 0 else 50.0 for w in widths]


def detect_tables_img2table(
    image_bgr, settings: Settings, px_to_pt: float, font_size_pt: float = 9.0, with_ocr: bool = True
) -> list[Table]:
    """Обнаруживает таблицы на растре скана через img2table + Tesseract.

    image_bgr — уже отрендеренная страница (см. page_processor), px_to_pt —
    коэффициент перевода пиксельных координат рендера в точки PDF (для
    единообразия с координатами слов текстового слоя/OCR на этой же
    странице, см. page_processor._filter_words_outside_tables). font_size_pt —
    см. detect_tables_pdfplumber — та же проблема раздувания страниц лишними
    переносами актуальна и для сканов.
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

    # Растр уже выровнен (deskew в page_processor), поэтому поворот
    # img2table не нужен — иначе координаты ячеек оказались бы в системе
    # координат ДРУГОГО (повёрнутого) растра, чем координаты слов OCR.
    # Текст ячеек по умолчанию берётся из слов, уже распознанных на всей
    # странице (fill_table_from_words) — повторный OCR внутри img2table
    # только удваивал время и местами давал в ячейках строку "None".
    ocr = TesseractOCR(lang=settings.ocr_languages) if with_ocr else None
    doc = Img2TableImage(src=buf.tobytes(), detect_rotation=False)
    try:
        extracted = doc.extract_tables(
            ocr=ocr, implicit_rows=False, borderless_tables=False, min_confidence=50
        )
    except Exception as exc:  # noqa: BLE001 - библиотека может кидать разные типы на "мусорных" страницах
        logger.warning("img2table: не удалось обработать страницу (%s)", exc)
        return []

    tables: list[Table] = []
    for et in extracted:
        table = _convert_img2table(et, px_to_pt, font_size_pt)
        if table is not None:
            tables.append(table)
    return tables


def _convert_img2table(extracted_table, px_to_pt: float, font_size_pt: float = 9.0) -> Table | None:
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
            if text == "None":
                text = ""
            para = Paragraph(runs=[Run(text=text, size_pt=font_size_pt)])
            cb = cell.bbox
            row_obj.cells.append(
                TableCell(
                    blocks=[para],
                    row_span=row_span,
                    col_span=col_span,
                    bbox=BBox(cb.x1 * px_to_pt, cb.y1 * px_to_pt, cb.x2 * px_to_pt, cb.y2 * px_to_pt),
                )
            )
        single = [cell.bbox for c_i, cell in enumerate(row) if spans.get(cell_owner.get((r, c_i)), (1, 1))[0] == 1]
        if single:
            row_obj.height_pt = statistics.median(b.y2 - b.y1 for b in single) * px_to_pt
        table.rows.append(row_obj)

    # Ширина столбца — по ячейкам, которые НЕ являются горизонтальным
    # объединением (col_span == 1): иначе ширина объединённой на несколько
    # столбцов ячейки ошибочно приписалась бы каждому из них.
    n_cols = max((len(r) for r in key_grid), default=0)
    widths_px = [0.0] * n_cols
    for (r, c), (row_span, col_span) in spans.items():
        if col_span != 1:
            continue
        x0, _, x1, _ = key_grid[r][c]
        widths_px[c] = max(widths_px[c], x1 - x0)
    table.col_widths_pt = [(w if w > 0 else 50.0) * px_to_pt for w in widths_px]
    return table


# Символы, которые OCR часто "видит" на линиях границ ячеек и которые
# прилипают к соседнему слову ("2|", "|Группа", "__").
_BORDER_JUNK_RE = re.compile(r"^[|_\[\]]+|[|_\[\]]+$")
_LEADING_QUOTE_JUNK_RE = re.compile(r"^[,‚„“”'\"`]+(?=\w)")
_STANDALONE_SYMBOLS = {"№", "-", "–", "—", "%", "+", "=", "×", "*"}
_WORD_DEFAULT_CELL_PADDING_PT = 5.4


def _clean_cell_word(word: LayoutWord, bbox: BBox) -> Optional[LayoutWord]:
    """Убирает "мусор" OCR на линиях границ ячеек: обрывки линий, принятые
    за "—"/"_"/"|", и прилипшие к слову кавычки/запятые от засечек."""
    text = _BORDER_JUNK_RE.sub("", word.text)
    if not any(ch in "«»\"“”„" for ch in text[1:]):
        text = _LEADING_QUOTE_JUNK_RE.sub("", text)
    text = text.strip()
    if not text:
        return None
    if not any(ch.isalnum() for ch in text):
        if text not in _STANDALONE_SYMBOLS:
            return None
        # Черта вплотную к горизонтальной границе ячейки — обрывок линии.
        cy = (word.y0 + word.y1) / 2
        if text in {"-", "–", "—"} and (cy - bbox.y0 < 3.0 or bbox.y1 - cy < 3.0):
            return None
    if text == word.text:
        return word
    return LayoutWord(**{**word.__dict__, "text": text})


def measure_cell_padding(cells_words: list[tuple[BBox, list[LayoutWord]]]) -> float:
    """Внутреннее поле ячеек: не больше наименьшего зазора между самой
    широкой строкой ячейки и её границами (иначе строка, поместившаяся в
    ячейку оригинала, в Word перенесётся). Нижний дециль — чтобы одна
    ячейка с ошибкой OCR у самой линии не обнуляла поле всей таблицы."""
    gaps = []
    for b, ws in cells_words:
        if not ws:
            continue
        lines = words_to_lines(ws)
        widest = max(lines, key=lambda ln: ln.x1 - ln.x0)
        gaps.append(min(widest.x0 - b.x0, b.x1 - widest.x1))
    gaps = [g for g in gaps if g >= 0.0]
    if not gaps:
        return _WORD_DEFAULT_CELL_PADDING_PT
    gaps.sort()
    return max(0.5, min(_WORD_DEFAULT_CELL_PADDING_PT, gaps[len(gaps) // 10] - 1.5))


def fill_table_from_words(table: Table, words: list[LayoutWord], fit_spacing: bool = False) -> None:
    """Заполняет ячейки таблицы абзацами из слов страницы (слово
    принадлежит ячейке, в которую попадает его центр).

    В отличие от "плоского" текста ячейки, так сохраняются кегль и
    начертание каждого слова, выравнивание текста внутри ячейки (по центру
    или по левому краю), принудительные переносы строк и вертикальное
    выравнивание. Ячейка, в которую не попало ни одного слова, сохраняет
    текст, полученный детектором таблиц (если он есть).
    """
    cells = [cell for row in table.rows for cell in row.cells if not cell.is_merge_continuation and cell.bbox]
    if not cells:
        return
    assigned: dict[int, list[LayoutWord]] = {id(c): [] for c in cells}
    for w in words:
        cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
        for cell in cells:
            b = cell.bbox
            if b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1:
                cleaned = _clean_cell_word(w, b) if table.source != "text" else w
                if cleaned is not None:
                    assigned[id(cell)].append(cleaned)
                break
    padding = measure_cell_padding([(c.bbox, assigned[id(c)]) for c in cells])
    if table.cell_padding_pt is None:
        table.cell_padding_pt = padding
    for cell in cells:
        cell_words = assigned[id(cell)]
        if not cell_words:
            continue
        blocks = build_cell_blocks(cell_words, cell.bbox, padding=table.cell_padding_pt, fit_spacing=fit_spacing)
        if not blocks:
            continue
        cell.blocks = blocks
        cell.v_align = _vertical_alignment(cell.bbox, cell_words)


def _vertical_alignment(bbox: BBox, words: list[LayoutWord]) -> str:
    top_gap = min(w.y0 for w in words) - bbox.y0
    bottom_gap = bbox.y1 - max(w.y1 for w in words)
    height = max(bbox.y1 - bbox.y0, 1.0)
    if top_gap > 0.2 * height and bottom_gap > 0.2 * height and abs(top_gap - bottom_gap) <= 0.35 * max(top_gap, bottom_gap) + 2:
        return "center"
    if top_gap > 0.3 * height and bottom_gap < 0.5 * top_gap:
        return "bottom"
    return "top"
