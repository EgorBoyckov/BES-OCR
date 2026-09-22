"""Построение "слов" с реалистичной геометрией (по метрикам Times New Roman)
для модульных тестов разметки — без PDF и OCR."""
from __future__ import annotations

from bes_ocr.core import font_metrics
from bes_ocr.core.layout_analysis import LayoutWord


def word_at(text: str, x0: float, baseline: float, size: float, bold: bool = False) -> LayoutWord:
    top, bottom = font_metrics.vertical_extent_em(text) or (0.66, 0.0)
    width = (font_metrics.ink_width_em(text, bold) or 0.5 * len(text)) * size
    return LayoutWord(
        text=text,
        x0=x0,
        y0=baseline - top * size,
        x1=x0 + width,
        y1=baseline + bottom * size,
        size_pt=size,
        bold=bold,
        baseline=baseline,
    )


def line_at(text: str, x0: float, baseline: float, size: float = 12.0, bold: bool = False) -> list[LayoutWord]:
    words = []
    x = x0
    for token in text.split():
        w = word_at(token, x, baseline, size, bold)
        words.append(w)
        adv = (font_metrics.advance_em(token, bold) or 0.5 * len(token)) * size
        x += adv + font_metrics.SPACE_ADVANCE_EM * size
    return words


def line_right(text: str, x1: float, baseline: float, size: float = 12.0, bold: bool = False) -> list[LayoutWord]:
    words = line_at(text, 0.0, baseline, size, bold)
    shift = x1 - words[-1].x1
    for w in words:
        w.x0 += shift
        w.x1 += shift
    return words


def line_center(text: str, center: float, baseline: float, size: float = 12.0, bold: bool = False) -> list[LayoutWord]:
    words = line_at(text, 0.0, baseline, size, bold)
    shift = center - (words[0].x0 + words[-1].x1) / 2
    for w in words:
        w.x0 += shift
        w.x1 += shift
    return words


def justified_line(text: str, x0: float, x1: float, baseline: float, size: float = 12.0) -> list[LayoutWord]:
    """Строка, растянутая по ширине [x0, x1] (как у выравнивания по ширине)."""
    words = line_at(text, x0, baseline, size)
    extra = (x1 - words[-1].x1) / max(1, len(words) - 1)
    for i, w in enumerate(words):
        w.x0 += extra * i
        w.x1 += extra * i
    return words
