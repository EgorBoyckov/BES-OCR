"""Оценка качества текстового слоя страницы PDF.

Решает, можно ли доверять встроенному тексту, или нужен OCR
(п.2 ТЗ: "если текстового слоя нет, он повреждён или недостаточен —
использовать локальный OCR").
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config.settings import Settings
from .pdf_source import PdfSource

_GARBAGE_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f�]")
_ALNUM_RE = re.compile(r"[^\W\d_]", re.UNICODE)


@dataclass
class TextLayerAssessment:
    usable: bool
    char_count: int
    garbage_ratio: float
    reason: str


def assess_text_layer(pdf: PdfSource, page_number: int, settings: Settings) -> TextLayerAssessment:
    raw = pdf.raw_text(page_number)
    stripped = raw.strip()
    char_count = len(stripped)

    if char_count < settings.min_text_layer_chars_per_page:
        return TextLayerAssessment(False, char_count, 1.0, "слишком мало текста")

    garbage_chars = len(_GARBAGE_RE.findall(raw))
    alnum_chars = len(_ALNUM_RE.findall(raw))
    garbage_ratio = garbage_chars / max(1, char_count)

    # Доля "букв" против общего числа непробельных символов — если почти нет
    # букв (только мусор/спецсимволы), слой считается непригодным.
    non_space = len(re.sub(r"\s", "", raw))
    alpha_ratio = alnum_chars / max(1, non_space)

    if garbage_ratio > settings.min_text_layer_garbage_ratio:
        return TextLayerAssessment(False, char_count, garbage_ratio, "высокая доля повреждённых символов")

    if alpha_ratio < 0.2:
        return TextLayerAssessment(False, char_count, garbage_ratio, "текст не содержит достаточно букв")

    return TextLayerAssessment(True, char_count, garbage_ratio, "текстовый слой пригоден")
