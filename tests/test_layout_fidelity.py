"""Воспроизведение вёрстки оригинала "один в один": кегль, выравнивание,
принудительные переносы, двухколоночные блоки (core/layout_analysis.py)."""
from dataclasses import replace

from bes_ocr.core.layout_analysis import build_blocks, build_cell_blocks, normalize_ocr_words
from bes_ocr.core.models import Alignment, BBox, ListItem, Paragraph, Table

from .layout_helpers import justified_line, line_at, line_center, line_right

LEFT, RIGHT = 60.0, 560.0


def _as_ocr(words, jitter=0.0):
    """Слова "как из OCR": без кегля/базовой линии, только рамки."""
    out = []
    for i, w in enumerate(words):
        out.append(replace(w, size_pt=11.0, bold=False, baseline=None, y1=w.y1 + (jitter if i % 2 else 0.0)))
    return out


def test_ocr_words_get_one_size_per_line_regardless_of_descenders():
    words = line_at("Образовательная программа компоненты образовательной программы при реализации", LEFT, 100, 12)
    words += line_at("которых организуется практическая подготовка перечень отчетных документов", LEFT, 114, 12)
    normalized = normalize_ocr_words(_as_ocr(words))
    assert {w.size_pt for w in normalized} == {12.0}
    # Базовая линия восстановлена по глифам, а не по низу рамки слова.
    first_line = [w for w in normalized if abs(w.baseline - 100) < 3]
    assert all(abs(w.baseline - 100) < 0.6 for w in first_line)


def test_short_word_line_takes_size_of_neighbouring_lines():
    """Строка из коротких слов ("с", "по") оценивается по высоте грубо —
    она должна получить кегль соседних строк той же ячейки/абзаца."""
    words = line_at("Кольева", LEFT, 100, 9) + line_at("Станиславовна", LEFT, 110, 9) + line_at("по", LEFT, 120, 9)
    normalized = normalize_ocr_words(_as_ocr(words, jitter=1.5))
    assert {w.size_pt for w in normalized} == {9.0}


def test_bold_detected_from_relative_stroke_width():
    body = []
    for i in range(6):
        body += line_at("обычный текст документа достаточно длинной строки", LEFT, 200 + i * 14, 12)
    title = line_center("Полужирный заголовок документа", 310, 100, 12)
    for w in body:
        w.stroke_pt = 0.11 * 12
    for w in title:
        w.stroke_pt = 0.15 * 12
    normalized = normalize_ocr_words(_as_ocr(body) + [replace(w, size_pt=11.0, baseline=None) for w in title])
    assert all(w.bold for w in normalized if w.y0 < 150)
    assert not any(w.bold for w in normalized if w.y0 > 150)


def test_right_aligned_header_keeps_forced_line_break():
    """Шапка "Приложение 1 / к договору..." — две строки по правому краю;
    первая заканчивается принудительно (следующее слово поместилось бы).
    Раньше строки склеивались в одну строку с отступом слева."""
    words = line_right("Приложение 1", RIGHT, 40)
    words += line_right("к договору № 618д/2025 о практической подготовке обучающихся", RIGHT, 54)
    words += justified_line("Текст документа во всю ширину страницы для определения границ", LEFT, RIGHT, 100)
    blocks = build_blocks(words, 595, 842)
    header = blocks[0]
    assert header.alignment == Alignment.RIGHT
    assert header.text == "Приложение 1\nк договору № 618д/2025 о практической подготовке обучающихся"
    assert header.line_pitch_pt == 14


def test_justified_paragraph_flows_without_forced_breaks():
    words = []
    lines = [
        "Федеральное государственное бюджетное образовательное учреждение высшего образования",
        "Уральский государственный экономический университет и Общество с ограниченной",
        "ответственностью заключили настоящий договор о нижеследующем",
    ]
    for i, text in enumerate(lines[:-1]):
        words += justified_line(text, LEFT, RIGHT, 100 + i * 14)
    words += line_at(lines[-1], LEFT, 100 + 2 * 14)
    blocks = build_blocks(words, 595, 842)
    assert len(blocks) == 1
    para = blocks[0]
    assert para.alignment == Alignment.JUSTIFY
    assert "\n" not in para.text
    assert para.n_lines == 3 and para.line_pitch_pt == 14


def test_short_left_line_ends_paragraph():
    words = justified_line("Первый абзац текста занимает всю строку от края до края", LEFT, RIGHT, 100)
    words += line_at("и заканчивается здесь.", LEFT, 114)
    words += justified_line("Второй абзац начинается с новой строки и тоже широкий", LEFT, RIGHT, 128)
    words += line_at("конец.", LEFT, 142)
    paragraphs = [b for b in build_blocks(words, 595, 842) if isinstance(b, Paragraph)]
    assert [p.text.split()[0] for p in paragraphs] == ["Первый", "Второй"]


def test_centered_title_detected_relative_to_content_bounds():
    words = justified_line("Строка основного текста для определения полей страницы документа", LEFT, RIGHT, 200)
    words += justified_line("Ещё одна строка основного текста для определения полей страницы", LEFT, RIGHT, 214)
    words += line_center("Заголовок по центру", (LEFT + RIGHT) / 2, 100, bold=True)
    title = build_blocks(words, 595, 842)[0]
    assert title.alignment == Alignment.CENTER
    assert title.runs[0].bold


def test_list_item_keeps_hanging_indent_geometry():
    words = justified_line("Абзац текста перед списком шириной во всю строку страницы", LEFT, RIGHT, 80)
    words += line_at("1. Совместный рабочий график проведения практики", LEFT, 100)
    words += line_at("2. Индивидуальное задание обучающегося", LEFT, 114)
    items = [b for b in build_blocks(words, 595, 842) if isinstance(b, ListItem)]
    assert [i.text for i in items] == ["Совместный рабочий график проведения практики", "Индивидуальное задание обучающегося"]
    assert items[0].first_line_indent_pt < 0  # маркер левее текста пункта
    assert items[0].indent_pt + items[0].first_line_indent_pt == LEFT


def _two_column_block(y0: float) -> list:
    gutter_l, gutter_r = 315.0, 325.0
    left = [
        "Университет:",
        "Федеральное государственное бюджетное",
        "образовательное учреждение высшего",
        "образования «Уральский государственный",
        "экономический университет»",
    ]
    right = [
        "Профильная организация:",
        "Общество с ограниченной ответственностью",
        '"ИТ-Сервис"',
        "ИНН: 7203402981",
        "Адрес: 625000, г. Тюмень, ул. Республики",
    ]
    words = []
    for i, (lt, rt) in enumerate(zip(left, right)):
        base = y0 + i * 14
        lw = line_at(lt, LEFT, base)
        if 0 < i < len(left) - 1 and len(lw) > 1:
            lw = justified_line(lt, LEFT, gutter_l, base)
        words += lw + line_at(rt, gutter_r, base)
    return words


def test_two_column_block_becomes_borderless_layout_table():
    """Реквизиты сторон в две колонки: раньше строки левой и правой колонки
    сливались в одну строку ("Университет: Профильная организация: ...")."""
    words = justified_line("Абзац основного текста над блоком реквизитов во всю ширину страницы", LEFT, RIGHT, 80)
    words += _two_column_block(120)
    words += justified_line("Абзац основного текста под блоком реквизитов во всю ширину страницы", LEFT, RIGHT, 220)
    blocks = build_blocks(words, 595, 842)
    tables = [b for b in blocks if isinstance(b, Table)]
    assert len(tables) == 1 and tables[0].borderless
    left_cell, right_cell = tables[0].rows[0].cells
    assert left_cell.text.startswith("Университет:")
    assert "экономический университет»" in left_cell.text
    assert right_cell.text.startswith("Профильная организация:")
    assert "Профильная" not in left_cell.text
    # Текст до и после блока остаётся обычными абзацами — область колонок
    # не растягивается "до конца страницы".
    assert isinstance(blocks[0], Paragraph) and isinstance(blocks[-1], Paragraph)
    assert blocks[-1].text.startswith("Абзац основного текста под")


def test_ragged_list_does_not_become_columns():
    """Короткие строки списка с совпадающими промежутками между словами — не
    колонки: вторая "колонка" была бы слишком узкой."""
    words = justified_line("Абзац основного текста во всю ширину страницы документа", LEFT, RIGHT, 60)
    for i, text in enumerate(["1. Первый пункт списка", "2. Второй пункт списка", "3. Третий пункт списка", "4. Четвёртый пункт"]):
        words += line_at(text, LEFT, 80 + i * 14)
    assert not any(isinstance(b, Table) for b in build_blocks(words, 595, 842))


def test_cell_blocks_keep_original_line_breaks_and_centering():
    cell = BBox(449, 200, 500, 250)
    words = []
    for i, text in enumerate(["с", "12.01.2026", "по", "07.02.2026"]):
        words += line_center(text, (cell.x0 + cell.x1) / 2, 210 + i * 10, 9)
    blocks = build_cell_blocks(words, cell, padding=3.0)
    assert len(blocks) == 1
    assert blocks[0].text == "с\n12.01.2026\nпо\n07.02.2026"
    assert blocks[0].alignment == Alignment.CENTER
