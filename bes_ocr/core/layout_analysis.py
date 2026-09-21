"""Анализ разметки страницы: слова → строки → параграфы/заголовки/списки.

Работает над унифицированным представлением слов (LayoutWord), общим для
текстового слоя PDF и результатов OCR — это единственное место, где
принимается решение о структуре текста, независимо от его источника.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .models import BBox, Alignment, Heading, ListItem, Paragraph, Run

_ORDERED_RE = re.compile(r"^\s*(\d{1,3}|[a-zA-Zа-яА-Я])[.)]\s+")
_BULLET_RE = re.compile(r"^\s*[•\-•‣◦⁃*]\s+")


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
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def median_size(self) -> float:
        return statistics.median([w.size_pt for w in self.words]) if self.words else 11.0


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


def _alignment_for_line(line: LayoutLine, page_width: float) -> Alignment:
    left_margin = line.x0
    right_margin = page_width - line.x1
    center_offset = abs(left_margin - right_margin)
    if center_offset < page_width * 0.04 and left_margin > page_width * 0.08:
        return Alignment.CENTER
    if right_margin < page_width * 0.03 and left_margin > page_width * 0.15:
        return Alignment.RIGHT
    return Alignment.LEFT


def _line_to_paragraph_runs(line: LayoutLine) -> list[Run]:
    runs: list[Run] = []
    for i, w in enumerate(line.words):
        if runs and runs[-1].bold == w.bold and runs[-1].italic == w.italic and abs(
            runs[-1].size_pt - w.size_pt
        ) < 0.5:
            runs[-1].text += " " + w.text
        else:
            text = (" " if i > 0 else "") + w.text
            runs.append(Run(text=text, bold=w.bold, italic=w.italic, size_pt=w.size_pt, font_name=w.font_name))
    return runs


def build_blocks(
    words: list[LayoutWord],
    page_width: float,
    page_height: float,
    paragraph_gap_factor: float = 1.6,
) -> list[object]:
    """Основная функция: слова страницы → список Block (Heading/Paragraph/ListItem)."""
    lines = words_to_lines(words)
    if not lines:
        return []

    # Медиану "обычного" размера текста считаем по строкам минимум с двумя
    # словами — короткие/шумные строки (одно слово, особенно артефакт OCR)
    # слишком волатильны и могут исказить оценку типичного размера шрифта.
    multi_word_lines = [ln for ln in lines if len(ln.words) >= 2]
    body_size = statistics.median([ln.median_size for ln in (multi_word_lines or lines)])
    blocks: list[object] = []
    prev_line: LayoutLine | None = None

    for line in lines:
        gap = (line.y0 - prev_line.y1) if prev_line else 0.0
        typical_line_height = max(line.y1 - line.y0, 1.0)
        new_paragraph = prev_line is None or gap > typical_line_height * paragraph_gap_factor * 0.5

        text = line.text.strip()
        alignment = _alignment_for_line(line, page_width)
        runs = _line_to_paragraph_runs(line)

        # Заголовком считаем только достаточно длинную по смыслу строку
        # (не менее 2 слов или одно длинное слово) с заметно (не на глаз)
        # более крупным шрифтом — единичное короткое "слово" на OCR-строке
        # часто оказывается артефактом распознавания с завышенной оценкой
        # размера (из-за высоты случайного штриха/засечки), а не заголовком.
        heading_candidate = len(line.words) >= 2 or (len(line.words) == 1 and len(text) >= 4)
        is_heading = heading_candidate and line.median_size > body_size * 1.25 and len(text) < 140
        ordered_match = _ORDERED_RE.match(text)
        bullet_match = _BULLET_RE.match(text)

        line_bbox = BBox(line.x0, line.y0, line.x1, line.y1)

        if is_heading:
            level = 1 if line.median_size > body_size * 1.45 else 2
            blocks.append(Heading(runs=runs, alignment=alignment, level=level, bbox=line_bbox))
        elif ordered_match or bullet_match:
            marker = ordered_match.group(0).strip() if ordered_match else bullet_match.group(0).strip()
            clean_text = text[len(ordered_match.group(0)) :] if ordered_match else text[len(bullet_match.group(0)) :]
            item_runs = [Run(text=clean_text, size_pt=line.median_size)]
            blocks.append(
                ListItem(
                    runs=item_runs,
                    alignment=alignment,
                    ordered=bool(ordered_match),
                    marker=marker,
                    indent_pt=line.x0,
                    bbox=line_bbox,
                )
            )
        elif new_paragraph or not blocks or not isinstance(blocks[-1], Paragraph) or isinstance(blocks[-1], (Heading, ListItem)):
            blocks.append(Paragraph(runs=runs, alignment=alignment, indent_pt=line.x0, bbox=line_bbox))
        else:
            last = blocks[-1]
            last.runs.append(Run(text=" " + line.text, size_pt=line.median_size))
            if last.bbox is not None:
                last.bbox = BBox(
                    min(last.bbox.x0, line_bbox.x0),
                    last.bbox.y0,
                    max(last.bbox.x1, line_bbox.x1),
                    line_bbox.y1,
                )

        prev_line = line

    return blocks
