"""Отделение цветных отметок (печатей, подписей) от напечатанного текста.

На сканах официальных документов печати и подписи почти всегда выполнены
цветными (синими/фиолетовыми) чернилами поверх чёрного напечатанного
текста. Если отдать такой растр OCR как есть, Tesseract "читает" росчерк
и кольцо печати как поток мусорных символов, а напечатанный текст под
печатью распознаётся хуже. Поэтому цветные отметки:

1. вырезаются в отдельные PNG с прозрачным фоном (сохраняется вид
   оригинала — печать и подпись в DOCX стоят там же, где на скане);
2. стираются из растра, который уходит в OCR (чёрный текст под печатью
   при этом остаётся — он не цветной).

Цветной напечатанный текст (синие гиперссылки, красные заголовки) не
должен приниматься за печать: такие кандидаты проверяются OCR и
отбрасываются, если уверенно читаются как обычный текст.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger("bes_ocr")


@dataclass
class ColorMark:
    x0: int
    y0: int
    x1: int
    y1: int
    png_rgba: bytes


def _ink_mask(image_bgr: np.ndarray, loose: bool = False) -> np.ndarray:
    """Маска цветных чернил. Строгая — для поиска отметок (без ложных
    срабатываний на цветных ореолах JPEG вокруг чёрного текста); мягкая —
    для стирания/вырезания уже найденной отметки целиком, включая бледные
    края оттиска и тёмные участки подписи."""
    hsv = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2HSV)
    s = hsv[:, :, 1].astype(np.int16)
    v = hsv[:, :, 2].astype(np.int16)
    b, g, r = (image_bgr[:, :, i].astype(np.int16) for i in range(3))
    chroma = np.maximum(np.maximum(b, g), r) - np.minimum(np.minimum(b, g), r)
    if loose:
        mask = (chroma > 22) & (s > 35) & (v > 30)
    else:
        # Насыщенный и не слишком тёмный пиксель (у почти чёрных пикселей
        # насыщенность в HSV шумная) с заметной абсолютной цветностью.
        mask = (s > 70) & (v > 60) & (chroma > 40)
    mask = mask.astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    return mask


def _looks_like_text(mask_crop: np.ndarray, ocr_lang: str) -> bool:
    """Цветной фрагмент, который уверенно читается как строки текста, —
    это напечатанный цветной текст, а не печать/подпись."""
    try:
        import pytesseract
    except ImportError:  # pragma: no cover
        return False
    img = 255 - mask_crop
    data = pytesseract.image_to_data(img, lang=ocr_lang, output_type=pytesseract.Output.DICT, config="--psm 6")
    area = float(mask_crop.shape[0] * mask_crop.shape[1]) or 1.0
    covered = 0.0
    good = 0
    for text, conf, w, h in zip(data["text"], data["conf"], data["width"], data["height"]):
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        t = (text or "").strip()
        if len(t) >= 3 and c >= 80 and sum(ch.isalpha() for ch in t) >= 3:
            good += 1
            covered += w * h
    return good >= 3 and covered / area >= 0.35


def extract_color_marks(
    image_bgr: np.ndarray, dpi: float, ocr_lang: str = "rus+eng"
) -> tuple[np.ndarray, list[ColorMark]]:
    """Возвращает (растр без цветных отметок, список отметок)."""
    mask = _ink_mask(image_bgr)
    if int(np.count_nonzero(mask)) < dpi * dpi * 0.002:
        return image_bgr, []

    # Склеиваем штрихи одной отметки (кольцо печати, буквы внутри неё,
    # отдельные росчерки подписи) в единую компоненту.
    join = max(3, int(dpi * 0.08))
    merged = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (join, join)))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(merged, connectivity=8)

    min_side = dpi * 0.3
    marks: list[ColorMark] = []
    cleaned = image_bgr.copy()
    h_img, w_img = mask.shape
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if w < min_side or h < min_side * 0.6:
            continue
        component = (labels[y : y + h, x : x + w] == i)
        ink = mask[y : y + h, x : x + w] & (component.astype(np.uint8) * 255)
        if int(np.count_nonzero(ink)) < dpi * dpi * 0.002:
            continue
        if _looks_like_text(ink, ocr_lang):
            continue

        pad = int(dpi * 0.02)
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(w_img, x + w + pad), min(h_img, y + h + pad)
        crop = image_bgr[y0:y1, x0:x1]
        # Внутри найденной области берём мягкую маску, ограниченную
        # окрестностью строгих штрихов этой отметки.
        near = np.zeros(mask.shape, np.uint8)
        near[y : y + h, x : x + w] = ink
        near = cv2.dilate(near[y0:y1, x0:x1], np.ones((join, join), np.uint8))
        comp_mask = _ink_mask(crop, loose=True) & near
        # Мягкая альфа: полностью непрозрачные штрихи, полупрозрачный
        # край — без "ореола" от фона бумаги.
        alpha = cv2.GaussianBlur(cv2.dilate(comp_mask, np.ones((2, 2), np.uint8)), (3, 3), 0)
        rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = alpha
        ok, buf = cv2.imencode(".png", rgba)
        if not ok:
            continue
        marks.append(ColorMark(x0, y0, x1, y1, buf.tobytes()))

        # Стираем цветные пиксели из растра для OCR (с небольшим запасом,
        # чтобы не остался цветной контур). Чёрный текст под печатью
        # не входит в маску и остаётся.
        erase = cv2.dilate(comp_mask, np.ones((3, 3), np.uint8)) > 0
        region = cleaned[y0:y1, x0:x1]
        region[erase] = 255

    if marks:
        logger.info("Найдено цветных отметок (печати/подписи): %d", len(marks))
    return cleaned, marks
