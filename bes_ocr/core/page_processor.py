"""Обработка одной страницы PDF: выбор источника текста, layout, таблицы, изображения.

Инкапсулирует решение "текстовый слой vs OCR" на уровне страницы (документ
может содержать смешанные страницы — п.2 ТЗ) и оборачивает всю работу в
PageProcessingError, чтобы ошибка одной страницы не рушила весь документ.
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import replace

from ..config.settings import Settings
from .errors import PageProcessingError
from .layout_analysis import LayoutWord, build_blocks, content_bounds, normalize_ocr_words, words_to_lines
from .models import BBox, ImageBlock, Page, Paragraph, Table
from .ocr_engine import OcrEngine, binarize, deskew, find_noise_regions, is_plausible_word, stroke_width_px
from .pdf_source import PdfSource
from .table_detection import (
    add_missed_tables,
    detect_tables_img2table,
    detect_tables_ruling_lines,
    detect_tables_pdfplumber,
    fill_table_from_words,
    measure_cell_padding,
)
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


def _words_inside(words: list[LayoutWord], bbox: BBox) -> list[LayoutWord]:
    return [w for w in words if _bbox_contains_point(bbox, (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2)]


def _normalize_text(text: str) -> str:
    return "".join(text.split())


def _apply_text_layer_words_to_table(table: Table, words: list[LayoutWord]) -> None:
    """Для таблиц текстового слоя текст ячеек берётся из pdfplumber (он
    точно знает границы ячеек), а кегль/начертание/выравнивание — из слов
    PyMuPDF. Если слова ячейки дают тот же текст, ячейка строится из слов
    целиком (с выравниванием и переносами оригинала); иначе к тексту
    pdfplumber применяются только кегль и начертание."""
    cells = [c for row in table.rows for c in row.cells if not c.is_merge_continuation and c.bbox is not None]
    table.cell_padding_pt = measure_cell_padding([(c.bbox, _words_inside(words, c.bbox)) for c in cells])
    for row in table.rows:
        for cell in row.cells:
            if cell.is_merge_continuation or cell.bbox is None:
                continue
            inside = _words_inside(words, cell.bbox)
            if not inside:
                continue
            original = cell.text
            probe = Table(
                rows=[type(row)(cells=[type(cell)(bbox=cell.bbox)])], source="text", cell_padding_pt=table.cell_padding_pt
            )
            fill_table_from_words(probe, inside)
            rebuilt = probe.rows[0].cells[0]
            if _normalize_text(rebuilt.text) == _normalize_text(original):
                cell.blocks = rebuilt.blocks
                cell.v_align = rebuilt.v_align
                continue
            size = statistics.median(w.size_pt for w in inside)
            all_bold = all(w.bold for w in inside)
            for block in cell.blocks:
                if isinstance(block, Paragraph):
                    for run in block.runs:
                        run.size_pt = size
                        run.bold = all_bold


def _fix_vertical_bars(words: list[LayoutWord]) -> list[LayoutWord]:
    """Одиночная "|" в тексте вне таблиц: высотой с цифру — это "1" (в
    Times New Roman единица на скане похожа на черту: "Приложение |"),
    выше строки — обрывок линии, не текст."""
    result = []
    for w in words:
        if w.text in ("|", "||", "l|", "|l") or (w.text == "l" and w.x1 - w.x0 < 0.3 * w.size_pt):
            height = w.y1 - w.y0
            if w.text in ("|", "l") and 0.55 * w.size_pt <= height <= 0.85 * w.size_pt:
                result.append(replace(w, text="1"))
            continue
        result.append(w)
    return result


def _plausible_count(words) -> int:
    return sum(1 for w in words if is_plausible_word(w.text.strip("|_[]")))


def _merge_cell_ocr(ocr_words, tables: list[Table], binary, ocr_engine: OcrEngine, px_to_pt: float):
    """Слова страницы, в которых для ячеек без распознанного текста (или с
    явно худшим результатом) подставлено распознавание самой ячейки."""
    result = list(ocr_words)
    for t in tables:
        for row in t.rows:
            for cell in row.cells:
                if cell.is_merge_continuation or cell.bbox is None:
                    continue
                b = cell.bbox
                x0, y0, x1, y1 = (int(round(v / px_to_pt)) for v in (b.x0, b.y0, b.x1, b.y1))
                inside = [w for w in result if x0 <= (w.x0 + w.x1) / 2 <= x1 and y0 <= (w.y0 + w.y1) / 2 <= y1]
                page_text = [w for w in inside if w.text.strip("|_[]—-")]
                if page_text and len(page_text) >= 3:
                    continue
                cell_words = ocr_engine.recognize_region(binary, x0, y0, x1, y1)
                if not cell_words:
                    continue
                if not page_text or _plausible_count(cell_words) > _plausible_count(page_text):
                    ids = {id(w) for w in inside}
                    result = [w for w in result if id(w) not in ids] + cell_words
    return result


def _encode_png(image) -> bytes | None:
    import cv2

    ok, buf = cv2.imencode(".png", image)
    return buf.tobytes() if ok else None


def process_page(
    pdf_path: str,
    pdf: PdfSource,
    page_number: int,
    settings: Settings,
    ocr_engine: OcrEngine,
) -> Page:
    """page_number — 0-based индекс."""
    start_time = time.monotonic()
    try:
        width_pt, height_pt = pdf.page_size_pt(page_number)
        page = Page(number=page_number + 1, width_pt=width_pt, height_pt=height_pt)

        assessment = assess_text_layer(pdf, page_number, settings)
        extra_blocks: list[object] = []  # изображения, не участвующие в разборе текста

        if assessment.usable:
            raw_words = pdf.extract_text_words(page_number)
            # Медианный размер шрифта тела документа передаётся в детектор
            # таблиц: текст ячеек по умолчанию не несёт информации о размере
            # шрифта (см. table_detection.py), а фиксированный запасной
            # размер модели (11pt) почти всегда крупнее реального шрифта
            # плотных таблиц бланков, из-за чего таблица переносит больше
            # строк, чем в оригинале, и документ раздувается лишними
            # страницами.
            body_font_size = statistics.median([w.font_size for w in raw_words]) if raw_words else 9.0
            tables = detect_tables_pdfplumber(pdf_path, page_number, font_size_pt=body_font_size)
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
                    baseline=w.baseline,
                )
                for w in raw_words
            ]
            for t in tables:
                _apply_text_layer_words_to_table(t, words)
            page.used_ocr = False

            for img in pdf.extract_images(page_number):
                bbox = img.get("bbox")
                extra_blocks.append(
                    ImageBlock(
                        data=img["data"],
                        width_px=img["width"],
                        height_px=img["height"],
                        fmt=img.get("ext", "png"),
                        bbox=BBox(bbox.x0, bbox.y0, bbox.x1, bbox.y1) if bbox else None,
                    )
                )
        else:
            import cv2

            image_bgr, zoom = pdf.render_page_image(page_number, dpi=settings.render_dpi)
            px_to_pt = 1.0 / zoom
            # Выравниваем перекос ОДИН раз: и OCR, и детектор таблиц, и
            # вырезание печатей работают в одной и той же системе координат.
            image_bgr = deskew(image_bgr)

            # Цветные печати/подписи — отдельными изображениями поверх
            # страницы; OCR получает растр уже без них.
            marks = []
            ocr_image = image_bgr
            if settings.extract_color_marks:
                from .color_marks import extract_color_marks

                ocr_image, marks = extract_color_marks(image_bgr, settings.render_dpi, settings.ocr_languages)
            for m in marks:
                extra_blocks.append(
                    ImageBlock(
                        data=m.png_rgba,
                        width_px=m.x1 - m.x0,
                        height_px=m.y1 - m.y0,
                        bbox=BBox(m.x0 * px_to_pt, m.y0 * px_to_pt, m.x1 * px_to_pt, m.y1 * px_to_pt),
                        floating=True,
                    )
                )

            ocr_words = ocr_engine.recognize_words(ocr_image, do_deskew=False)

            if ocr_engine.is_likely_handwriting_or_noise(ocr_words):
                page.had_handwriting_fallback = True
                data = _encode_png(image_bgr)
                if data:
                    page.blocks.append(
                        ImageBlock(
                            data=data,
                            width_px=image_bgr.shape[1],
                            height_px=image_bgr.shape[0],
                            caption="Страница сохранена как изображение (низкая уверенность распознавания)",
                        )
                    )
                logger.info("Страница %d: сохранена как изображение (вероятен рукописный/нераспознаваемый текст)", page_number + 1)
                return page

            binary = binarize(ocr_image)
            tables = detect_tables_img2table(ocr_image, settings, px_to_pt, with_ocr=False)
            # Запасной детектор по линиям разметки: таблица, которую img2table
            # пропустила (так бывает на части окружений/версий библиотек),
            # иначе превратилась бы в поток абзацев с "|" на месте линий.
            try:
                ruled = detect_tables_ruling_lines(ocr_image, px_to_pt, dpi=settings.render_dpi)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Страница %d: детектор таблиц по линиям не сработал (%s)", page_number + 1, exc)
                ruled = []
            found = len(tables)
            tables = add_missed_tables(tables, ruled)
            if len(tables) > found:
                logger.info(
                    "Страница %d: %d таблиц(ы) найдено по линиям разметки в дополнение к img2table",
                    page_number + 1,
                    len(tables) - found,
                )

            # Текст ячеек таблиц — по умолчанию из распознавания всей
            # страницы (на реальном скане оно точнее: 97% слов шапки и
            # строк таблицы против 91% при распознавании каждой ячейки
            # отдельно). Отдельное распознавание ячейки (psm 6) — запасной
            # вариант для ячеек, где распознавание страницы ничего не нашло
            # или нашло заметно меньше осмысленных слов: в плотных таблицах
            # сегментация всей страницы теряет содержимое узких граф ("№",
            # номера строк).
            if tables:
                ocr_words = _merge_cell_ocr(ocr_words, tables, binary, ocr_engine, px_to_pt)

            # Порог уверенности отсекает мусор, но правильно прочитанные
            # "нормальные" слова оставляем даже при низкой уверенности —
            # уверенность Tesseract для целого слова калибрована плохо.
            mark_boxes = [(m.x0, m.y0, m.x1, m.y1) for m in marks]

            def _keep(w) -> bool:
                plausible = is_plausible_word(w.text)
                cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
                if any(x0 <= cx <= x1 and y0 <= cy <= y1 for x0, y0, x1, y1 in mark_boxes):
                    # Под стёртой печатью остаются обрывки штрихов — там
                    # доверяем только "нормальным" словам или очень
                    # уверенному распознаванию.
                    return plausible or w.confidence >= 85
                return w.confidence >= settings.ocr_word_confidence_threshold or plausible

            confident = [w for w in ocr_words if _keep(w)]
            raw_layout_words = []
            for w in confident:
                stroke = stroke_width_px(binary, w.x0, w.y0, w.x1, w.y1)
                raw_layout_words.append(
                    LayoutWord(
                        text=w.text,
                        x0=w.x0 * px_to_pt,
                        y0=w.y0 * px_to_pt,
                        x1=w.x1 * px_to_pt,
                        y1=w.y1 * px_to_pt,
                        size_pt=max(6.0, (w.y1 - w.y0) * px_to_pt * 0.8),
                        stroke_pt=stroke * px_to_pt if stroke else None,
                    )
                )
            # Кегль и начертание оцениваются по всей странице сразу (включая
            # слова внутри таблиц) — так у текста одного уровня везде один
            # и тот же кегль.
            all_words = normalize_ocr_words(raw_layout_words)
            body_font_size = statistics.median(w.size_pt for w in all_words) if all_words else 11.0
            for t in tables:
                fill_table_from_words(
                    t, _words_inside(all_words, t.bbox) if t.bbox else all_words, fit_spacing=True
                )

            # Локальные пятна визуального шума (чёрные печати, подписи,
            # помарки, наложенные на печатный текст) — п.3 ТЗ: сохраняем такой
            # участок как изображение вместо потока ошибочных символов.
            # Ищем только вне уже найденных таблиц, иначе короткие
            # обёрнутые значения в узких столбцах (даты, номера) ложно
            # похожи на "пятно шума".
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
            noise_regions_px = find_noise_regions(words_outside_tables_px, settings, img_w, img_h)
            noise_regions_pt = []
            pad = 15
            for nx0, ny0, nx1, ny1 in noise_regions_px:
                cx0, cy0 = max(0, int(nx0) - pad), max(0, int(ny0) - pad)
                cx1, cy1 = min(img_w, int(nx1) + pad), min(img_h, int(ny1) + pad)
                crop = image_bgr[cy0:cy1, cx0:cx1]
                if crop.size == 0:
                    continue
                # Фон участка делаем прозрачным: изображение стоит поверх
                # страницы и не должно закрывать соседний текст.
                gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
                rgba = cv2.cvtColor(crop, cv2.COLOR_BGR2BGRA)
                rgba[:, :, 3] = cv2.threshold(255 - gray, 40, 255, cv2.THRESH_TOZERO)[1]
                data = _encode_png(rgba)
                if not data:
                    continue
                noise_regions_pt.append((nx0 * px_to_pt, ny0 * px_to_pt, nx1 * px_to_pt, ny1 * px_to_pt))
                extra_blocks.append(
                    ImageBlock(
                        data=data,
                        width_px=crop.shape[1],
                        height_px=crop.shape[0],
                        bbox=BBox(cx0 * px_to_pt, cy0 * px_to_pt, cx1 * px_to_pt, cy1 * px_to_pt),
                        floating=True,
                    )
                )
            if noise_regions_pt:
                logger.info(
                    "Страница %d: %d участок(ов) с визуальным шумом (печать/подпись) сохранены как изображение",
                    page_number + 1,
                    len(noise_regions_pt),
                )

            def _is_noise(w: LayoutWord) -> bool:
                cx, cy = (w.x0 + w.x1) / 2, (w.y0 + w.y1) / 2
                if any(nx0 <= cx <= nx1 and ny0 <= cy <= ny1 for nx0, ny0, nx1, ny1 in noise_regions_pt):
                    return True
                # Одиночные "слова" гигантского кегля из обрывков штрихов.
                return w.size_pt > 2.0 * body_font_size and not is_plausible_word(w.text)

            words = [w for w in _fix_vertical_bars(all_words) if not _is_noise(w)]
            page.used_ocr = True

        table_bboxes = [t.bbox for t in tables if t.bbox]
        text_words = _filter_words_outside_tables(words, table_bboxes)
        bounds = None
        if text_words:
            bounds = content_bounds(words_to_lines(text_words), width_pt)
        text_blocks = build_blocks(
            text_words, width_pt, height_pt, bounds=bounds, fit_spacing=page.used_ocr, drop_symbol_lines=page.used_ocr
        )

        ordered: list[tuple[float, object]] = []
        for b in text_blocks:
            y = b.bbox.y0 if getattr(b, "bbox", None) else 0.0
            ordered.append((y, b))
        for t in tables:
            y = t.bbox.y0 if t.bbox else 0.0
            ordered.append((y, t))
        for img in extra_blocks:
            ordered.append((img.bbox.y0 if img.bbox else 0.0, img))

        ordered.sort(key=lambda item: item[0])
        page.blocks = [b for _, b in ordered]

        n_tables = sum(1 for b in page.blocks if isinstance(b, Table) and not b.borderless)
        n_images = sum(1 for b in page.blocks if isinstance(b, ImageBlock))
        source = "OCR (скан)" if page.used_ocr else "текстовый слой"
        logger.info(
            "Страница %d: источник — %s, блоков: %d (таблиц: %d, изображений: %d), %.1f с",
            page_number + 1,
            source,
            len(page.blocks),
            n_tables,
            n_images,
            time.monotonic() - start_time,
        )
        return page
    except Exception as exc:  # noqa: BLE001
        raise PageProcessingError(page_number + 1, str(exc)) from exc
