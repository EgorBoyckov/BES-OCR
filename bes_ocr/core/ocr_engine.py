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


def deskew(image_bgr: np.ndarray) -> np.ndarray:
    """Определяет и исправляет небольшой перекос скана."""
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.bitwise_not(gray)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(thresh)
    if coords is None or len(coords) < 50:
        return image_bgr
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = -(90 + angle)
    else:
        angle = -angle
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

    def recognize_cell_text(self, image_bgr: np.ndarray) -> str:
        """Распознаёт содержимое одной ячейки таблицы (короткий текст)."""
        image = preprocess_for_ocr(image_bgr)
        text = pytesseract.image_to_string(
            image, lang=self.settings.ocr_languages, config="--psm 6"
        )
        return text.strip()

    def is_likely_handwriting_or_noise(self, words: list[OcrWord]) -> bool:
        """Эвристика п.3 ТЗ: рукописный/нераспознаваемый текст → не плодить мусор."""
        if not words:
            return False
        low_conf = [w for w in words if w.confidence < self.settings.handwriting_confidence_threshold]
        ratio = len(low_conf) / len(words)
        return ratio >= self.settings.handwriting_garbage_word_ratio
