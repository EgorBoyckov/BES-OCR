"""Оценка кегля текста OCR по метрикам глифов (core/font_metrics.py)."""
import pytest

from bes_ocr.core import font_metrics


@pytest.mark.parametrize("text", ["программа", "компоненты", "Университет", "(ФИО,", "12.01.2026", "обучения"])
def test_size_from_height_accounts_for_ascenders_and_descenders(text):
    """Регрессия: раньше кегль = высота рамки слова × 0.8, из-за чего слова
    с выносными элементами ("программа") и без них ("компоненты") в одной
    строке получали кегль, различающийся почти в полтора раза."""
    top, bottom = font_metrics.vertical_extent_em(text)
    height = (top + bottom) * 12.0
    assert font_metrics.size_from_height(text, height) == pytest.approx(12.0, rel=1e-6)


def test_size_from_width_uses_ink_width():
    text = "образовательной"
    width = font_metrics.ink_width_em(text) * 10.0
    assert font_metrics.size_from_width(text, width) == pytest.approx(10.0, rel=1e-6)
    # Полужирное начертание шире — оценка по обычным ширинам была бы завышена.
    bold_width = font_metrics.ink_width_em(text, bold=True) * 10.0
    assert font_metrics.size_from_width(text, bold_width, bold=True) == pytest.approx(10.0, rel=1e-6)
    assert font_metrics.size_from_width(text, bold_width) > 10.0


def test_short_and_unknown_words_give_no_width_estimate():
    assert font_metrics.size_from_width("по", 10.0) is None
    assert font_metrics.size_from_width("日本語テキスト", 50.0) is None
