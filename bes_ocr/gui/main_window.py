"""Главное окно BES OCR."""
from __future__ import annotations

import os
import subprocess
import sys
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QTextCharFormat, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
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
from .settings_dialog import SettingsDialog
from .widgets import DropArea, FileListWidget, FileStatus

_ACCENT = "#2f7fe0"
_TEXT_MUTED = "#5a6472"
_BG = "#f4f6f9"


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


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(f"color:{_TEXT_MUTED}; font-size: 12px; font-weight: 600; letter-spacing: 0.3px;")
    return lbl


def _hline() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setStyleSheet("color: #e3e7ed;")
    return line


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{__app_name__} — PDF → Word")
        self.resize(820, 720)
        self.setStyleSheet(f"QMainWindow {{ background: {_BG}; }}")

        self.settings = DEFAULT_SETTINGS
        self.logger, self.log_queue = build_logger(self.settings.log_dir)
        self.selected_files: list[str] = []
        self.output_dir: str = os.path.expanduser("~")
        self.worker: ConversionWorker | None = None
        self.start_time: float = 0.0
        self.last_output_dir: str | None = None
        self.total_files_in_run: int = 0
        self.output_paths: dict[str, str] = {}

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
        root.setSpacing(14)
        root.setContentsMargins(24, 22, 24, 18)

        header_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel(__app_name__)
        title_font = QFont()
        title_font.setPointSize(23)
        title_font.setBold(True)
        title.setFont(title_font)
        subtitle = QLabel("PDF → Word · локальное распознавание и восстановление структуры документа")
        subtitle.setStyleSheet(f"color: {_TEXT_MUTED}; font-size: 12px;")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_row.addLayout(title_box)
        header_row.addStretch(1)
        self.btn_settings = QPushButton("Настройки…")
        self.btn_settings.setFlat(True)
        self.btn_settings.setStyleSheet(f"color:{_ACCENT}; font-size: 12px; border: none;")
        self.btn_settings.clicked.connect(self._open_settings_dialog)
        header_row.addWidget(self.btn_settings, alignment=Qt.AlignmentFlag.AlignTop)
        version_label = QLabel(f"версия {__version__}")
        version_label.setStyleSheet("color:#9aa5b1; font-size:11px;")
        header_row.addWidget(version_label, alignment=Qt.AlignmentFlag.AlignTop)
        root.addLayout(header_row)
        root.addWidget(_hline())

        # ---- выбор файлов ----
        root.addWidget(_section_label("ФАЙЛЫ"))
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
        self.files_count_label = QLabel("Файлов не выбрано")
        self.files_count_label.setStyleSheet(f"color:{_TEXT_MUTED}; font-size:12px;")
        file_buttons.addWidget(self.files_count_label)
        root.addLayout(file_buttons)

        self.file_list = FileListWidget()
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.file_list.setMaximumHeight(130)
        self.file_list.setStyleSheet(
            "QListWidget { background:white; border:1px solid #e3e7ed; border-radius:8px; }"
        )
        self.file_list.setToolTip("Двойной клик по готовому файлу открывает результат")
        self.file_list.itemDoubleClicked.connect(self._on_file_item_double_clicked)
        root.addWidget(self.file_list)

        # ---- папка сохранения ----
        root.addWidget(_section_label("СОХРАНЕНИЕ"))
        out_layout = QHBoxLayout()
        self.out_label = QLabel(f"Папка: {self.output_dir}")
        self.out_label.setStyleSheet("font-size: 12px;")
        self.btn_out = QPushButton("Выбрать папку…")
        self.btn_out.clicked.connect(self._select_output_dir)
        out_layout.addWidget(self.out_label, 1)
        out_layout.addWidget(self.btn_out)
        root.addLayout(out_layout)

        # ---- действия ----
        action_layout = QHBoxLayout()
        self.btn_convert = QPushButton("Конвертировать в Word")
        self.btn_convert.setStyleSheet(
            f"QPushButton {{ background:{_ACCENT}; color:white; font-weight:600; padding:9px 18px;"
            " border-radius:7px; font-size: 13px; }"
            " QPushButton:disabled { background:#a9c4e8; }"
            " QPushButton:hover:!disabled { background:#2568bd; }"
        )
        self.btn_convert.clicked.connect(self._start_conversion)
        self.btn_cancel = QPushButton("Отменить")
        self.btn_cancel.setStyleSheet(
            "QPushButton { padding:9px 18px; border-radius:7px; font-size: 13px; }"
        )
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self._cancel_conversion)
        action_layout.addWidget(self.btn_convert)
        action_layout.addWidget(self.btn_cancel)
        action_layout.addStretch(1)
        root.addLayout(action_layout)

        # ---- прогресс ----
        root.addWidget(_hline())
        root.addWidget(_section_label("ПРОГРЕСС"))

        self.batch_label = QLabel("")
        self.batch_label.setStyleSheet("font-size: 12px; font-weight: 600;")
        root.addWidget(self.batch_label)

        self.stage_label = QLabel("Ожидание запуска")
        self.stage_label.setStyleSheet("font-size: 13px;")
        root.addWidget(self.stage_label)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet(
            "QProgressBar { border:1px solid #e3e7ed; border-radius:6px; background:white; height:18px; text-align:center; }"
            f"QProgressBar::chunk {{ background:{_ACCENT}; border-radius:6px; }}"
        )
        root.addWidget(self.progress_bar)

        self.page_label = QLabel("")
        self.page_label.setStyleSheet(f"color:{_TEXT_MUTED}; font-size: 12px;")
        root.addWidget(self.page_label)

        status_layout = QHBoxLayout()
        self.elapsed_label = QLabel("Время обработки: —")
        self.elapsed_label.setStyleSheet("font-size: 12px;")
        self.btn_open_folder = QPushButton("Открыть папку с результатом")
        self.btn_open_folder.setEnabled(False)
        self.btn_open_folder.clicked.connect(self._open_result_folder)
        status_layout.addWidget(self.elapsed_label)
        status_layout.addStretch(1)
        status_layout.addWidget(self.btn_open_folder)
        root.addLayout(status_layout)

        # ---- журнал ----
        root.addWidget(_hline())
        log_header = QHBoxLayout()
        log_header.addWidget(_section_label("ЖУРНАЛ"))
        log_header.addStretch(1)
        self.btn_clear_log = QPushButton("Очистить")
        self.btn_clear_log.setFlat(True)
        self.btn_clear_log.setStyleSheet(f"color:{_ACCENT}; font-size: 12px; border: none;")
        self.btn_clear_log.clicked.connect(lambda: self.log_view.clear())
        log_header.addWidget(self.btn_clear_log)
        root.addLayout(log_header)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(3000)
        self.log_view.setStyleSheet(
            "QPlainTextEdit { background:#12161c; color:#d7dde5; border-radius:8px; padding:8px;"
            " font-family: 'DejaVu Sans Mono', monospace; font-size: 11px; }"
        )
        root.addWidget(self.log_view, 1)

    # ---------- file selection ----------
    def _select_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Выбор PDF", "", "PDF файлы (*.pdf)")
        if paths:
            self._add_files(paths)

    def _add_files(self, paths: list[str]) -> None:
        for p in paths:
            if p not in self.selected_files:
                self.selected_files.append(p)
                self.file_list.add_file(p, os.path.basename(p))
        self._update_files_count_label()

    def _clear_files(self) -> None:
        self.selected_files.clear()
        self.file_list.clear_files()
        self.output_paths.clear()
        self._update_files_count_label()

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == SettingsDialog.DialogCode.Accepted:
            self.settings = dialog.result_settings()
            self.logger.info(
                "Настройки изменены: язык=%s, DPI=%d, потоков=%d",
                self.settings.ocr_languages,
                self.settings.render_dpi,
                self.settings.max_workers,
            )

    def _on_file_item_double_clicked(self, item) -> None:
        row = self.file_list.row(item)
        if 0 <= row < len(self.selected_files):
            path = self.selected_files[row]
            out_path = self.output_paths.get(path)
            if out_path and os.path.isfile(out_path):
                _open_folder(out_path)

    def _update_files_count_label(self) -> None:
        n = len(self.selected_files)
        if n == 0:
            self.files_count_label.setText("Файлов не выбрано")
        else:
            word = "файл" if n % 10 == 1 and n % 100 != 11 else ("файла" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "файлов")
            self.files_count_label.setText(f"Выбрано: {n} {word}")

    def _select_output_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Папка сохранения", self.output_dir)
        if path:
            self.output_dir = path
            self.out_label.setText(f"Папка: {self.output_dir}")

    # ---------- conversion ----------
    def _start_conversion(self) -> None:
        if not self.selected_files:
            QMessageBox.warning(self, "Нет файлов", "Сначала выберите или перетащите хотя бы один PDF-файл.")
            return
        if not os.path.isdir(self.output_dir):
            QMessageBox.critical(self, "Ошибка", "Папка сохранения недоступна.")
            return

        for p in self.selected_files:
            self.file_list.set_status(p, os.path.basename(p), FileStatus.PENDING)

        self.btn_convert.setEnabled(False)
        self.btn_select.setEnabled(False)
        self.btn_clear.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.btn_open_folder.setEnabled(False)
        self.progress_bar.setValue(0)
        self.start_time = time.monotonic()
        self.total_files_in_run = len(self.selected_files)
        self.elapsed_timer.start(1000)

        self.worker = ConversionWorker(list(self.selected_files), self.output_dir, self.settings)
        self.worker.file_started.connect(self._on_file_started)
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

    def _path_for_filename(self, file_name: str) -> str | None:
        for p in self.selected_files:
            if os.path.basename(p) == file_name:
                return p
        return None

    def _on_file_started(self, file_name: str, index: int, total: int) -> None:
        self.batch_label.setText(f"Файл {index} из {total}: {file_name}")
        p = self._path_for_filename(file_name)
        if p:
            self.file_list.set_status(p, file_name, FileStatus.PROCESSING)
        self.progress_bar.setValue(0)
        self.page_label.setText("")

    def _on_progress(self, file_name: str, stage: str, current: int, total: int) -> None:
        self.stage_label.setText(stage)
        if total > 0:
            self.progress_bar.setValue(int(current / total * 100))
            self.page_label.setText(f"Страница {current} из {total}")
        else:
            self.page_label.setText("")

    def _on_file_done(
        self, file_name: str, out_path: str, elapsed: float, warnings: list[str], n_tables: int, n_images: int
    ) -> None:
        self.last_output_dir = os.path.dirname(out_path)
        detail = f"{elapsed:.1f} с, таблиц: {n_tables}, изобр.: {n_images}"
        if warnings:
            detail += f", предупреждений: {len(warnings)}"
        p = self._path_for_filename(file_name)
        if p:
            self.file_list.set_status(p, file_name, FileStatus.DONE, detail)
            self.output_paths[p] = out_path
        msg = f"Готово: {file_name} → {os.path.basename(out_path)} ({detail})"
        if warnings:
            for w in warnings:
                self.logger.warning("%s: %s", file_name, w)
        self.logger.info(msg)

    def _on_file_failed(self, file_name: str, message: str) -> None:
        p = self._path_for_filename(file_name)
        if p:
            self.file_list.set_status(p, file_name, FileStatus.FAILED, message[:60])
        self.logger.error("Не удалось обработать %s: %s", file_name, message)

    def _on_all_done(self) -> None:
        self.btn_convert.setEnabled(True)
        self.btn_select.setEnabled(True)
        self.btn_clear.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.elapsed_timer.stop()
        self.stage_label.setText("Обработка завершена")
        self.page_label.setText("")
        self.progress_bar.setValue(100)
        if self.last_output_dir:
            self.btn_open_folder.setEnabled(True)
            n_done = sum(1 for p in self.selected_files if self.file_list.status_of(p) == FileStatus.DONE)
            n_failed = sum(1 for p in self.selected_files if self.file_list.status_of(p) == FileStatus.FAILED)
            summary = f"Обработано файлов: {n_done} из {len(self.selected_files)}."
            if n_failed:
                summary += f" С ошибками: {n_failed}."
            QMessageBox.information(self, "Готово", summary)

    def _update_elapsed(self) -> None:
        elapsed = time.monotonic() - self.start_time
        self.elapsed_label.setText(f"Время обработки: {elapsed:.0f} с")

    def _open_result_folder(self) -> None:
        if self.last_output_dir:
            _open_folder(self.last_output_dir)

    def _drain_log_queue(self) -> None:
        while not self.log_queue.empty():
            line = self.log_queue.get_nowait()
            fmt = QTextCharFormat()
            if " [WARNING] " in line:
                fmt.setForeground(QColor("#e0a72e"))
            elif " [ERROR] " in line:
                fmt.setForeground(QColor("#e05353"))
            else:
                fmt.setForeground(QColor("#d7dde5"))
            cursor = self.log_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            cursor.insertText(line + "\n", fmt)
            self.log_view.setTextCursor(cursor)
