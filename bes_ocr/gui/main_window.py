"""Главное окно BES OCR."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..config.settings import DEFAULT_SETTINGS
from ..jobs.worker import ConversionWorker
from ..logging_setup import build_logger
from .widgets import DropArea, FileListWidget


def _open_folder(path: str) -> None:
    try:
        if sys.platform.startswith("linux"):
            subprocess.Popen(["xdg-open", path])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    except Exception:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} — PDF → Word")
        self.resize(760, 640)

        self.settings = DEFAULT_SETTINGS
        self.logger, self.log_queue = build_logger(self.settings.log_dir)
        self.selected_files: list[str] = []
        self.output_dir: str = os.path.expanduser("~")
        self.worker: ConversionWorker | None = None
        self.start_time: float = 0.0
        self.last_output_dir: str | None = None

        self._build_ui()

        self.log_timer = QTimer(self)
        self.log_timer.timeout.connect(self._drain_log_queue)
        self.log_timer.start(300)

        self.elapsed_timer = QTimer(self)
        self.elapsed_timer.timeout.connect(self._update_elapsed)

    # ---------- UI construction ----------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(12)
        root.setContentsMargins(20, 20, 20, 20)

        title = QLabel(__app_name__)
        title_font = QFont()
        title_font.setPointSize(22)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("PDF → Word")
        subtitle.setStyleSheet("color: #5a6472; font-size: 13px;")
        root.addWidget(title)
        root.addWidget(subtitle)

        self.drop_area = DropArea()
        self.drop_area.files_dropped.connect(self._add_files)
        root.addWidget(self.drop_area)

        file_buttons = QHBoxLayout()
        self.btn_select = QPushButton("Выбрать PDF…")
        self.btn_select.clicked.connect(self._select_files)
        self.btn_clear = QPushButton("Очистить список")
        self.btn_clear.clicked.connect(self._clear_files)
        file_buttons.addWidget(self.btn_select)
        file_buttons.addWidget(self.btn_clear)
        file_buttons.addStretch(1)
        root.addLayout(file_buttons)

        self.file_list = FileListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setMaximumHeight(120)
        root.addWidget(self.file_list)

        out_layout = QHBoxLayout()
        self.out_label = QLabel(f"Папка сохранения: {self.output_dir}")
        self.btn_out = QPushButton("Выбрать папку…")
        self.btn_out.clicked.connect(self._select_output_dir)
        out_layout.addWidget(self.out_label, 1)
        out_layout.addWidget(self.btn_out)
        root.addLayout(out_layout)

        action_layout = QHBoxLayout()
        self.btn_convert = QPushButton("Конвертировать в Word")
        self.btn_convert.setStyleSheet(
            "QPushButton { background:#2f7fe0; color:white; font-weight:600; padding:8px 16px; border-radius:6px; }"
            "QPushButton:disabled { background:#a9c4e8; }"
        )
        self.btn_convert.clicked.connect(self._start_conversion)
        self.btn_cancel = QPushButton("Отменить")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        action_layout.addWidget(self.btn_convert)
        action_layout.addWidget(self.btn_cancel)
        action_layout.addStretch(1)
        root.addLayout(action_layout)

        self.stage_label = QLabel("")
        self.page_label = QLabel("")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        root.addWidget(self.stage_label)
        root.addWidget(self.progress_bar)
        root.addWidget(self.page_label)

        status_layout = QHBoxLayout()
        self.elapsed_label = QLabel("Время обработки: —")
        self.btn_open_folder = QPushButton("Открыть папку с результатом")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_result_folder)
        status_layout.addWidget(self.elapsed_label)
        status_layout.addStretch(1)
        status_layout.addWidget(self.btn_open_folder)
        root.addLayout(status_layout)

        root.addWidget(QLabel("Журнал:"))
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        root.addWidget(self.log_view, 1)

        version_label = QLabel(f"версия {__version__}")
        version_label.setStyleSheet("color:#9aa5b1; font-size:11px;")
        version_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        root.addWidget(version_label)

    # ---------- file selection ----------
    def _select_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выбор PDF", "", "PDF файлы (*.pdf)")
        if paths:
            self._add_files(paths)

    def _add_files(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self.selected_files:
                self.selected_files.append(p)
                self.file_list.addItem(QListWidgetItem(os.path.basename(p)))

    def _clear_files(self) -> None:
        self.selected_files.clear()
        self.file_list.clear()

    def _select_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка сохранения", self.output_dir)
        if path:
            self.output_dir = path
            self.out_label.setText(f"Папка сохранения: {self.output_dir}")

    # ---------- conversion ----------
    def _start_conversion(self) -> None:
        if not self.selected_files:
            QMessageBox.warning(self, "Нет файлов", "Сначала выберите или перетащите хотя бы один PDF-файл.")
            return
        if not os.path.isdir(self.output_dir):
            QMessageBox.critical(self, "Ошибка", "Папка сохранения недоступна.")
            return

        self.btn_convert.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open_folder.setEnabled(False)
        self.progress_bar.setValue(0)
        self.start_time = time.monotonic()
        self.elapsed_timer.start(1000)

        self.worker = ConversionWorker(list(self.selected_files), self.output_dir, self.settings)
        self.worker.progress.connect(self._on_progress)
        self.worker.file_done.connect(self._on_file_done)
        self.worker.file_failed.connect(self._on_file_failed)
        self.worker.all_done.connect(self._on_all_done)
        self.worker.start()

    def _cancel_conversion(self) -> None:
        if self.worker:
            self.worker.cancel()
            self.stage_label.setText("Отмена…")
            self.logger.info("Пользователь запросил отмену обработки")

    def _on_progress(self, file_name: str, stage: str, current: int, total: int) -> None:
        self.stage_label.setText(f"{file_name}: {stage}")
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))
            self.page_label.setText(f"Страница {current} из {total}")
        else:
            self.page_label.setText("")

    def _on_file_done(self, file_name: str, out_path: str, elapsed: float, warnings: list[str]) -> None:
        self.last_output_dir = os.path.dirname(out_path)
        msg = f"Готово: {file_name} → {os.path.basename(out_path)} ({elapsed:.1f} с)"
        if warnings:
            msg += f" — предупреждений: {len(warnings)}"
            for w in warnings:
                self.logger.warning("%s: %s", file_name, w)
        self.logger.info(msg)

    def _on_file_failed(self, file_name: str, message: str) -> None:
        self.logger.error("Не удалось обработать %s: %s", file_name, message)

    def _on_all_done(self) -> None:
        self.btn_convert.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.elapsed_timer.stop()
        self.stage_label.setText("Обработка завершена")
        self.page_label.setText("")
        self.progress_bar.setValue(100)
        if self.last_output_dir:
            self.btn_open_folder.setEnabled(True)
            QMessageBox.information(
                self, "Готово", "Конвертация завершена. Результаты сохранены в выбранной папке."
            )

    def _update_elapsed(self) -> None:
        elapsed = time.monotonic() - self.start_time
        self.elapsed_label.setText(f"Время обработки: {elapsed:.0f} с")

    def _open_result_folder(self) -> None:
        if self.last_output_dir:
            _open_folder(self.last_output_dir)

    def _drain_log_queue(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            self.log_view.appendPlainText(line)
