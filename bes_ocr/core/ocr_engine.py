"""Локальный OCR-движок на базе Tesseract.

Препроцессинг (OpenCV) + pytesseract.image_to_data для получения слов с
bbox и confidence — нужно и для layout-анализа, и для распознавания текста
внутри ячеек таблиц.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
import pytesseract

from ..config.settings import Settings


@dataclass
class OcrWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float
    line_id: int


def estimate_skew_angle(image_bgr: np.ndarray) -> float:
    """Оценивает угол перекоса скана по протяжённым почти горизонтальным
    отрезкам (текстовые строки, линии таблиц) через преобразование Хафа.

    minAreaRect по всем ненулевым пикселям страницы (более простой и ранее
    использовавшийся подход) на практике нечувствителен к типичному
    небольшому перекосу скана (доли градуса — единицы градусов): угол
    описывающего прямоугольника всей страницы определяется общей формой
    страницы, а не тонким наклоном строк/линий, и часто округляется до 0
    даже при заметном на таблице перекосе. Хаф по конкретным отрезкам даёт
    устойчивую оценку в таких случаях.
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 25, 10
    )
    h, w = binary.shape
    lines = cv2.HoughLinesP(
        binary, 1, np.pi / 1440, threshold=200, minLineLength=int(w * 0.15), maxLineGap=20
    )
    if lines is None:
        return 0.0
    angles = []
    weights = []
    for x1, y1, x2, y2 in lines.reshape(-1, 4):
        angle = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(angle) < 10:
            angles.append(angle)
            weights.append(np.hypot(x2 - x1, y2 - y1))
    if not angles:
        return 0.0
    return float(np.average(angles, weights=weights))


def deskew(image_bgr: np.ndarray) -> np.ndarray:
    """Определяет и исправляет небольшой перекос скана."""
    angle = estimate_skew_angle(image_bgr)
    if abs(angle) < 0.1 or abs(angle) > 15:
        # Не трогаем: либо уже ровно, либо это, вероятно, ошибка детекции угла.
        return image_bgr
    (h, w) = image_bgr.shape[:2]
    center = (w // 2, h // 2)
    matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
    return cv2.warpAffine(
        image_bgr, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def binarize(image_bgr: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.fastNlMeansDenoising(gray, h=7)
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15
    )
    return thresh


def preprocess_for_ocr(image_bgr: np.ndarray) -> np.ndarray:
    return binarize(deskew(image_bgr))


class OcrEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def recognize_words(self, image_bgr: np.ndarray, preprocess: bool = True) -> list[OcrWord]:
        image = preprocess_for_ocr(image_bgr) if preprocess else image_bgr
        data = pytesseract.image_to_data(
            image,
            lang=self.settings.ocr_languages,
            output_type=pytesseract.Output.DICT,
            config="--psm 3",
        )
        words: list[OcrWord] = []
        n = len(data["text"])
        line_counter = 0
        seen_lines: dict[tuple, int] = {}
        for i in range(n):
            text = data["text"][i].strip()
            if not text:
                continue
            conf_raw = data["conf"][i]
            try:
                conf = float(conf_raw)
            except (TypeError, ValueError):
                conf = -1.0
            if conf < 0:
                continue
            line_key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            if line_key not in seen_lines:
                seen_lines[line_key] = line_counter
                line_counter += 1
            x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
            words.append(
                OcrWord(
                    text=text,
                    x0=float(x),
                    y0=float(y),
                    x1=float(x + w),
                    y1=float(y + h),
                    confidence=conf,
                    line_id=seen_lines[line_key],
                )
            )
        return words

    def is_likely_handwriting_or_noise(self, words: list[OcrWord]) -> bool:
        """Эвристика п.3 ТЗ: рукописный/нераспознаваемый текст → не плодить мусор."""
        if not words:
            return False
        low_conf = [w for w in words if w.confidence < self.settings.handwriting_confidence_threshold]
        ratio = len(low_conf) / len(words)
        return ratio >= self.settings.handwriting_garbage_word_ratio
