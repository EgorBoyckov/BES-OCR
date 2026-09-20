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
