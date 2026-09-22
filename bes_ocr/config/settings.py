"""Настройки приложения."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class Settings:
    ocr_languages: str = "rus+eng"
    render_dpi: int = 300
    max_workers: int = field(default_factory=lambda: max(1, min(4, os.cpu_count() or 1)))
    min_text_layer_chars_per_page: int = 20
    min_text_layer_garbage_ratio: float = 0.35
    ocr_word_confidence_threshold: float = 35.0
    handwriting_confidence_threshold: float = 25.0
    handwriting_garbage_word_ratio: float = 0.6
    table_line_min_length_ratio: float = 0.25
    # Цветные печати/подписи на сканах — отдельными изображениями поверх
    # текста, а не мусором в OCR (см. core/color_marks.py).
    extract_color_marks: bool = True
    # Шрифт текста, распознанного OCR (у скана нет информации о шрифте;
    # Times New Roman — стандарт официальных документов).
    default_font_name: str = "Times New Roman"
    temp_dir_prefix: str = "bes_ocr_"
    log_dir: str = field(
        default_factory=lambda: os.path.join(
            os.path.expanduser("~"), ".local", "share", "bes-ocr", "logs"
        )
    )


DEFAULT_SETTINGS = Settings()
