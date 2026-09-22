"""Вспомогательные виджеты: область drag-and-drop и список файлов со статусом."""
from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget


class DropArea(QWidget):
    """Область для перетаскивания PDF-файлов."""

    files_dropped = Signal(list)

    _STYLE_IDLE = (
        "#dropArea { border: 2px dashed #b7c0cc; border-radius: 12px; background: #f7f9fc; }"
    )
    _STYLE_ACTIVE = (
        "#dropArea { border: 2px dashed #2f7fe0; border-radius: 12px; background: #eaf2fd; }"
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(120)
        self.setObjectName("dropArea")

        layout = QVBoxLayout(self)
        self.label = QLabel("Перетащите PDF-файлы сюда\nили нажмите «Выбрать PDF…» ниже")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setStyleSheet("color:#5a6472; font-size: 13px; border: none; background: transparent;")
        layout.addWidget(self.label)

        self.setStyleSheet(self._STYLE_IDLE)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()
                self.setStyleSheet(self._STYLE_ACTIVE)

    def dragLeaveEvent(self, event):  # noqa: N802
        self.setStyleSheet(self._STYLE_IDLE)

    def dropEvent(self, event):  # noqa: N802
        self.setStyleSheet(self._STYLE_IDLE)
        paths = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.toLocalFile().lower().endswith(".pdf")
        ]
        if paths:
            self.files_dropped.emit(paths)


class FileStatus(Enum):
    PENDING = "Ожидание"
    PROCESSING = "Обработка…"
    DONE = "Готово"
    FAILED = "Ошибка"
    CANCELLED = "Отменено"


_STATUS_COLOR = {
    FileStatus.PENDING: QColor("#8a94a3"),
    FileStatus.PROCESSING: QColor("#2f7fe0"),
    FileStatus.DONE: QColor("#1f9254"),
    FileStatus.FAILED: QColor("#c0392b"),
    FileStatus.CANCELLED: QColor("#8a94a3"),
}


class FileListWidget(QListWidget):
    """Список файлов, где для каждого элемента отдельно отображается статус."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: dict[str, QListWidgetItem] = {}
        self.setAlternatingRowColors(True)

    def add_file(self, path: str, display_name: str) -> None:
        item = QListWidgetItem(f"  {display_name}   —   {FileStatus.PENDING.value}")
        item.setForeground(_STATUS_COLOR[FileStatus.PENDING])
        item.setData(Qt.ItemDataRole.UserRole, FileStatus.PENDING)
        self.addItem(item)
        self._items[path] = item

    def clear_files(self) -> None:
        self.clear()
        self._items.clear()

    def set_status(self, path: str, display_name: str, status: FileStatus, detail: str = "") -> None:
        item = self._items.get(path)
        if item is None:
            return
        suffix = f"   —   {status.value}"
        if detail:
            suffix += f" ({detail})"
        item.setText(f"  {display_name}{suffix}")
        item.setForeground(_STATUS_COLOR[status])
        item.setData(Qt.ItemDataRole.UserRole, status)

    def status_of(self, path: str) -> FileStatus | None:
        item = self._items.get(path)
        return item.data(Qt.ItemDataRole.UserRole) if item else None
