"""Обработка одной страницы PDF: выбор источника текста, layout, таблицы, изображения.

Инкапсулирует решение "текстовый слой vs OCR" на уровне страницы (документ
может содержать смешанные страницы — п.2 ТЗ) и оборачивает всю работу в
PageProcessingError, чтобы ошибка одной страницы не рушила весь документ.
"""
from __future__ import annotations

import logging

from ..config.settings import Settings
from .errors import PageProcessingError
from .layout_analysis import LayoutWord, build_blocks
from .models import BBox, ImageBlock, Page
from .ocr_engine import OcrEngine, find_noise_regions
from .pdf_source import PdfSource
from .table_detection import detect_tables_img2table, detect_tables_pdfplumber
from .text_layer import assess_text_layer

logger = logging.getLogger("bes_ocr")

POINTS_PER_PIXEL_DEFAULT = 72.0 / 300.0


def _bbox_contains_point(bbox: BBox, x: float, y: float) -> bool:
    return bbox.x0 - 1 <= x <= bbox.x1 + 1 and bbox.y0 - 1 <= y <= bbox.y1 + 1


def _filter_words_outside_tables(words: list[LayoutWord], table_bboxes: list[BBox]) -> list[LayoutWord]:
    if not table_bboxes:
        return words
    kept = []
    for w in words:
        cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
        if any(_bbox_contains_point(bb, cx, cy) for bb in table_bboxes):
            continue
        kept.append(w)
    return kept


def process_page(
    pdf_path: str,
    pdf: PdfSource,
    page_number: int,
    settings: Settings,
    ocr_engine: OcrEngine,
) -> Page:
    """page_number — 0-based индекс."""
    try:
        width_pt, height_pt = pdf.page_size_pt(page_number)
        page = Page(number=page_number + 1, width_pt=width_pt, height_pt=height_pt)

        assessment = assess_text_layer(pdf, page_number, settings)
        noise_blocks: list[tuple[float, ImageBlock]] = []

        if assessment.usable:
            tables = detect_tables_pdfplumber(pdf_path, page_number)
            raw_words = pdf.extract_text_words(page_number)
            words = [
                LayoutWord(
                    text=w.text,
                    x0=w.x0,
                    y0=w.y0,
                    x1=w.x1,
                    y1=w.y1,
                    size_pt=w.font_size,
                    bold=w.bold,
                    italic=w.italic,
                    font_name=w.font_name,
                )
                for w in raw_words
            ]
            page.used_ocr = False
        else:
            image_bgr, zoom = pdf.render_page_image(page_number, dpi=settings.render_dpi)
            px_to_pt = 1.0 / zoom
            ocr_words = ocr_engine.recognize_words(image_bgr)

            if ocr_engine.is_likely_handwriting_or_noise(ocr_words):
                page.had_handwriting_fallback = True
                import cv2

                ok, buf = cv2.imencode(".png", image_bgr)
                if ok:
                    page.blocks.append(
                        ImageBlock(
                            data=buf.tobytes(),
                            width_px=image_bgr.shape[1],
                            height_px=image_bgr.shape[0],
                            caption="Страница сохранена как изображение (низкая уверенность распознавания)",
                        )
                    )
                logger.info("Страница %d: сохранена как изображение (вероятен рукописный/нераспознаваемый текст)", page_number + 1)
                return page

            tables = detect_tables_img2table(image_bgr, settings, px_to_pt)

            img_h, img_w = image_bgr.shape[:2]
            table_bboxes_px = [
                (t.bbox.x0 / px_to_pt, t.bbox.y0 / px_to_pt, t.bbox.x1 / px_to_pt, t.bbox.y1 / px_to_pt)
                for t in tables
                if t.bbox
            ]
            words_outside_tables_px = [
                w
                for w in ocr_words
                if not any(
                    bx0 - 1 <= (w.x0 + w.x1) / 2 <= bx1 + 1 and by0 - 1 <= (w.y0 + w.y1) / 2 <= by1 + 1
                    for bx0, by0, bx1, by1 in table_bboxes_px
                )
            ]
            # Локальные пятна визуального шума (печати, подписи, помарки,
            # наложенные на печатный текст) — п.3 ТЗ: сохраняем такой
            # участок как изображение вместо потока ошибочных символов.
            # Ищем только вне уже найденных таблиц, иначе короткие
            # обёрнутые значения в узких столбцах (даты, номера) ложно
            # похожи на "пятно шума".
            noise_regions_px = find_noise_regions(words_outside_tables_px, settings, img_w, img_h)
            if noise_regions_px:
                import cv2

                pad = 15
                for nx0, ny0, nx1, ny1 in noise_regions_px:
                    cx0, cy0 = max(0, int(nx0) - pad), max(0, int(ny0) - pad)
                    cx1, cy1 = min(img_w, int(nx1) + pad), min(img_h, int(ny1) + pad)
                    crop = image_bgr[cy0:cy1, cx0:cx1]
                    if crop.size == 0:
                        continue
                    ok, buf = cv2.imencode(".png", crop)
                    if not ok:
                        continue
                    noise_blocks.append(
                        (
                            cy0 * px_to_pt,
                            ImageBlock(
                                data=buf.tobytes(),
                                width_px=crop.shape[1],
                                height_px=crop.shape[0],
                                bbox=BBox(cx0 * px_to_pt, cy0 * px_to_pt, cx1 * px_to_pt, cy1 * px_to_pt),
                            ),
                        )
                    )
                logger.info(
                    "Страница %d: %d участок(ов) с визуальным шумом (печать/подпись) сохранены как изображение",
                    page_number + 1,
                    len(noise_blocks),
                )

            def _in_noise_region(w) -> bool:
                cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
                return any(nx0 <= cx <= nx1 and ny0 <= cy <= ny1 for nx0, ny0, nx1, ny1 in noise_regions_px)

            words = [
                LayoutWord(
                    text=w.text,
                    x0=w.x0 * px_to_pt,
                    y0=w.y0 * px_to_pt,
                    x1=w.x1 * px_to_pt,
                    y1=w.y1 * px_to_pt,
                    size_pt=max(6.0, (w.y1 - w.y0) * px_to_pt * 0.8),
                )
                for w in ocr_words
                if w.confidence >= settings.ocr_word_confidence_threshold and not _in_noise_region(w)
            ]
            page.used_ocr = True

        table_bboxes = [t.bbox for t in tables if t.bbox]
        text_words = _filter_words_outside_tables(words, table_bboxes)
        text_blocks = build_blocks(text_words, width_pt, height_pt)

        ordered: list[tuple[float, object]] = []
        for b in text_blocks:
            y = b.bbox.y0 if getattr(b, "bbox", None) else 0.0
            ordered.append((y, b))
        for t in tables:
            y = t.bbox.y0 if t.bbox else 0.0
            ordered.append((y, t))
        ordered.extend(noise_blocks)

        if assessment.usable:
            for img in pdf.extract_images(page_number):
                bbox = img.get("bbox")
                y = bbox.y0 if bbox else 0.0
                ordered.append(
                    (
                        y,
                        ImageBlock(
                            data=img["data"],
                            width_px=img["width"],
                            height_px=img["height"],
                            fmt=img.get("ext", "png"),
                            bbox=BBox(bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else None,
                        ),
                    )
                )

        ordered.sort(key=lambda item: item[0])
        page.blocks = [b for _, b in ordered]
        return page
    except Exception as exc:  # noqa: BLE001
        raise PageProcessingError(page_number + 1, str(exc)) from exc
