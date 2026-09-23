"""Отделение цветных печатей/подписей от напечатанного текста (core/color_marks.py)."""
import io

import numpy as np
import pytest
from PIL import Image, ImageDraw

from bes_ocr.core.color_marks import extract_color_marks

from .conftest import SERIF_FONT_PATHS, _first_font

BLUE = (40, 70, 200)


def _bgr(img: Image.Image) -> np.ndarray:
    return np.asarray(img.convert("RGB"))[:, :, ::-1].copy()


def test_stamp_is_cut_out_with_transparency_and_erased_but_black_text_kept():
    font = _first_font(SERIF_FONT_PATHS, 50)
    if font is None:
        pytest.skip("нет шрифта")
    img = Image.new("RGB", (1200, 800), "white")
    draw = ImageDraw.Draw(img)
    draw.text((100, 380), "Руководитель организации", font=font, fill="black", anchor="ls")
    draw.ellipse((350, 200, 750, 600), outline=BLUE, width=12)
    cleaned, marks = extract_color_marks(_bgr(img), dpi=300)

    assert len(marks) == 1
    m = marks[0]
    assert m.x0 <= 350 and m.y0 <= 200 and m.x1 >= 750 and m.y1 >= 600
    stamp = Image.open(io.BytesIO(m.png_rgba))
    assert stamp.mode == "RGBA"
    alpha = np.asarray(stamp)[:, :, 3]
    assert alpha.max() == 255 and (alpha == 0).mean() > 0.5  # фон прозрачный

    # Синего в растре для OCR не осталось, а чёрный текст под печатью цел.
    b, r = cleaned[:, :, 0].astype(int), cleaned[:, :, 2].astype(int)
    assert int(((b - r) > 60).sum()) < 50
    text_region = cleaned[330:390, 100:1000]
    assert int((text_region.max(axis=2) < 80).sum()) > 2000


def test_coloured_printed_text_is_not_taken_for_a_stamp():
    font = _first_font(SERIF_FONT_PATHS, 60)
    if font is None:
        pytest.skip("нет шрифта")
    img = Image.new("RGB", (1600, 600), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(["Официальный сайт организации", "Электронная почта для связи", "Телефон приёмной комиссии"]):
        draw.text((100, 150 + i * 90), line, font=font, fill=BLUE, anchor="ls")
    cleaned, marks = extract_color_marks(_bgr(img), dpi=300)
    assert marks == []
    assert np.array_equal(cleaned, _bgr(img))


def test_grayscale_page_is_untouched():
    img = Image.new("RGB", (800, 600), "white")
    ImageDraw.Draw(img).ellipse((100, 100, 500, 500), outline="black", width=10)
    src = _bgr(img)
    cleaned, marks = extract_color_marks(src, dpi=300)
    assert marks == [] and cleaned is src
