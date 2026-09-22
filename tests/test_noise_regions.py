"""Тесты поиска локальных пятен визуального шума (печати/подписи) — п.3 ТЗ.

Ключевой урок с реального документа: доверие (confidence) отдельного слова
здесь ненадёжно — Tesseract часто уверенно "читает" мусор ростчерка подписи
как короткие псевдослова вперемешку с настоящим текстом; надёжнее
ориентироваться на плотное скопление коротких/невнятных слов.
"""
from bes_ocr.config.settings import Settings
from bes_ocr.core.ocr_engine import OcrWord, find_noise_regions


def _word(text, x0, y0, conf, size=30):
    return OcrWord(text=text, x0=x0, y0=y0, x1=x0 + size, y1=y0 + size, confidence=conf, line_id=0)


def test_dense_short_garbage_cluster_is_flagged():
    settings = Settings()
    words = [
        _word("L", 100, 100, 69),
        _word("ya", 128, 105, 49),
        _word("&", 158, 98, 89),
        _word("ль", 188, 102, 52),
        _word("PERLE", 100, 132, 38),
        _word("6K", 220, 134, 9),
        _word("ЗЕ", 250, 130, 62),
        _word("Я", 100, 164, 39),
        _word("+", 220, 166, 74),
    ]
    regions = find_noise_regions(words, settings, page_width=2000, page_height=3000)
    assert len(regions) == 1
    x0, y0, x1, y1 = regions[0]
    assert x0 <= 100 and y0 <= 98
    assert x1 >= 280 and y1 >= 190


def test_clean_paragraph_is_not_flagged():
    settings = Settings()
    words = [
        _word(w, 100 + i * 120, 100, 95, size=100)
        for i, w in enumerate(["Обычный", "печатный", "абзац", "текста", "документа"])
    ]
    regions = find_noise_regions(words, settings, page_width=2000, page_height=3000)
    assert regions == []


def test_short_wrapped_table_values_not_flagged():
    """Короткие значения в узкой колонке таблицы (даты, номера) не должны
    ложно приниматься за пятно шума — они распознаются уверенно."""
    settings = Settings()
    words = [
        _word("с", 100, 100, 96, size=20),
        _word("12.01.2026", 100, 130, 94, size=90),
        _word("по", 100, 160, 95, size=20),
        _word("07.02.2026", 100, 190, 93, size=90),
    ]
    regions = find_noise_regions(words, settings, page_width=2000, page_height=3000)
    assert regions == []


def test_empty_words_returns_no_regions():
    settings = Settings()
    assert find_noise_regions([], settings, page_width=2000, page_height=3000) == []
