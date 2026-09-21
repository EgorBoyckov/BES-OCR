"""Обнаружение и восстановление таблиц.

Два независимых пути, оба дают единую модель `Table`/`TableCell` с
row_span/col_span для объединённых ячеек:

* `detect_tables_pdfplumber` — для страниц с пригодным текстовым слоем:
  границы ячеек и точный текст берутся напрямую из PDF.
* `detect_tables_opencv` — для сканов: сетка строится по линиям
  (морфология OpenCV), текст каждой ячейки распознаётся отдельным вызовом
  OCR (точнее, чем резать текст всей страницы по координатам).
"""
from __future__ import annotations

import pdfplumber

from ..config.settings import Settings
from .models import BBox, Paragraph, Run, Table, TableCell, TableRow
from .ocr_engine import OcrEngine


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


def _detect_grid_lines(binary_mask, axis: int, min_length_ratio: float):
    """axis=0 → горизонтальные линии, axis=1 → вертикальные. Возвращает координаты (центры)."""
    import cv2
    import numpy as np

    h, w = binary_mask.shape
    if axis == 0:
        size = max(10, w // 30)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (size, 1))
    else:
        size = max(10, h // 30)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, size))

    eroded = cv2.erode(binary_mask, kernel, iterations=1)
    lines_mask = cv2.dilate(eroded, kernel, iterations=1)

    projection = lines_mask.sum(axis=1 if axis == 0 else 0) / 255
    limit = (w if axis == 0 else h) * min_length_ratio
    coords = []
    in_line = False
    start = 0
    for i, val in enumerate(projection):
        if val >= limit and not in_line:
            in_line = True
            start = i
        elif val < limit and in_line:
            in_line = False
            coords.append((start + i - 1) // 2)
    if in_line:
        coords.append((start + len(projection) - 1) // 2)
    return coords, lines_mask


def _hough_grid_lines(binary_crop, horizontal: bool, min_length_ratio: float, cluster_dist: int = 30):
    """Находит координаты линий сетки через преобразование Хафа.

    В отличие от чисто морфологического подхода (эрозия строго горизонтальным/
    вертикальным ядром), устойчиво к небольшому перекосу скана: реальные
    сканы редко идеально ровные, и даже перекос менее градуса разрывает
    тонкие линии на множество фрагментов при построчной/постолбцовой
    проекции, из-за чего таблица не обнаруживается вовсе. Хаф ищет отрезки
    близкой к горизонтали/вертикали ориентации напрямую, без этого
    ограничения. Возвращает (координаты, маска с отрисованными линиями —
    используется дальше для проверки наличия границы при поиске
    объединённых ячеек).
    """
    import math

    import cv2
    import numpy as np

    h, w = binary_crop.shape
    min_len = max(20, (w if horizontal else h) * min_length_ratio)
    mask = np.zeros_like(binary_crop)

    # threshold высокий относительно minLineLength: сплошная линованная
    # линия таблицы набирает почти полный голос почти на всей своей длине,
    # тогда как строка текста (даже длинная) — рыхлая/прерывистая из-за
    # засечек и пробелов между буквами и почти никогда не проходит порог
    # 0.7*min_len. Это отсекает текст, который иначе Хаф с меньшим порогом
    # иногда принимает за линию таблицы.
    lines = cv2.HoughLinesP(
        binary_crop,
        1,
        np.pi / 720,
        threshold=max(10, int(min_len * 0.7)),
        minLineLength=int(min_len),
        maxLineGap=max(10, int(min_len * 0.05)),
    )
    if lines is None:
        return [], mask

    raw: list[float] = []
    angle_tol_deg = 5
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
        if horizontal and abs(angle) < angle_tol_deg:
            raw.append((y1 + y2) / 2.0)
            cv2.line(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, 3)
        elif (not horizontal) and abs(abs(angle) - 90) < angle_tol_deg:
            raw.append((x1 + x2) / 2.0)
            cv2.line(mask, (int(x1), int(y1)), (int(x2), int(y2)), 255, 3)

    raw.sort()
    clustered: list[tuple[float, int]] = []
    for c in raw:
        if clustered and abs(c - clustered[-1][0]) < cluster_dist:
            prev_c, n = clustered[-1]
            clustered[-1] = ((prev_c * n + c) / (n + 1), n + 1)
        else:
            clustered.append((c, 1))

    coords = [int(round(c)) for c, _ in clustered]
    return coords, mask


def _find_table_regions(binary, min_area_ratio: float = 0.01):
    """Находит прямоугольные области, где присутствуют и горизонтальные, и
    вертикальные линии — кандидаты в таблицы. Порог длины линии считается
    относительно самой области, а не всей страницы, чтобы находить и
    небольшие таблицы, занимающие лишь часть страницы.
    """
    import cv2

    h, w = binary.shape
    h_coords_raw, h_mask_raw = _detect_grid_lines(binary, axis=0, min_length_ratio=0.03)
    v_coords_raw, v_mask_raw = _detect_grid_lines(binary, axis=1, min_length_ratio=0.03)
    if not h_coords_raw or not v_coords_raw:
        return []

    combined = cv2.bitwise_or(h_mask_raw, v_mask_raw)
    combined = cv2.dilate(combined, cv2.getStructuringElement(cv2.MORPH_RECT, (15, 15)))
    contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    regions = []
    min_area = w * h * min_area_ratio
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        if cw * ch < min_area or cw < 30 or ch < 30:
            continue
        regions.append((max(0, x - 5), max(0, y - 5), min(w, x + cw + 5), min(h, y + ch + 5)))
    return regions


def _map_rect_to_original(x0, y0, x1, y1, inv_matrix):
    """Переводит прямоугольник из координат выровненного (повёрнутого)
    изображения обратно в координаты исходного скана."""
    if inv_matrix is None:
        return x0, y0, x1, y1
    import numpy as np

    pts = np.array([[x0, y0, 1.0], [x1, y0, 1.0], [x1, y1, 1.0], [x0, y1, 1.0]])
    mapped = pts @ inv_matrix.T
    xs, ys = mapped[:, 0], mapped[:, 1]
    return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())


def detect_tables_opencv(image_bgr, ocr_engine: OcrEngine, settings: Settings) -> list[Table]:
    """Обнаруживает таблицы по линиям на растре скана.

    Поиск линий сетки выполняется на выровненной (deskewed) копии страницы —
    даже небольшой перекос скана (доли градуса) ломает построчную/постолбцовую
    детекцию линий, хотя визуально почти незаметен. Но текст ячеек
    распознаётся по ИСХОДНОМУ, неповёрнутому изображению: поворот
    (warpAffine, даже с INTER_CUBIC) заметно размывает некрупный текст и
    резко ухудшает точность OCR. Поэтому координаты найденной сетки
    пересчитываются обратно в координаты исходного изображения перед
    вырезкой ячеек и перед сохранением bbox таблицы.
    """
    import cv2

    from .ocr_engine import estimate_skew_angle

    angle = estimate_skew_angle(image_bgr)
    h, w = image_bgr.shape[:2]
    if abs(angle) < 0.1 or abs(angle) > 15:
        rotated = image_bgr
        inv_matrix = None
    else:
        center = (w / 2, h / 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(
            image_bgr, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
        )
        inv_matrix = cv2.invertAffineTransform(matrix)

    gray = cv2.cvtColor(rotated, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )

    regions = _find_table_regions(binary)
    tables: list[Table] = []
    for rx0, ry0, rx1, ry1 in regions:
        crop = binary[ry0:ry1, rx0:rx1]
        table = _detect_table_in_region(crop, image_bgr, inv_matrix, rx0, ry0, ocr_engine, settings)
        if table is not None:
            tables.append(table)
    return tables


def _detect_table_in_region(
    binary_crop, original_image_bgr, inv_matrix, off_x: int, off_y: int, ocr_engine: OcrEngine, settings: Settings
):
    h_coords_l, h_mask = _hough_grid_lines(binary_crop, horizontal=True, min_length_ratio=settings.table_line_min_length_ratio)
    v_coords_l, v_mask = _hough_grid_lines(binary_crop, horizontal=False, min_length_ratio=settings.table_line_min_length_ratio)

    if len(h_coords_l) < 2 or len(v_coords_l) < 2:
        return None

    h_coords = [c + off_y for c in h_coords_l]
    v_coords = [c + off_x for c in v_coords_l]

    # Проверка объединения ячеек — по самому бинарному изображению кропа, а
    # не по разреженной маске из отфильтрованных длинных линий Хафа: реальный
    # скан бумажного (часто сшитого/скреплённого) документа обычно не просто
    # повёрнут на постоянный угол, а слегка "волнистый" — одна и та же линия
    # таблицы может отклоняться по вертикали на десятки пикселей между левым
    # и правым краем страницы сильнее, чем предсказывает единый угол
    # поворота всей страницы. Поэтому границу ищем не строго в ожидаемой
    # координате, а лучшим совпадением в широком окне поиска вокруг неё —
    # это специально смещает баланс в сторону "не объединять", если есть
    # сомнение: ложное объединение необратимо склеивает текст разных строк в
    # одну ячейку, а пропущенное объединение просто оставляет две ячейки с
    # похожим содержимым, что гораздо легче заметить и не искажает данные.
    # Радиус поиска — с учётом ТОЛЬКО двух конкретных соседних полос, между
    # которыми проверяется граница (не минимума по всей таблице: одна
    # маленькая строка где-то ещё в таблице не должна сужать поиск там, где
    # соседние строки высокие и волнистость линии может быть заметнее).
    def _search_radius(gap_a: int, gap_b: int) -> int:
        return max(18, min(35, min(gap_a, gap_b) // 3))

    def vertical_border_present(x_expected, y0, y1, gap_before, gap_after) -> bool:
        search = _search_radius(gap_before, gap_after)
        x_lo = max(0, x_expected - search)
        x_hi = min(binary_crop.shape[1], x_expected + search + 1)
        y_lo, y_hi = max(0, y0 + 3), max(y0 + 4, y1 - 3)
        band = binary_crop[y_lo:y_hi, x_lo:x_hi]
        if band.size == 0:
            return False
        col_cov = (band > 0).mean(axis=0)
        return bool(col_cov.size) and col_cov.max() > 0.6

    def horizontal_border_present(y_expected, x0, x1, gap_before, gap_after) -> bool:
        search = _search_radius(gap_before, gap_after)
        y_lo = max(0, y_expected - search)
        y_hi = min(binary_crop.shape[0], y_expected + search + 1)
        x_lo, x_hi = max(0, x0 + 3), max(x0 + 4, x1 - 3)
        band = binary_crop[y_lo:y_hi, x_lo:x_hi]
        if band.size == 0:
            return False
        row_cov = (band > 0).mean(axis=1)
        return bool(row_cov.size) and row_cov.max() > 0.6

    n_rows = len(h_coords_l) - 1
    n_cols = len(v_coords_l) - 1
    if n_rows < 1 or n_cols < 1:
        return None

    # Определяем отсутствующие внутренние границы → объединённые ячейки.
    # border_present работает в локальных координатах кропа (h_coords_l/v_coords_l).
    merged_right = [[False] * n_cols for _ in range(n_rows)]
    merged_down = [[False] * n_cols for _ in range(n_rows)]
    for r in range(n_rows):
        for c in range(n_cols):
            if c < n_cols - 1:
                gap_before = v_coords_l[c + 1] - v_coords_l[c]
                gap_after = v_coords_l[c + 2] - v_coords_l[c + 1] if c + 2 < len(v_coords_l) else gap_before
                present = vertical_border_present(
                    v_coords_l[c + 1], h_coords_l[r], h_coords_l[r + 1], gap_before, gap_after
                )
                merged_right[r][c] = not present
            if r < n_rows - 1:
                gap_before = h_coords_l[r + 1] - h_coords_l[r]
                gap_after = h_coords_l[r + 2] - h_coords_l[r + 1] if r + 2 < len(h_coords_l) else gap_before
                present = horizontal_border_present(
                    h_coords_l[r + 1], v_coords_l[c], v_coords_l[c + 1], gap_before, gap_after
                )
                merged_down[r][c] = not present

    owner = {}
    span = {}
    visited = [[False] * n_cols for _ in range(n_rows)]
    for r in range(n_rows):
        for c in range(n_cols):
            if visited[r][c]:
                continue
            # растим прямоугольную область объединения вправо/вниз
            c_end = c
            while c_end + 1 < n_cols and merged_right[r][c_end]:
                c_end += 1
            r_end = r
            can_grow = True
            while can_grow and r_end + 1 < n_rows:
                for cc in range(c, c_end + 1):
                    if not merged_down[r_end][cc]:
                        can_grow = False
                        break
                if can_grow:
                    r_end += 1
            for rr in range(r, r_end + 1):
                for cc in range(c, c_end + 1):
                    visited[rr][cc] = True
                    owner[(rr, cc)] = (r, c)
            span[(r, c)] = (r_end - r + 1, c_end - c + 1)

    img_h, img_w = original_image_bgr.shape[:2]
    table_ox0, table_oy0, table_ox1, table_oy1 = _map_rect_to_original(
        v_coords[0], h_coords[0], v_coords[-1], h_coords[-1], inv_matrix
    )
    table = Table(
        bbox=BBox(table_ox0, table_oy0, table_ox1, table_oy1),
        source="ocr",
    )
    for r in range(n_rows):
        row_obj = TableRow()
        for c in range(n_cols):
            own = owner.get((r, c), (r, c))
            if own != (r, c):
                row_obj.cells.append(TableCell(is_merge_continuation=True, row_span=0, col_span=0))
                continue
            row_span, col_span = span.get((r, c), (1, 1))
            y0, y1 = h_coords[r], h_coords[r + row_span]
            x0, x1 = v_coords[c], v_coords[c + col_span]
            # Прямоугольник ячейки найден в координатах выровненной страницы;
            # для OCR вырезаем из ИСХОДНОГО изображения (без размытия от
            # поворота), поэтому пересчитываем координаты обратно.
            ox0, oy0, ox1, oy1 = _map_rect_to_original(x0, y0, x1, y1, inv_matrix)
            pad = 4
            cx0 = max(0, min(img_w, int(ox0) + pad))
            cx1 = max(cx0 + 1, min(img_w, int(ox1) - pad))
            cy0 = max(0, min(img_h, int(oy0) + pad))
            cy1 = max(cy0 + 1, min(img_h, int(oy1) - pad))
            crop = original_image_bgr[cy0:cy1, cx0:cx1]
            text = ocr_engine.recognize_cell_text(crop) if crop.size > 0 else ""
            para = Paragraph(runs=[Run(text=text)])
            row_obj.cells.append(
                TableCell(blocks=[para], row_span=row_span, col_span=col_span, bbox=BBox(ox0, oy0, ox1, oy1))
            )
        table.rows.append(row_obj)
    return table
