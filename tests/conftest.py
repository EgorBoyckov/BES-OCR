"""Генерация тестовых PDF-фикстур (без бинарных файлов в репозитории)."""
from __future__ import annotations

import io
import os

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

import pymupdf as fitz

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(FONT_PATH, size)
    except Exception:
        return ImageFont.load_default()


def _draw_grid(draw: ImageDraw.ImageDraw, x0, y0, n_rows, n_cols, col_w, row_h, skip_v_in_row=None):
    for r in range(n_rows + 1):
        draw.line((x0, y0 + r * row_h, x0 + n_cols * col_w, y0 + r * row_h), fill="black", width=3)
    for c in range(n_cols + 1):
        y_start = y0
        if skip_v_in_row is not None and c not in (0, n_cols):
            y_start = y0 + row_h * (skip_v_in_row + 1)
        draw.line((x0 + c * col_w, y_start, x0 + c * col_w, y0 + n_rows * row_h), fill="black", width=3)


def _image_pdf_from_pil(img: Image.Image, out_path: str) -> None:
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(fitz.Rect(0, 0, 595, 842), stream=buf.getvalue())
    doc.save(out_path)
    doc.close()


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> str:
    return str(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture()
def text_pdf(fixtures_dir) -> str:
    """PDF с обычным текстовым слоем: заголовок, абзац, простая таблица."""
    path = os.path.join(fixtures_dir, "text.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, h - 60, "Test Document Title")
    c.setFont("Helvetica", 11)
    c.drawString(40, h - 100, "This is a simple paragraph of body text used to check")
    c.drawString(40, h - 115, "that text extraction and layout analysis work correctly.")

    x0, y0 = 40, h - 300
    rows, cols, cell_w, cell_h = 3, 3, 100, 25
    for r in range(rows + 1):
        c.line(x0, y0 - r * cell_h, x0 + cols * cell_w, y0 - r * cell_h)
    for cc in range(cols + 1):
        c.line(x0 + cc * cell_w, y0, x0 + cc * cell_w, y0 - rows * cell_h)
    c.setFont("Helvetica", 10)
    data = [["A1", "B1", "C1"], ["A2", "B2", "C2"], ["A3", "B3", "C3"]]
    for r in range(rows):
        for cc in range(cols):
            c.drawString(x0 + cc * cell_w + 10, y0 - r * cell_h - 17, data[r][cc])
    c.showPage()
    c.save()
    return path


@pytest.fixture()
def english_pdf(fixtures_dir) -> str:
    path = os.path.join(fixtures_dir, "english.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h - 60, "English Language Report")
    c.setFont("Helvetica", 11)
    for i, line in enumerate(
        [
            "This document is written entirely in English.",
            "It is used to verify English OCR and text-layer extraction.",
        ]
    ):
        c.drawString(40, h - 100 - i * 15, line)
    c.showPage()
    c.save()
    return path


@pytest.fixture()
def russian_scanned_pdf(fixtures_dir) -> str:
    """Скан (изображение) с русским текстом и таблицей с объединённой шапкой."""
    path = os.path.join(fixtures_dir, "russian_scanned.pdf")
    img = Image.new("RGB", (1654, 2339), "white")
    draw = ImageDraw.Draw(img)
    font = _font(40)
    font_small = _font(28)
    draw.text((80, 80), "Тестовый документ на русском языке", font=font, fill="black")
    draw.text((80, 160), "Это простой абзац текста для проверки OCR распознавания.", font=font_small, fill="black")

    # Ячейки заполнены словами разумной длины (не однословными обрывками):
    # у img2table калибровка типичного размера символа и межстрочного
    # интервала строится по статистике реального текста на странице —
    # таблица из одних коротких 3-4-буквенных слов даёт слишком мало данных
    # для калибровки и распознаётся ненадёжно даже при чёткой сетке линий.
    x0, y0, col_w, row_h, n_cols, n_rows = 80, 400, 320, 110, 3, 3
    _draw_grid(draw, x0, y0, n_rows, n_cols, col_w, row_h, skip_v_in_row=0)
    draw.text((x0 + 10, y0 + 40), "Заголовок таблицы на всю ширину", font=font_small, fill="black")
    data = [
        ["Иванов Иван", "Инженер отдела", "Первое значение"],
        ["Петров Пётр", "Ведущий специалист", "Второе значение"],
    ]
    for r in range(2):
        for c in range(3):
            draw.text((x0 + c * col_w + 10, y0 + (r + 1) * row_h + 15), data[r][c], font=font_small, fill="black")

    _image_pdf_from_pil(img, path)
    return path


@pytest.fixture()
def mixed_language_pdf(fixtures_dir) -> str:
    path = os.path.join(fixtures_dir, "mixed.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, h - 60, "Mixed Language / Смешанный документ")
    c.setFont("Helvetica", 11)
    c.drawString(40, h - 100, "English part: quick brown fox jumps over the lazy dog.")
    c.drawString(40, h - 120, "Russian part is only embedded in the scanned fixture (Helvetica lacks Cyrillic glyphs).")
    c.showPage()
    c.save()
    return path


@pytest.fixture()
def table_merged_cells_pdf(fixtures_dir) -> str:
    """Таблица с объединёнными по горизонтали ячейками в заголовке (текстовый PDF)."""
    path = os.path.join(fixtures_dir, "table_merged.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    x0, y0 = 40, h - 100
    col_w, row_h = 90, 25
    n_cols, n_rows = 4, 4
    # outer border + horizontal lines
    for r in range(n_rows + 1):
        c.line(x0, y0 - r * row_h, x0 + n_cols * col_w, y0 - r * row_h)
    for cc in range(n_cols + 1):
        y_top = y0 - row_h if cc not in (0, n_cols) else y0
        c.line(x0 + cc * col_w, y_top, x0 + cc * col_w, y0 - n_rows * row_h)
    c.setFont("Helvetica-Bold", 10)
    c.drawString(x0 + 10, y0 - 17, "Combined header spanning columns")
    c.setFont("Helvetica", 9)
    data = [
        ["1", "10.5", "2024-01-01", "OK"],
        ["2", "-3.2", "2024-01-02", "FAIL"],
        ["3", "0", "2024-01-03", "OK"],
    ]
    for r in range(3):
        for cc in range(4):
            c.drawString(x0 + cc * col_w + 10, y0 - (r + 2) * row_h + 8, data[r][cc])
    c.showPage()
    c.save()
    return path


@pytest.fixture()
def multipage_table_pdf(fixtures_dir) -> str:
    """Таблица, продолжающаяся на нескольких страницах."""
    path = os.path.join(fixtures_dir, "multipage_table.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    for page in range(2):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, h - 50, f"Table part {page + 1}")
        x0, y0, col_w, row_h, n_cols, n_rows = 40, h - 90, 120, 22, 3, 8
        for r in range(n_rows + 1):
            c.line(x0, y0 - r * row_h, x0 + n_cols * col_w, y0 - r * row_h)
        for cc in range(n_cols + 1):
            c.line(x0 + cc * col_w, y0, x0 + cc * col_w, y0 - n_rows * row_h)
        c.setFont("Helvetica", 9)
        for r in range(n_rows):
            for cc in range(n_cols):
                c.drawString(x0 + cc * col_w + 8, y0 - r * row_h - 15, f"p{page + 1}r{r}c{cc}")
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def images_pdf(fixtures_dir) -> str:
    path = os.path.join(fixtures_dir, "with_images.pdf")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((40, 60), "Document with an embedded image", fontsize=16)
    img = Image.new("RGB", (300, 200), "lightblue")
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 290, 190), outline="black", width=4)
    d.text((100, 90), "LOGO", fill="black", font=_font(30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    page.insert_image(fitz.Rect(100, 150, 400, 350), stream=buf.getvalue())
    doc.save(path)
    doc.close()
    return path


@pytest.fixture()
def complex_mixed_pdf(fixtures_dir) -> str:
    """Заголовок + список + таблица + текст на одной странице."""
    path = os.path.join(fixtures_dir, "complex.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    c.setFont("Helvetica-Bold", 18)
    c.drawString(40, h - 50, "Complex Mixed Page")
    c.setFont("Helvetica", 11)
    c.drawString(40, h - 90, "1. First numbered item")
    c.drawString(40, h - 105, "2. Second numbered item")
    c.drawString(40, h - 120, "- Bullet item one")
    c.drawString(40, h - 135, "- Bullet item two")

    x0, y0, col_w, row_h = 40, h - 250, 90, 22
    for r in range(3):
        c.line(x0, y0 - r * row_h, x0 + 3 * col_w, y0 - r * row_h)
    for cc in range(4):
        c.line(x0 + cc * col_w, y0, x0 + cc * col_w, y0 - 2 * row_h)
    c.setFont("Helvetica", 9)
    for r in range(2):
        for cc in range(3):
            c.drawString(x0 + cc * col_w + 8, y0 - r * row_h - 15, f"r{r}c{cc}")

    c.setFont("Helvetica", 11)
    c.drawString(40, h - 320, "Trailing paragraph after the table to check ordering.")
    c.showPage()
    c.save()
    return path


@pytest.fixture()
def multipage_pdf(fixtures_dir) -> str:
    path = os.path.join(fixtures_dir, "multipage.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    for i in range(5):
        c.setFont("Helvetica-Bold", 16)
        c.drawString(40, h - 60, f"Page {i + 1} heading")
        c.setFont("Helvetica", 11)
        c.drawString(40, h - 100, f"Body text for page {i + 1}.")
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def large_pdf(fixtures_dir) -> str:
    """Документ на 100+ страниц — проверка производительности/потоковой обработки."""
    path = os.path.join(fixtures_dir, "large.pdf")
    c = canvas.Canvas(path, pagesize=A4)
    w, h = A4
    for i in range(105):
        c.setFont("Helvetica", 12)
        c.drawString(40, h - 60, f"Page {i + 1} of a large document")
        c.drawString(40, h - 90, "Lorem ipsum dolor sit amet, consectetur adipiscing elit.")
        c.showPage()
    c.save()
    return path


@pytest.fixture()
def corrupted_pdf(fixtures_dir) -> str:
    path = os.path.join(fixtures_dir, "corrupted.pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4\nthis is not a valid pdf body at all\n%%EOF")
    return path


@pytest.fixture()
def no_text_layer_pdf(russian_scanned_pdf) -> str:
    return russian_scanned_pdf


SERIF_FONT_PATHS = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
)
SERIF_BOLD_FONT_PATHS = (
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
)


def _first_font(paths, size):
    for p in paths:
        if os.path.isfile(p):
            return ImageFont.truetype(p, size)
    return None


@pytest.fixture()
def official_form_scan_pdf(fixtures_dir) -> str:
    """Скан (300 dpi) типового бланка, воспроизводящий сценарии реального
    документа из отчёта пользователя: шапка из двух строк по правому краю,
    полужирный заголовок по центру, нумерованный список, реквизиты сторон
    в две колонки и синяя печать, частично перекрывающая текст. Шрифт —
    Liberation Serif (метрически совместим с Times New Roman) 12pt."""
    regular = _first_font(SERIF_FONT_PATHS, 50)  # 12pt при 300 dpi
    bold = _first_font(SERIF_BOLD_FONT_PATHS, 50)
    if regular is None or bold is None or "Liberation" not in (regular.path or ""):
        pytest.skip("нужен шрифт Liberation Serif (метрически совместимый с Times New Roman)")
    path = os.path.join(fixtures_dir, "official_form_scan.pdf")
    img = Image.new("RGB", (2480, 3508), "white")
    draw = ImageDraw.Draw(img)
    pitch = 58  # ~14pt
    left, right = 250, 2330

    draw.text((right, 180), "Приложение 1", font=regular, fill="black", anchor="rs")
    draw.text((right, 180 + pitch), "к договору № 618д/2025 о практической подготовке обучающихся",
              font=regular, fill="black", anchor="rs")
    center = (left + right) // 2
    draw.text((center, 360), "Образовательная программа и перечень отчетных документов",
              font=bold, fill="black", anchor="ms")
    draw.text((center, 360 + pitch), "по практической подготовке обучающихся", font=bold, fill="black", anchor="ms")

    y = 560
    for text in ("Перечень отчетных документов",):
        draw.text((left, y), text, font=bold, fill="black", anchor="ls")
    for i, text in enumerate((
        "1. Совместный рабочий график проведения практики",
        "2. Индивидуальное задание обучающегося",
        "3. Отчет по практике с приложениями",
    )):
        draw.text((left, y + (i + 1) * pitch), text, font=regular, fill="black", anchor="ls")

    y = 900
    gutter = 1350
    left_col = (
        "Университет:",
        "Уральский государственный",
        "экономический университет",
        "Адрес: 620144, г. Екатеринбург",
        "ул. 8 Марта, 62/45",
    )
    right_col = (
        "Профильная организация:",
        "Общество с ограниченной ответственностью",
        "ИНН: 7203402981",
        "Адрес: 625000, г. Тюмень",
        "ул. Республики, д. 61",
    )
    for i, (lt, rt) in enumerate(zip(left_col, right_col)):
        draw.text((left, y + i * pitch), lt, font=regular, fill="black", anchor="ls")
        draw.text((gutter, y + i * pitch), rt, font=regular, fill="black", anchor="ls")

    # Синяя печать: кольцо с текстом по кругу, частично поверх нижней строки
    # правой колонки.
    cx, cy, r = 1900, 1330, 190
    stamp_blue = (40, 70, 200)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=stamp_blue, width=10)
    draw.ellipse((cx - r + 40, cy - r + 40, cx + r - 40, cy + r - 40), outline=stamp_blue, width=6)
    small = _first_font(SERIF_BOLD_FONT_PATHS, 34)
    draw.text((cx, cy), "ПЕЧАТЬ", font=small, fill=stamp_blue, anchor="mm")
    draw.line((1450, 1250, 1600, 1150, 1700, 1260), fill=stamp_blue, width=5)  # "подпись"

    _image_pdf_from_pil(img, path)
    return path
