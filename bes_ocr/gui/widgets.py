"""Вспомогательные виджеты: область drag-and-drop и список файлов."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QListWidget, QVBoxLayout, QWidget


class DropArea(QWidget):
    """Область для перетаскивания PDF-файлов."""

    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(140)
        self.setObjectName("dropArea")

        layout = QVBoxLayout(self)
        self.label = QLabel("Перетащите PDF-файлы сюда\nили нажмите «Выбрать PDF»")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.setStyleSheet(
            """
            #dropArea {
                border: 2px dashed #9aa5b1;
                border-radius: 10px;
                background: #f7f9fb;
            }
            """
        )

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(u.toLocalFile().lower().endswith(".pdf") for u in urls):
                event.acceptProposedAction()
                self.setStyleSheet(
                    "#dropArea { border: 2px dashed #2f7fe0; border-radius: 10px; background: #eaf2fd; }"
                )

    def dragLeaveEvent(self, event):  # noqa: N802
        self.setStyleSheet(
            "#dropArea { border: 2px dashed #9aa5b1; border-radius: 10px; background: #f7f9fb; }"
        )

    def dropEvent(self, event):  # noqa: N802
        self.setStyleSheet(
            "#dropArea { border: 2px dashed #9aa5b1; border-radius: 10px; background: #f7f9fb; }"
        )
        paths = [
            u.toLocalFile()
            for u in event.mimeData().urls()
            if u.toLocalFile().lower().endswith(".pdf")
        ]
        if paths:
            self.files_dropped.emit(paths)


class FileListWidget(QListWidget):
    pass
