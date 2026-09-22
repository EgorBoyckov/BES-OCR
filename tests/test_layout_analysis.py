from bes_ocr.core.layout_analysis import LayoutWord, build_blocks
from bes_ocr.core.models import Heading, ListItem, Paragraph


def test_heading_detected_by_font_size():
    words = [
        LayoutWord(text="Title", x0=40, y0=40, x1=120, y1=60, size_pt=18),
        LayoutWord(text="Body", x0=40, y0=100, x1=90, y1=112, size_pt=11),
        LayoutWord(text="text", x0=95, y0=100, x1=130, y1=112, size_pt=11),
    ]
    blocks = build_blocks(words, page_width=595, page_height=842)
    assert isinstance(blocks[0], Heading)
    assert isinstance(blocks[1], Paragraph)
    assert blocks[1].text == "Body text"


def test_ordered_list_detected():
    words = [LayoutWord(text=w, x0=40 + i * 30, y0=40, x1=60 + i * 30, y1=52, size_pt=11) for i, w in enumerate(["1.", "First", "item"])]
    blocks = build_blocks(words, page_width=595, page_height=842)
    assert isinstance(blocks[0], ListItem)
    assert blocks[0].ordered
    assert "First item" in blocks[0].text


def test_bullet_list_detected():
    words = [LayoutWord(text=w, x0=40 + i * 30, y0=40, x1=60 + i * 30, y1=52, size_pt=11) for i, w in enumerate(["-", "Bullet", "point"])]
    blocks = build_blocks(words, page_width=595, page_height=842)
    assert isinstance(blocks[0], ListItem)
    assert not blocks[0].ordered


def _list_line(marker: str, text: str, x0: float, y0: float) -> list[LayoutWord]:
    words = [marker] + text.split()
    return [
        LayoutWord(text=w, x0=x0 + i * 30, y0=y0, x1=x0 + i * 30 + 20, y1=y0 + 12, size_pt=11)
        for i, w in enumerate(words)
    ]


def test_nested_list_levels_from_indent():
    """Уровень вложенности пункта списка определяется по относительному
    отступу (indent_pt), а не всегда 0 — иначе вложенные списки в DOCX
    визуально неотличимы от списка без вложенности (п.4 ТЗ: сохранять
    структуру списков)."""
    words: list[LayoutWord] = []
    words += _list_line("1.", "Top level one", x0=40, y0=40)
    words += _list_line("-", "Nested bullet", x0=70, y0=60)
    words += _list_line("-", "Nested bullet two", x0=70, y0=80)
    words += _list_line("2.", "Top level two", x0=40, y0=100)
    blocks = build_blocks(words, page_width=595, page_height=842)

    list_items = [b for b in blocks if isinstance(b, ListItem)]
    assert [li.level for li in list_items] == [0, 1, 1, 0]


def test_paragraph_space_before_reflects_real_gap():
    """Регрессия: `Paragraph.space_before_pt` раньше существовал в модели,
    но build_blocks никогда его не заполнял — DOCX-рендерер получал везде
    0 и полагался на жёстко зашитый в шаблон Word отступ (10pt + 1.15×),
    независимо от того, насколько плотно расположен текст в оригинале."""
    words = [
        LayoutWord(text="First", x0=40, y0=40, x1=90, y1=52, size_pt=11),
        LayoutWord(text="paragraph.", x0=95, y0=40, x1=160, y1=52, size_pt=11),
        # Большой разрыв (98pt) перед следующим абзацем — явно больше, чем
        # обычный межстрочный интервал.
        LayoutWord(text="Second", x0=40, y0=150, x1=95, y1=162, size_pt=11),
        LayoutWord(text="paragraph.", x0=100, y0=150, x1=165, y1=162, size_pt=11),
    ]
    blocks = build_blocks(words, page_width=595, page_height=842)
    paragraphs = [b for b in blocks if isinstance(b, Paragraph)]
    assert len(paragraphs) == 2
    assert paragraphs[0].space_before_pt == 0.0  # первый блок на странице
    assert paragraphs[1].space_before_pt == 98.0  # 150 - 52
