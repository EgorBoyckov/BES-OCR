"""Локальный OCR-движок на базе Tesseract.

Препроцессинг (OpenCV) + pytesseract.image_to_data для получения слов с
bbox и confidence — нужно и для layout-анализа, и для распознавания текста
внутри ячеек таблиц.
"""
from __future__ import annotations

import re
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
    """Бинаризация перед OCR.

    Раньше здесь был `fastNlMeansDenoising` + `adaptiveThreshold` (локальный
    порог) — рассчитано на неровное освещение (тени, блики), но на
    реальном документе (плоский планшетный скан) заметно проигрывало
    простому глобальному порогу Отсу: ниже средняя уверенность Tesseract
    (82.8 против 87.6 на "трудной" странице), и предлог "от" распознавался
    как "OT" (заглавные латинские) вместо корректного "от" — измерено
    напрямую на реальном сканированном документе проекта. Оставлен лёгкий
    `GaussianBlur` вместо тяжёлого `fastNlMeansDenoising` — сглаживает
    шум скана почти без потери резкости краёв символов и заметно быстрее.
    """
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return thresh


def preprocess_for_ocr(image_bgr: np.ndarray, do_deskew: bool = True) -> np.ndarray:
    return binarize(deskew(image_bgr) if do_deskew else image_bgr)


def stroke_width_px(binary: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> float | None:
    """Типичная толщина штриха символов внутри рамки (в пикселях) —
    2·площадь / периметр чернил (для штриха ширины w и длины L площадь
    w·L, периметр ≈ 2L). Используется для распознавания полужирного
    начертания: у OCR нет информации о шрифте, но полужирный текст на
    скане заметно "толще" обычного того же кегля.

    Оценка непрерывная, в отличие от медианы карты расстояний до фона:
    та на мелком тексте принимает лишь несколько дискретных значений
    (шаг ~0.5 пикселя ≈ 10–15% толщины штриха), и обычная строка
    случайно попадала в "полужирные"."""
    h, w = binary.shape[:2]
    xa, ya, xb, yb = max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))
    if xb - xa < 3 or yb - ya < 3:
        return None
    ink = (binary[ya:yb, xa:xb] == 0).astype(np.uint8)
    area = int(ink.sum())
    if area < 15:
        return None
    contours, _ = cv2.findContours(ink, cv2.RETR_LIST, cv2.CHAIN_APPROX_NONE)
    perimeter = sum(cv2.arcLength(c, True) for c in contours)
    if perimeter <= 0:
        return None
    return float(2.0 * area / perimeter)


class OcrEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def recognize_words(
        self, image_bgr: np.ndarray, preprocess: bool = True, do_deskew: bool = True, psm: int = 3
    ) -> list[OcrWord]:
        image = preprocess_for_ocr(image_bgr, do_deskew=do_deskew) if preprocess else image_bgr
        data = pytesseract.image_to_data(
            image,
            lang=self.settings.ocr_languages,
            output_type=pytesseract.Output.DICT,
            config=f"--psm {psm}",
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

    def recognize_region(
        self, binary: np.ndarray, x0: int, y0: int, x1: int, y1: int, inset: int = 4
    ) -> list[OcrWord]:
        """Распознаёт прямоугольную область (обычно ячейку таблицы) уже
        бинаризованного растра отдельно от остальной страницы.

        Сегментация страницы целиком (psm 3) в плотных таблицах регулярно
        теряет содержимое узких ячеек ("№", номера строк) и склеивает слова
        соседних ячеек через линию границы; распознавание каждой ячейки как
        самостоятельного блока (psm 6) этих проблем не имеет. Координаты
        слов возвращаются в системе координат всей страницы.
        """
        h, w = binary.shape[:2]
        xa, ya = max(0, x0 + inset), max(0, y0 + inset)
        xb, yb = min(w, x1 - inset), min(h, y1 - inset)
        if xb - xa < 4 or yb - ya < 4:
            return []
        crop = binary[ya:yb, xa:xb]
        if int(np.count_nonzero(crop == 0)) < 12:
            return []  # пустая ячейка
        pad = 12
        padded = cv2.copyMakeBorder(crop, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        words = self.recognize_words(padded, preprocess=False, psm=6)
        if not words:
            words = self.recognize_words(padded, preprocess=False, psm=7)
        if not words:
            # Одиночный символ (номер строки в узкой графе "№").
            words = self.recognize_words(padded, preprocess=False, psm=10)
        for word in words:
            word.x0 += xa - pad
            word.x1 += xa - pad
            word.y0 += ya - pad
            word.y1 += ya - pad
        return words

    def is_likely_handwriting_or_noise(self, words: list[OcrWord]) -> bool:
        """Эвристика п.3 ТЗ: рукописный/нераспознаваемый текст → не плодить мусор."""
        if not words:
            return False
        low_conf = [w for w in words if w.confidence < self.settings.handwriting_confidence_threshold]
        ratio = len(low_conf) / len(words)
        return ratio >= self.settings.handwriting_garbage_word_ratio


_PLAUSIBLE_WORD_RE = re.compile(
    r"^[«\"'(\[№]?("
    r"[А-ЯЁа-яё]{3,}(-[А-ЯЁа-яё]+)*"
    r"|[A-Za-z]{3,}(-[A-Za-z]+)*"
    r"|\d+([.,:/-]\d+)*"
    r")[»\"')\].,:;!?№]{0,3}$"
)


_EMAIL_OR_URL_RE = re.compile(
    r"^[\w.+-]+@[\w-]+(\.[\w-]+)+[.,;]?$|^(https?://|www\.)[\w./-]+[.,;]?$", re.IGNORECASE
)


def is_plausible_word(text: str) -> bool:
    """Похоже ли распознанное слово на обычное слово/число, а не на
    обрывок штриха. Уверенность Tesseract для целого слова откалибрована
    плохо: на реальных сканах правильно прочитанные слова нередко получают
    20–30%, и отбрасывать их только по порогу — значит терять текст."""
    text = text.strip()
    if _EMAIL_OR_URL_RE.match(text):
        return True
    if not _PLAUSIBLE_WORD_RE.match(text):
        return False
    letters = [ch for ch in text if ch.isalpha()]
    # Слово из смеси заглавных и строчных посередине ("ПрОфИль") — типичный мусор.
    if len(letters) >= 3 and any(ch.isupper() for ch in letters[1:]) and any(ch.islower() for ch in letters[1:]):
        return False
    return True


def _cluster_words_by_proximity(words: list[OcrWord], gap: float) -> list[list[OcrWord]]:
    """Группирует слова по близости bbox (union-find на пересечении
    расширенных на gap прямоугольников). Использовано для поиска локальных
    "пятен визуального шума" (подписи, печати) — п.3 ТЗ: результат должен
    оставаться локальным (одна строка/несколько соседних слов), поэтому gap
    заведомо меньше типичного межабзацного отступа."""
    n = len(words)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    boxes = [(w.x0 - gap, w.y0 - gap, w.x1 + gap, w.y1 + gap) for w in words]
    for i in range(n):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 1, n):
            bx0, by0, bx1, by1 = boxes[j]
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                union(i, j)

    groups: dict[int, list[OcrWord]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(words[i])
    return list(groups.values())


def find_noise_regions(
    words: list[OcrWord], settings: Settings, page_width: float, page_height: float
) -> list[tuple[float, float, float, float]]:
    """Находит локальные области скана, похожие на подпись/печать/помарку,
    а не на печатный текст — п.3 ТЗ требует сохранять такие участки как
    изображение, а не порождать поток ошибочных символов.

    Важное наблюдение с реального документа: доверие (confidence) отдельного
    слова здесь ненадёжно само по себе — Tesseract часто уверенно (60-90%)
    "читает" мусорные штрихи росчерка подписи как короткие псевдослова
    вперемешку с настоящим напечатанным текстом той же строки (ФИО
    подписанта), поэтому пороговая фильтрация по confidence одного слова
    такую область не находит. Вместо этого ищем плотные локальные скопления
    коротких/невнятных слов: печатный текст почти всегда состоит из слов
    нормальной длины, а печать/подпись — из обрывков.
    """
    clusters = _cluster_words_by_proximity(words, gap=5.0)
    page_area = max(page_width * page_height, 1.0)
    regions: list[tuple[float, float, float, float]] = []
    for cluster in clusters:
        if len(cluster) < 6:
            continue
        suspect = [
            w
            for w in cluster
            if w.confidence < 60.0 or (len(w.text.strip()) <= 2 and w.confidence < 85.0)
        ]
        if len(suspect) / len(cluster) < 0.4:
            continue
        x0 = min(w.x0 for w in cluster)
        y0 = min(w.y0 for w in cluster)
        x1 = max(w.x1 for w in cluster)
        y1 = max(w.y1 for w in cluster)
        if (x1 - x0) * (y1 - y0) > 0.05 * page_area:
            continue  # похоже на обычный плотный абзац текста, а не на пятно шума
        # Сам участок — без "нормальных" слов скопления (обычно строка
        # текста рядом с подписью): они не должны оказаться внутри
        # изображения и пропасть из текста.
        core = [w for w in cluster if not is_plausible_word(w.text)] or suspect
        regions.append(
            (min(w.x0 for w in core), min(w.y0 for w in core), max(w.x1 for w in core), max(w.y1 for w in core))
        )
    return regions
