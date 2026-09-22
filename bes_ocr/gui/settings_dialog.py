"""Диалог настроек обработки (язык OCR, DPI рендера, число потоков).

Настройки применяются только к текущему сеансу приложения (хранятся в
`MainWindow.settings`, не пишутся на диск) — этого достаточно для ручной
подстройки под конкретный документ/машину, не требуя отдельного механизма
персистентной конфигурации сверх уже существующего `config/settings.py`.
"""
from __future__ import annotations

import copy
import os

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
)

from ..config.settings import Settings


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки обработки")
        self.setMinimumWidth(380)
        self._result_settings = settings

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.lang_edit = QLineEdit(settings.ocr_languages)
        self.lang_edit.setPlaceholderText("напр. rus+eng")
        form.addRow("Языки OCR (Tesseract):", self.lang_edit)

        self.dpi_spin = QSpinBox()
        self.dpi_spin.setRange(150, 600)
        self.dpi_spin.setSingleStep(50)
        self.dpi_spin.setValue(settings.render_dpi)
        form.addRow("DPI рендера скана:", self.dpi_spin)

        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(1, max(16, os.cpu_count() or 1))
        self.threads_spin.setValue(settings.max_workers)
        form.addRow("Потоков обработки страниц:", self.threads_spin)

        layout.addLayout(form)

        hint = QLabel(
            "Более высокий DPI и больше потоков дают точнее и быстрее "
            "распознавание, но требуют больше памяти/CPU. Изменения "
            "действуют только для следующих запусков конвертации."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#5a6472; font-size:11px;")
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def result_settings(self) -> Settings:
        """Возвращает копию исходных настроек с применёнными изменениями —
        не мутирует переданный объект напрямую, чтобы отмена диалога
        (Cancel) гарантированно ничего не меняла."""
        updated = copy.copy(self._result_settings)
        lang = self.lang_edit.text().strip()
        if lang:
            updated.ocr_languages = lang
        updated.render_dpi = self.dpi_spin.value()
        updated.max_workers = self.threads_spin.value()
        return updated
