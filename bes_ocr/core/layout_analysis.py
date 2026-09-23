"""Анализ разметки страницы: слова → строки → параграфы/заголовки/списки.

Работает над унифицированным представлением слов (LayoutWord), общим для
текстового слоя PDF и результатов OCR — это единственное место, где
принимается решение о структуре текста, независимо от его источника.

Цель — не просто "правильный текст", а совпадение вёрстки с оригиналом
один в один: тот же кегль, то же выравнивание, те же принудительные
переносы строк и те же вертикальные положения строк. Поэтому, помимо
структуры, модуль сохраняет в абзацах геометрию оригинала (базовая линия,
шаг строк, левая/правая границы), а генератор DOCX воспроизводит её.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field, replace
from typing import Optional

from . import font_metrics
from .models import Alignment, BBox, Heading, ListItem, Paragraph, Run, Table, TableCell, TableRow

_ORDERED_RE = re.compile(r"^\s*(\d{1,3}|[a-zA-Zа-яА-Я])[.)]\s+")
_BULLET_RE = re.compile(r"^\s*[•\-•‣◦⁃*]\s+")

# Стандартная доля нижнего выносного элемента Times New Roman — базовая
# линия строки, у которой не удалось оценить её по глифам.
_DESCENT_EM = 0.216


@dataclass
class LayoutWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    size_pt: float = 11.0
    bold: bool = False
    italic: bool = False
    font_name: str = ""
    # Базовая линия (абсолютная y) — известна точно для текстового слоя,
    # для OCR вычисляется по метрикам глифов (normalize_ocr_words).
    baseline: Optional[float] = None
    # Толщина штриха в точках (только OCR) — признак полужирного начертания.
    stroke_pt: Optional[float] = None


@dataclass
class LayoutLine:
    words: list[LayoutWord] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def center_x(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def median_size(self) -> float:
        return statistics.median([w.size_pt for w in self.words]) if self.words else 11.0

    @property
    def size(self) -> float:
        """Кегль строки — взвешенная по длине слов медиана (одно короткое
        слово с иным размером, например номер сноски, не должно менять
        оценку для всей строки)."""
        return _weighted_median([(w.size_pt, max(1, len(w.text))) for w in self.words]) or 11.0

    @property
    def bold(self) -> bool:
        total = sum(len(w.text) for w in self.words) or 1
        return sum(len(w.text) for w in self.words if w.bold) / total >= 0.6

    @property
    def baseline(self) -> float:
        values = [w.baseline for w in self.words if w.baseline is not None]
        if values:
            return statistics.median(values)
        return self.y1 - _DESCENT_EM * self.size


def _weighted_median(pairs: list[tuple[float, float]]) -> Optional[float]:
    pairs = [(v, w) for v, w in pairs if v is not None and w > 0]
    if not pairs:
        return None
    pairs.sort()
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def _bold_threshold(pairs: list[tuple[float, float]]) -> float:
    """Порог относительной толщины штриха, выше которого строка полужирная.

    Толщины строк одного кегля образуют один класс (только обычный текст)
    или два (обычный и полужирный) с заметным разрывом между ними. Порог —
    посередине самого большого разрыва (по отношению соседних значений),
    если он не меньше 10% и ниже разрыва остаётся основная масса текста;
    иначе полужирного в группе нет (порог — заведомо выше всех значений,
    с запасом 25% над нижним квартилем)."""
    values = sorted((v, w) for v, w in pairs if v)
    if not values:
        return float("inf")
    total = sum(w for _, w in values)
    base = _weighted_quantile(values, 0.3) or values[0][0]
    best_gap, best_threshold = 1.0, base * 1.25
    acc = 0.0
    for (lo, w), (hi, _) in zip(values, values[1:]):
        acc += w
        if acc < 0.3 * total or lo <= 0:
            continue
        gap = hi / lo
        if gap > best_gap:
            best_gap, best_threshold = gap, (lo * hi) ** 0.5
    if best_gap >= 1.1 and best_threshold >= base * 1.08:
        return best_threshold
    return base * 1.25


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> Optional[float]:
    """Взвешенный квантиль. Для опорной толщины штриха обычного текста
    берётся нижний (~30%) квантиль, а не медиана: иначе многословный
    полужирный заголовок сам сдвигает опорное значение вверх, и порог
    "полужирности" оказывается между классами неудачно."""
    pairs = sorted((v, w) for v, w in pairs if v is not None and w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total * q:
            return v
    return pairs[-1][0]


def _percentile(values: list[float], p: float) -> float:
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[idx]


def words_to_lines(words: list[LayoutWord], y_tolerance: float = 4.0) -> list[LayoutLine]:
    """Группирует слова в строки по вертикальному перекрытию, слева направо.

    Однопроходная группировка по СКОЛЬЗЯЩЕМУ СРЕДНЕМУ центру накопленной
    строки (а не по её мгновенному min/max bbox, который дрейфует по мере
    добавления слов и делает результат чувствительным к порядку обработки).
    На реальных сканах слова одной и той же визуальной строки (особенно
    из независимо распознанных смежных блоков текста) редко имеют абсолютно
    одинаковый y0/y1 (дрожание OCR-рамок), и старая версия (сравнение с
    каждой уже существующей строкой по её текущему bbox) иногда
    непредсказуемо не сливала слова одной и той же строки.
    """
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: ((w.y0 + w.y1) / 2, w.x0))
    lines: list[LayoutLine] = []
    current: list[LayoutWord] = []
    current_center = 0.0
    for w in sorted_words:
        center = (w.y0 + w.y1) / 2
        if current:
            avg_height = sum(ww.y1 - ww.y0 for ww in current) / len(current)
            if abs(center - current_center) > y_tolerance + avg_height * 0.35:
                lines.append(LayoutLine(words=current))
                current = []
        current.append(w)
        current_center = sum((ww.y0 + ww.y1) / 2 for ww in current) / len(current)
    if current:
        lines.append(LayoutLine(words=current))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    lines.sort(key=lambda ln: ln.y0)
    return lines


# --------------------------------------------------------------------------
# Кегль и начертание слов OCR
# --------------------------------------------------------------------------

_STANDARD_SIZES = (6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5, 10, 10.5, 11, 11.5, 12, 13, 14, 15, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 48)


def snap_font_size(size: float) -> float:
    """Округляет оценку кегля до ближайшего "типового" значения: реальные
    документы набираются кеглями 9/10/11/12/14..., а не 11.73pt — и
    одинаковый кегль у всех строк одного уровня важнее, чем попытка
    передать погрешность измерения."""
    best = min(_STANDARD_SIZES, key=lambda s: abs(s - size))
    # Предпочитаем целый кегль половинному, если оценка к нему близка.
    rounded = round(size)
    if abs(size - rounded) <= 0.3 and rounded in _STANDARD_SIZES:
        return float(rounded)
    return float(best)


def _cluster_sizes(values: list[tuple[float, float]], rel_tol: float = 0.07) -> dict[float, float]:
    """Сводит близкие оценки кегля к общему значению (кластеризация по
    весу): строки одного уровня текста на скане дают оценки с разбросом в
    несколько процентов, в DOCX они должны получить ОДИН кегль."""
    remaining = sorted({round(v, 2) for v, _ in values})
    weight = {}
    for v, w in values:
        weight[round(v, 2)] = weight.get(round(v, 2), 0.0) + w
    mapping: dict[float, float] = {}
    while remaining:
        peak = max(remaining, key=lambda v: weight[v])
        members = [v for v in remaining if abs(v - peak) <= peak * rel_tol]
        center = _weighted_median([(v, weight[v]) for v in members]) or peak
        snapped = snap_font_size(center)
        for v in members:
            mapping[v] = snapped
        remaining = [v for v in remaining if v not in members]
    return mapping


def normalize_ocr_words(words: list[LayoutWord], default_size: float = 11.0) -> list[LayoutWord]:
    """Оценивает кегль, начертание и базовую линию слов, полученных OCR.

    Кегль оценивается по строке целиком, а не по отдельному слову: сначала
    по ширине слов (метрики Times New Roman — font_metrics), при нехватке
    длинных слов — по высоте с учётом выносных элементов глифов. Затем
    оценки страницы кластеризуются и округляются до типовых кеглей.
    Полужирность — по относительной толщине штриха: строка заметно
    "толще" строк того же кегля на странице.
    """
    if not words:
        return []
    lines = words_to_lines(words)

    raw_size: dict[int, float] = {}
    strong: set[int] = set()
    line_weight: dict[int, float] = {}
    stroke_ratio: dict[int, Optional[float]] = {}
    for i, line in enumerate(lines):
        width_est = []
        height_est = []
        for w in line.words:
            sig = sum(1 for ch in w.text if ch.isalnum())
            s_w = font_metrics.size_from_width(w.text, w.x1 - w.x0)
            if s_w:
                width_est.append((s_w, sig))
            s_h = font_metrics.size_from_height(w.text, w.y1 - w.y0)
            if s_h:
                height_est.append((s_h, max(1, sig)))
        if sum(wt for _, wt in width_est) >= 4:
            size = _weighted_median(width_est)
            strong.add(i)
        elif height_est:
            size = _weighted_median(height_est)
        else:
            size = None
        if size is None or not (3.0 <= size <= 72.0):
            size = None
        if size is not None:
            raw_size[i] = size
            line_weight[i] = sum(len(w.text) for w in line.words)
        # Толщина штриха строки — по буквам: у цифр и знаков (особенно "1")
        # штрих заметно толще, и в короткой строке они искажали бы оценку.
        strokes = [(w.stroke_pt, sum(ch.isalpha() for ch in w.text) or 0.3) for w in line.words if w.stroke_pt]
        stroke = _weighted_median(strokes)
        stroke_ratio[i] = (stroke / size) if stroke and size else None

    # Полужирность: сравниваем толщину штриха строки с типичной для строк
    # близкого кегля (у мелкого текста относительная толщина на скане всегда
    # чуть больше из-за постоянного "расплывания" краёв при сканировании,
    # поэтому общий порог для всей страницы ошибался бы).
    bold_line: dict[int, bool] = {}
    all_ratios = [(stroke_ratio[i], line_weight[i]) for i in raw_size if stroke_ratio[i]]
    global_ref = _weighted_median(all_ratios) if all_ratios else None
    for i in raw_size:
        r = stroke_ratio[i]
        if r is None or global_ref is None:
            bold_line[i] = False
            continue
        peers = [
            (stroke_ratio[j], line_weight[j])
            for j in raw_size
            if stroke_ratio[j] and abs(raw_size[j] - raw_size[i]) <= raw_size[i] * 0.15
        ]
        if sum(w for _, w in peers) < 60:
            peers = all_ratios
        bold_line[i] = r >= _bold_threshold(peers)

    # Уточнение кегля полужирных строк по ширинам полужирного начертания
    # (оно шире обычного, иначе кегль заголовка завышается на ~5%).
    for i, is_bold in bold_line.items():
        if not is_bold:
            continue
        est = [
            (font_metrics.size_from_width(w.text, w.x1 - w.x0, bold=True), sum(1 for ch in w.text if ch.isalnum()))
            for w in lines[i].words
        ]
        est = [(v, wt) for v, wt in est if v]
        if sum(wt for _, wt in est) >= 4:
            raw_size[i] = _weighted_median(est)

    # Строки без длинных слов ("с", "по", "1", "систем") дают по высоте
    # лишь грубую оценку — если рядом есть надёжно оценённая строка
    # близкого кегля (соседняя строка той же ячейки/абзаца), берём её кегль.
    for i in list(raw_size):
        if i in strong:
            continue
        line = lines[i]
        best = None
        for j in strong:
            other = lines[j]
            overlap = min(line.x1, other.x1) - max(line.x0, other.x0)
            if overlap < -2 * raw_size[j]:
                continue
            dist = abs(other.baseline - line.baseline)
            if dist > 3.5 * raw_size[j]:
                continue
            if best is None or dist < best[0]:
                best = (dist, j)
        if best is not None and abs(raw_size[best[1]] - raw_size[i]) <= 0.35 * raw_size[best[1]]:
            raw_size[i] = raw_size[best[1]]

    mapping = _cluster_sizes([(raw_size[i], line_weight[i]) for i in raw_size])
    fallback = snap_font_size(_weighted_median([(raw_size[i], line_weight[i]) for i in raw_size]) or default_size)

    result: list[LayoutWord] = []
    for i, line in enumerate(lines):
        size = mapping.get(round(raw_size[i], 2), fallback) if i in raw_size else fallback
        is_bold = bold_line.get(i, False)
        ref = stroke_ratio.get(i)
        for w in line.words:
            word_bold = is_bold
            # Отдельное выделенное слово внутри обычной строки.
            if not is_bold and global_ref and w.stroke_pt and len(w.text) >= 4:
                word_bold = (w.stroke_pt / size) > global_ref * 1.35 and (ref is None or (w.stroke_pt / size) > ref * 1.25)
            ext = font_metrics.vertical_extent_em(w.text)
            baseline = w.y1 - ext[1] * size if ext else w.y1 - _DESCENT_EM * size
            result.append(replace(w, size_pt=size, bold=word_bold, baseline=baseline))
    return result


# --------------------------------------------------------------------------
# Строки → абзацы
# --------------------------------------------------------------------------


def _word_width(w: LayoutWord) -> float:
    return w.x1 - w.x0


def _is_list_start(line: LayoutLine) -> Optional[tuple[re.Match, bool]]:
    # Маркер пункта списка стоит рядом с текстом пункта; "—" или "1." далеко
    # слева от остального текста строки — это другое (строка подписи,
    # номер в графе и т.п.).
    if len(line.words) >= 2 and line.words[1].x0 - line.words[0].x1 > 3.0 * line.size:
        return None
    text = line.text.strip()
    m = _ORDERED_RE.match(text)
    if m:
        return m, True
    m = _BULLET_RE.match(text)
    if m:
        return m, False
    return None


def _natural_wrap(prev: LayoutLine, nxt: LayoutLine, x_left: float, x_right: float, centered: bool) -> bool:
    """Был ли перенос между prev и nxt "естественным" (следующее слово не
    поместилось бы в строку) или принудительным (строка закончена раньше —
    Enter/Shift+Enter в оригинале). Естественные переносы текстовый
    процессор сделает сам, принудительные нужно сохранить явно."""
    size = prev.size
    space = font_metrics.SPACE_ADVANCE_EM * size
    first = _word_width(nxt.words[0])
    slack = 0.5 * size
    if centered:
        return (prev.x1 - prev.x0) + space + first > (x_right - x_left) - slack
    return prev.x1 + space + first > x_right - slack


def _alignment_shape(prev: LayoutLine, line: LayoutLine, tol: float) -> tuple[bool, bool, bool]:
    left_ok = abs(line.x0 - prev.x0) <= tol
    center_ok = abs(line.center_x - prev.center_x) <= tol * 1.5
    right_ok = abs(line.x1 - prev.x1) <= tol
    return left_ok, center_ok, right_ok


def _chain_line_pitch(blocks: list[Paragraph]) -> None:
    """Абзацы, идущие вплотную друг к другу (следующий начинается на
    следующей строке), получают шаг строк, точно ведущий к базовой линии
    следующего абзаца.

    Шаг, измеренный внутри абзаца, от абзаца к абзацу немного "гуляет"
    (±0.3pt), а перенести следующий абзац ВЫШЕ текстовый процессор не
    может — отрицательного отступа перед абзацем нет. Без этой поправки
    погрешность копится только вниз: на странице из десятка абзацев текст
    внизу оказывался на 10pt ниже оригинала."""
    for a, b in zip(blocks, blocks[1:]):
        if a.baseline_pt is None or b.baseline_pt is None:
            continue
        size_a = _weighted_median([(r.size_pt, len(r.text)) for r in a.runs if r.text.strip()]) or 0.0
        size_b = _weighted_median([(r.size_pt, len(r.text)) for r in b.runs if r.text.strip()]) or 0.0
        if not size_a or abs(size_a - size_b) > 0.6:
            continue
        n = max(1, a.n_lines)
        pitch = a.line_pitch_pt or b.line_pitch_pt or size_a * 1.15
        step = (b.baseline_pt - a.baseline_pt) / n
        # "Вплотную": до следующего абзаца ровно n строк с тем же шагом.
        # Допуск 20%: у однострочных строк базовая линия по OCR "гуляет" на
        # 1–1.5pt; нижняя граница шага — чтобы при точном интервале не
        # обрезались верхушки заглавных букв.
        if abs(step - pitch) <= 0.2 * pitch and step >= size_a * 1.05:
            a.line_pitch_pt = step


class _ParagraphBuilder:
    """Группирует строки одного "потока" текста (страница или колонка) в
    абзацы и определяет их выравнивание относительно границ потока."""

    def __init__(
        self,
        x_left: float,
        x_right: float,
        body_size: float,
        allow_headings: bool = True,
        fit_spacing: bool = False,
        keep_line_breaks: bool = False,
    ):
        self.keep_line_breaks = keep_line_breaks
        self.x_left = x_left
        self.x_right = x_right
        self.body_size = body_size
        self.allow_headings = allow_headings
        self.fit_spacing = fit_spacing
        self.list_indent_stack: list[float] = []

    # Допуск в 8pt отсеивает дрожание OCR-координат внутри одного уровня
    # списка, не давая ему ошибочно создать лишний уровень вложенности.
    _LIST_INDENT_TOLERANCE = 8.0

    def build(self, lines: list[LayoutLine], prev_bottom: Optional[float] = None) -> list[object]:
        groups: list[list[LayoutLine]] = []
        forced: list[list[bool]] = []
        for line in lines:
            if groups:
                verdict = self._continues(groups[-1], line)
                if verdict is not None:
                    groups[-1].append(line)
                    forced[-1].append(verdict)
                    continue
            groups.append([line])
            forced.append([])
        blocks = []
        for g, f in zip(groups, forced):
            block = self._make_block(g, f)
            gap = (g[0].y0 - prev_bottom) if prev_bottom is not None else 0.0
            block.space_before_pt = max(0.0, gap)
            prev_bottom = g[-1].y1
            blocks.append(block)
        _chain_line_pitch(blocks)
        return blocks

    def _continues(self, group: list[LayoutLine], line: LayoutLine) -> Optional[bool]:
        """None — строка начинает новый абзац; иначе True/False — строка
        продолжает абзац после принудительного/естественного переноса."""
        prev = group[-1]
        if _is_list_start(line):
            return None
        size = prev.size
        if abs(line.size - size) > 0.6 or line.bold != prev.bold:
            return None
        pitch = line.baseline - prev.baseline
        if pitch <= 0:
            return None
        if len(group) >= 2:
            ref_pitch = statistics.median(b.baseline - a.baseline for a, b in zip(group, group[1:]))
            if pitch > ref_pitch * 1.25 + 1.0:
                return None
        elif pitch > size * 1.75:
            return None

        tol = max(2.5, 0.35 * size)
        left_ok, center_ok, right_ok = _alignment_shape(prev, line, tol)
        head = _is_list_start(group[0])
        if head is not None and len(group[0].words) > 1:
            # Продолжение пункта списка выравнивается по тексту пункта, а
            # не по маркеру.
            text_x = group[0].words[1].x0 if len(group) == 1 else prev.x0
            left_ok = abs(line.x0 - text_x) <= tol or abs(line.x0 - group[0].x0) <= tol
            # Маркер в "красной строке", продолжение — от левого края
            # потока ("3. Проведена оценка ..." / "практики:").
            if len(group) == 1 and abs(line.x0 - self.x_left) <= tol and line.x0 < group[0].x0 - tol:
                left_ok = True
        # Первая строка абзаца может иметь красную строку (отступ первой
        # строки) — вторая строка тогда начинается левее первой.
        first_line_indent = len(group) == 1 and 0 < prev.x0 - line.x0 <= 6 * size and line.x0 >= self.x_left - tol
        if not (left_ok or center_ok or right_ok or first_line_indent):
            return None

        # Для строк по центру/справа перенос "естественный", только если
        # следующее слово не поместилось бы в ширину потока целиком.
        centered = (center_ok or right_ok) and not left_ok
        natural = _natural_wrap(prev, line, self.x_left, self.x_right, centered)
        if natural:
            return False
        # Принудительный перенос: у выровненного влево/по ширине текста это
        # конец абзаца; у строк по центру или справа (шапки, заголовки) —
        # разрыв строки внутри одного абзаца.
        if (left_ok or first_line_indent) and not (right_ok and not left_ok):
            return None
        return True

    def _make_block(self, group: list[LayoutLine], forced: list[bool]) -> Paragraph:
        size = _weighted_median([(ln.size, len(ln.text)) for ln in group]) or self.body_size
        tol = max(2.5, 0.35 * size)
        list_info = _is_list_start(group[0])

        # --- текст и runs ---------------------------------------------------
        all_words: list[tuple[LayoutWord, str]] = []  # (слово, разделитель перед ним)
        for li, line in enumerate(group):
            for wi, w in enumerate(line.words):
                if li == 0 and wi == 0:
                    sep = ""
                elif wi == 0:
                    prev_text = group[li - 1].words[-1].text
                    if forced[li - 1] or self.keep_line_breaks:
                        sep = "\n"
                    elif prev_text.endswith("-") and w.text[:1].islower():
                        sep = ""
                    else:
                        sep = " "
                else:
                    sep = " "
                all_words.append((w, sep))

        marker = ""
        if list_info is not None:
            m, _ = list_info
            marker = m.group(0).strip()
            # Отрезаем слова, образующие маркер.
            consumed = 0
            while all_words and consumed < len(marker):
                consumed += len(all_words[0][0].text) + (1 if consumed else 0)
                all_words.pop(0)
            if all_words:
                all_words[0] = (all_words[0][0], "")

        runs: list[Run] = []
        for w, sep in all_words:
            key = (w.bold, w.italic, round(w.size_pt * 2) / 2, w.font_name or None)
            if runs and (runs[-1].bold, runs[-1].italic, round(runs[-1].size_pt * 2) / 2, runs[-1].font_name) == key:
                runs[-1].text += sep + w.text
            else:
                if runs and sep:
                    runs[-1].text += sep
                    sep = ""
                runs.append(Run(text=sep + w.text, bold=w.bold, italic=w.italic, size_pt=w.size_pt, font_name=w.font_name or None))

        if self.fit_spacing:
            spacing = _fit_char_spacing([w for w, _ in all_words], size)
            for run in runs:
                run.char_spacing_pt = spacing

        # --- геометрия ------------------------------------------------------
        bbox = BBox(min(ln.x0 for ln in group), min(ln.y0 for ln in group), max(ln.x1 for ln in group), max(ln.y1 for ln in group))
        n = len(group)
        pitch = None
        if n >= 2:
            pitch = statistics.median(b.baseline - a.baseline for a, b in zip(group, group[1:]))
        alignment, indent, first_indent, right_edge = self._geometry(group, forced, tol)

        common = dict(
            runs=runs,
            alignment=alignment,
            indent_pt=indent,
            first_line_indent_pt=first_indent,
            right_edge_pt=right_edge,
            bbox=bbox,
            baseline_pt=group[0].baseline,
            line_pitch_pt=pitch,
            n_lines=n,
        )

        if list_info is not None:
            m, ordered = list_info
            marker_x = group[0].x0
            text_x = group[0].words[1].x0 if len(group[0].words) > 1 else marker_x
            while self.list_indent_stack and marker_x < self.list_indent_stack[-1] - self._LIST_INDENT_TOLERANCE:
                self.list_indent_stack.pop()
            if not self.list_indent_stack or marker_x > self.list_indent_stack[-1] + self._LIST_INDENT_TOLERANCE:
                self.list_indent_stack.append(marker_x)
            level = len(self.list_indent_stack) - 1
            left = text_x
            if len(group) > 1:
                cont_x = min(ln.x0 for ln in group[1:])
                if cont_x < text_x - tol:
                    left = cont_x  # продолжение левее текста пункта
            common.update(indent_pt=left, first_line_indent_pt=marker_x - left)
            if common["alignment"] in (Alignment.CENTER, Alignment.RIGHT):
                common["alignment"] = Alignment.LEFT
            return ListItem(ordered=ordered, marker=marker, level=level, text_x_pt=text_x, **common)

        text = "".join(r.text for r in runs).strip()
        heading_candidate = len(group[0].words) >= 2 or len(text) >= 4
        if self.allow_headings and heading_candidate and size > self.body_size * 1.25 and len(text) < 140:
            level = 1 if size > self.body_size * 1.45 else 2
            return Heading(level=level, **common)
        return Paragraph(**common)

    def _geometry(self, group: list[LayoutLine], forced: list[bool], tol: float):
        """(выравнивание, левая граница, отступ первой строки, правая граница)."""
        xl, xr = self.x_left, self.x_right
        region_center = (xl + xr) / 2
        first = group[0]
        n = len(group)

        def centered_geometry(center: float):
            offset = center - region_center
            if abs(offset) <= tol:
                return Alignment.CENTER, xl, 0.0, xr
            if offset > 0:
                return Alignment.CENTER, xl + 2 * offset, 0.0, xr
            return Alignment.CENTER, xl, 0.0, xr + 2 * offset

        if n == 1:
            d_left = abs(first.x0 - xl)
            d_center = abs(first.center_x - region_center)
            d_right = abs(first.x1 - xr)
            # Строка почти во всю ширину — обычная строка текста слева.
            if first.x1 - first.x0 >= 0.8 * (xr - xl) and d_left <= 2 * tol:
                return Alignment.LEFT, (xl if d_left <= 1.0 else first.x0), 0.0, xr
            best = min(d_left, d_center, d_right)
            if d_left <= tol and d_left <= best + 0.5:
                return Alignment.LEFT, (xl if d_left <= 1.0 else first.x0), 0.0, xr
            if d_center <= 2 * tol and d_center <= best + 0.5:
                return centered_geometry(first.center_x)
            if d_right <= 2 * tol and first.x0 > xl + tol:
                return Alignment.RIGHT, xl, 0.0, first.x1
            return Alignment.LEFT, (xl if d_left <= 1.0 else first.x0), 0.0, xr

        rest = group[1:]
        lefts = [ln.x0 for ln in rest]
        left = min(lefts)
        # Допускаем одну "выбившуюся" строку в длинном абзаце (на сканах —
        # строка, часть которой скрыта печатью/подписью).
        misaligned = sum(1 for x in lefts if x - left > tol)
        left_aligned = (misaligned == 0 or (len(rest) >= 2 and misaligned == 1)) and abs(first.x0 - left) <= 6 * first.size
        rights = [ln.x1 for ln in group]
        rights_wrapped = [ln.x1 for ln, f in zip(group[:-1], forced) if not f]
        centers = [ln.center_x for ln in group]
        centered = max(centers) - min(centers) <= 1.5 * tol
        right_aligned = max(rights) - min(rights) <= tol

        if left_aligned and not (centered and abs(first.x0 - left) > tol):
            first_indent = first.x0 - left
            if abs(first_indent) <= tol * 0.5:
                first_indent = 0.0
            if rights_wrapped and max(rights_wrapped) - min(rights_wrapped) <= tol and max(rights_wrapped) >= xr - 2 * tol:
                return Alignment.JUSTIFY, left, first_indent, max(rights_wrapped)
            edge = xr
            if rights_wrapped and max(rights_wrapped) < xr - 3 * first.size:
                edge = max(rights_wrapped) + first.size
            return Alignment.LEFT, left, first_indent, edge
        # У текста по правому краю левая граница — граница потока, а не
        # начало самой длинной строки: иначе строка, чуть более широкая в
        # Word, чем в оригинале, перенеслась бы лишний раз.
        if right_aligned and not centered:
            return Alignment.RIGHT, xl, 0.0, max(rights)
        if centered:
            return centered_geometry(statistics.median(centers))
        if right_aligned:
            return Alignment.RIGHT, xl, 0.0, max(rights)
        return Alignment.LEFT, min(ln.x0 for ln in group), 0.0, xr


def _fit_char_spacing(words: list[LayoutWord], size: float) -> float:
    """Межбуквенный интервал, при котором текст абзаца, набранный Times New
    Roman выбранного кегля, займёт ту же ширину, что и в оригинале.

    Кегль округляется до типового, а шрифт оригинала может быть чуть уже
    или шире Times New Roman — без компенсации строки, заполненные в
    оригинале "до края", в Word не помещаются и переносятся иначе
    (последнее слово строки уезжает на следующую). Сравниваются только
    ширины слов, не пробелов — у выровненного по ширине текста пробелы
    растянуты."""
    src = pred = 0.0
    gaps = 0
    for w in words:
        if len(w.text) < 2:
            continue
        ink = font_metrics.ink_width_em(w.text, w.bold)
        if ink is None:
            continue
        src += w.x1 - w.x0
        pred += ink * w.size_pt
        gaps += len(w.text) - 1
    if gaps < 8 or pred <= 0:
        return 0.0
    c = (src - pred) / gaps
    c = max(-0.035 * size, min(0.035 * size, c))
    return round(c, 2) if abs(c) >= 0.03 else 0.0


# --------------------------------------------------------------------------
# Многоколоночные блоки
# --------------------------------------------------------------------------


@dataclass
class _ColumnBand:
    start: int
    end: int  # включительно
    gutter_x0: float
    gutter_x1: float


def _free_intervals(line: LayoutLine, lo: float, hi: float) -> list[tuple[float, float]]:
    spans = sorted((w.x0, w.x1) for w in line.words)
    free = []
    cursor = lo
    for a, b in spans:
        if a > cursor:
            free.append((cursor, min(a, hi)))
        cursor = max(cursor, b)
        if cursor >= hi:
            break
    if cursor < hi:
        free.append((cursor, hi))
    return [(a, b) for a, b in free if b > a]


def _intersect(a: list[tuple[float, float]], b: list[tuple[float, float]], min_w: float) -> list[tuple[float, float]]:
    out = []
    for a0, a1 in a:
        for b0, b1 in b:
            lo, hi = max(a0, b0), min(a1, b1)
            if hi - lo >= min_w:
                out.append((lo, hi))
    return out


def _find_column_bands(lines: list[LayoutLine], x_left: float, x_right: float, body_size: float) -> list[_ColumnBand]:
    """Ищет полосы строк, разделённые на две колонки общим вертикальным
    "коридором" пустого места (как блок реквизитов "Университет:" /
    "Профильная организация:").

    Ключевое наблюдение (см. docs/LIMITATIONS.md, предыдущие попытки):
    внутри полосы многие строки содержат текст только ОДНОЙ колонки, и
    разрыв внутри отдельной строки ничего не говорит о границе колонок.
    Здесь граница ищется иначе — как интервал по x, свободный от слов во
    ВСЕХ строках полосы одновременно (одноколоночная строка ему не
    мешает). Ложные срабатывания на "реках" пробелов выровненного по ширине
    текста и на широко разнесённых словах отсекаются требованиями:
    не меньше трёх строк с текстом по обе стороны коридора, обе колонки
    широкие (не меньше четверти ширины), и хотя бы одна из колонок
    выровнена по коридору (правая начинается от одной x или левая
    заканчивается на одной x). Область действия — только строки между
    первой и последней двусторонней строкой, никогда "до конца страницы".
    """
    width = x_right - x_left
    if width <= 0 or len(lines) < 3:
        return []
    lo = x_left + 0.2 * width
    hi = x_right - 0.2 * width
    min_gutter = max(4.0, 0.45 * body_size)
    bands: list[_ColumnBand] = []
    i = 0
    n = len(lines)
    while i < n:
        free = _free_intervals(lines[i], lo, hi)
        free = [(a, b) for a, b in free if b - a >= min_gutter]
        if not free:
            i += 1
            continue
        best: Optional[_ColumnBand] = None
        j = i
        cur = free
        while j + 1 < n:
            gap = lines[j + 1].y0 - lines[j].y1
            if gap > 2.5 * max(lines[j].size, body_size):
                break
            nxt = _intersect(cur, _free_intervals(lines[j + 1], lo, hi), min_gutter)
            if not nxt:
                break
            cur = nxt
            j += 1
            cand = _evaluate_band(lines, i, j, cur, x_left, x_right)
            if cand is not None:
                best = cand
        if best is not None:
            bands.append(best)
            i = best.end + 1
        else:
            i += 1
    return bands


def _evaluate_band(lines, i, j, intervals, x_left, x_right) -> Optional[_ColumnBand]:
    width = x_right - x_left
    best = None
    for g0, g1 in intervals:
        two_sided = []
        for k in range(i, j + 1):
            left = [w for w in lines[k].words if w.x1 <= g0 + 0.5]
            right = [w for w in lines[k].words if w.x0 >= g1 - 0.5]
            if left and right:
                two_sided.append((k, left, right))
        if len(two_sided) < 3:
            continue
        start, end = two_sided[0][0], two_sided[-1][0]
        if len(two_sided) < 0.5 * (end - start + 1):
            continue
        band_lines = lines[start : end + 1]
        left_words = [w for ln in band_lines for w in ln.words if w.x1 <= g0 + 0.5]
        right_words = [w for ln in band_lines for w in ln.words if w.x0 >= g1 - 0.5]
        left_extent = max(w.x1 for w in left_words) - min(w.x0 for w in left_words)
        right_extent = max(w.x1 for w in right_words) - min(w.x0 for w in right_words)
        if left_extent < 0.25 * width or right_extent < 0.25 * width:
            continue
        if statistics.median(len(l) for _, l, _ in two_sided) < 2 or statistics.median(len(r) for _, _, r in two_sided) < 2:
            continue
        size = statistics.median(ln.size for ln in band_lines)
        tol = max(2.5, 0.5 * size)
        right_starts = [min(w.x0 for w in r) for _, _, r in two_sided]
        left_ends = [max(w.x1 for w in l) for _, l, _ in two_sided]
        rs_med, le_med = statistics.median(right_starts), statistics.median(left_ends)
        rs_aligned = sum(1 for v in right_starts if abs(v - rs_med) <= tol) >= 0.6 * len(two_sided)
        le_aligned = sum(1 for v in left_ends if abs(v - le_med) <= tol) >= 0.6 * len(two_sided)
        if not (rs_aligned or le_aligned):
            continue
        # Границы полосы — по строкам, которые действительно "опираются" на
        # коридор (правая часть начинается от него или левая заканчивается
        # у него). Строка обычного абзаца, у которой растянутый пробел
        # случайно совпал с коридором, в полосу не попадает.
        consistent = [
            k
            for (k, _, _), rs, le in zip(two_sided, right_starts, left_ends)
            if (rs_aligned and abs(rs - rs_med) <= tol) or (le_aligned and abs(le - le_med) <= tol)
        ]
        if len(consistent) < 3:
            continue
        start, end = consistent[0], consistent[-1]

        # Соседние строки, которые явно принадлежат колонкам, но не
        # "опираются" на коридор (подпись "Руководитель" с отступом, строка
        # подписей "____ А.Ю. Коковихин      ____ /Хрипунов И.А./"):
        # текст целиком по одну сторону коридора или по обе, но с широким
        # промежутком. У строки обычного абзаца, чей пробел случайно
        # совпал с коридором, промежуток — обычный растянутый пробел.
        def belongs(k: int) -> bool:
            left = [w for w in lines[k].words if w.x1 <= g0 + 0.5]
            right = [w for w in lines[k].words if w.x0 >= g1 - 0.5]
            if len(left) + len(right) != len(lines[k].words):
                return False
            if left and right:
                return min(w.x0 for w in right) - max(w.x1 for w in left) >= max(20.0, 1.8 * size)
            return True

        def near(a: int, b: int) -> bool:
            return abs(lines[b].y0 - lines[a].y1) <= 2.5 * size

        while end + 1 <= j and near(end, end + 1) and belongs(end + 1):
            end += 1
        while end + 1 < len(lines) and near(end, end + 1) and belongs(end + 1) and _fits_gutter(lines[end + 1], g0, g1):
            end += 1
        cand = _ColumnBand(start, end, g0, g1)
        if best is None or (cand.end - cand.start) > (best.end - best.start):
            best = cand
    return best


def _fits_gutter(line: LayoutLine, g0: float, g1: float) -> bool:
    return not any(w.x0 < g1 - 0.5 and w.x1 > g0 + 0.5 for w in line.words)


def _split_line(line: LayoutLine, g0: float, g1: float) -> tuple[Optional[LayoutLine], Optional[LayoutLine]]:
    mid = (g0 + g1) / 2
    left = [w for w in line.words if (w.x0 + w.x1) / 2 < mid]
    right = [w for w in line.words if (w.x0 + w.x1) / 2 >= mid]
    return (LayoutLine(left) if left else None, LayoutLine(right) if right else None)


def _band_to_table(
    band_lines: list[LayoutLine], band: _ColumnBand, x_left: float, x_right: float, body_size: float, fit_spacing: bool
) -> Table:
    left_lines, right_lines = [], []
    for ln in band_lines:
        a, b = _split_line(ln, band.gutter_x0, band.gutter_x1)
        if a:
            left_lines.append(a)
        if b:
            right_lines.append(b)
    col_split = band.gutter_x1 - 0.5  # правая колонка начинается от коридора
    left_bounds = (x_left, max(ln.x1 for ln in left_lines) if left_lines else band.gutter_x0)
    right_bounds = (
        min(ln.x0 for ln in right_lines) if right_lines else band.gutter_x1,
        max(ln.x1 for ln in right_lines) if right_lines else x_right,
    )
    cells = []
    for col_lines, (xl, xr) in ((left_lines, left_bounds), (right_lines, right_bounds)):
        builder = _ParagraphBuilder(xl, xr, body_size, fit_spacing=fit_spacing)
        blocks = builder.build(sorted(col_lines, key=lambda ln: ln.y0)) if col_lines else []
        cells.append(TableCell(blocks=blocks))
    y0 = min(ln.y0 for ln in band_lines)
    y1 = max(ln.y1 for ln in band_lines)
    right_end = max(x_right, right_bounds[1])
    cells[0].bbox = BBox(x_left, y0, col_split, y1)
    cells[1].bbox = BBox(col_split, y0, right_end, y1)
    return Table(
        rows=[TableRow(cells=cells)],
        col_widths_pt=[col_split - x_left, right_end - col_split],
        bbox=BBox(x_left, y0, right_end, y1),
        source="layout",
        borderless=True,
    )


# --------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------


def content_bounds(lines: list[LayoutLine], page_width: float) -> tuple[float, float]:
    """Левая и правая границы основного текста страницы (не края страницы)."""
    lefts = [ln.x0 for ln in lines]
    x_left = _percentile(lefts, 0.1)
    wide = [ln.x1 for ln in lines if ln.x1 > page_width * 0.5]
    x_right = _percentile(wide, 0.9) if wide else page_width - x_left
    if x_right <= x_left + 36:
        x_right = page_width - x_left
    return x_left, x_right


def build_blocks(
    words: list[LayoutWord],
    page_width: float,
    page_height: float,
    paragraph_gap_factor: float = 1.6,
    bounds: Optional[tuple[float, float]] = None,
    detect_columns: bool = True,
    fit_spacing: bool = False,
    drop_symbol_lines: bool = False,
) -> list[object]:
    """Основная функция: слова страницы → список Block (Heading/Paragraph/
    ListItem, а для двухколоночных фрагментов — Table(borderless=True))."""
    lines = words_to_lines(words)
    if drop_symbol_lines:
        # Строки из одного-двух знаков без букв и цифр ("—", ".") на скане —
        # обрывки линий и штрихов, а не текст.
        lines = [
            ln for ln in lines
            if any(ch.isalnum() for ch in ln.text) or len(ln.text.replace(" ", "")) > 3
        ]
    if not lines:
        return []

    # Медиану "обычного" размера текста считаем по строкам минимум с двумя
    # словами — короткие/шумные строки (одно слово, особенно артефакт OCR)
    # слишком волатильны и могут исказить оценку типичного размера шрифта.
    multi_word_lines = [ln for ln in lines if len(ln.words) >= 2]
    body_size = statistics.median([ln.size for ln in (multi_word_lines or lines)])
    x_left, x_right = bounds if bounds else content_bounds(lines, page_width)

    bands = _find_column_bands(lines, x_left, x_right, body_size) if detect_columns else []
    builder = _ParagraphBuilder(x_left, x_right, body_size, fit_spacing=fit_spacing)
    blocks: list[object] = []
    prev_bottom: Optional[float] = None
    idx = 0
    for band in bands + [None]:
        stop = band.start if band else len(lines)
        run = lines[idx:stop]
        if run:
            new_blocks = builder.build(run, prev_bottom)
            if new_blocks and prev_bottom is None:
                new_blocks[0].space_before_pt = 0.0
            blocks.extend(new_blocks)
            prev_bottom = run[-1].y1
        if band is None:
            break
        band_lines = lines[band.start : band.end + 1]
        blocks.append(_band_to_table(band_lines, band, x_left, x_right, body_size, fit_spacing))
        prev_bottom = max(ln.y1 for ln in band_lines)
        idx = band.end + 1
    return blocks


def build_cell_blocks(
    words: list[LayoutWord], cell_bbox: BBox, padding: float = 1.0, fit_spacing: bool = False
) -> list[object]:
    """Абзацы внутри ячейки таблицы: те же правила, что и для страницы, но
    границы потока — внутренние границы ячейки."""
    lines = words_to_lines(words)
    if not lines:
        return []
    body_size = statistics.median(ln.size for ln in lines)
    # В узких ячейках таблиц разбиение на строки воспроизводится явно:
    # перенос "по ширине" слишком чувствителен к полям ячейки и ширине
    # шрифта — одно лишнее слово в строке меняет вид всей ячейки.
    builder = _ParagraphBuilder(
        cell_bbox.x0 + padding,
        cell_bbox.x1 - padding,
        body_size,
        allow_headings=False,
        fit_spacing=fit_spacing,
        keep_line_breaks=True,
    )
    return builder.build(lines)
