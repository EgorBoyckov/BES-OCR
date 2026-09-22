"""Фоновая обработка PDF в отдельном потоке Qt, чтобы GUI не зависал."""
from __future__ import annotations

import os
import time

from PySide6.QtCore import QThread, Signal

from ..config.settings import Settings
from ..core.docx_writer import build_docx
from ..core.errors import BesOcrError, CancelledError
from ..core.pipeline import CancellationToken, ProgressEvent, process_document


class ConversionWorker(QThread):
    file_started = Signal(str, int, int)  # file_name, index(1-based), total_files
    progress = Signal(str, str, int, int)  # file_name, stage, current, total
    file_done = Signal(str, str, float, list, int, int)  # file_name, output_path, elapsed_s, warnings, n_tables, n_images
    file_failed = Signal(str, str)  # file_name, error message
    all_done = Signal()

    def __init__(self, pdf_paths: list[str], output_dir: str, settings: Settings, parent=None):
        super().__init__(parent)
        self.pdf_paths = pdf_paths
        self.output_dir = output_dir
        self.settings = settings
        self.cancel_token = CancellationToken()

    def cancel(self) -> None:
        self.cancel_token.cancel()

    def run(self) -> None:
        total_files = len(self.pdf_paths)
        for index, pdf_path in enumerate(self.pdf_paths, start=1):
            file_name = os.path.basename(pdf_path)
            start = time.monotonic()
            if self.cancel_token.is_cancelled:
                break
            self.file_started.emit(file_name, index, total_files)
            try:
                def on_progress(evt: ProgressEvent, fn=file_name):
                    self.progress.emit(fn, evt.stage, evt.current_page, evt.total_pages)

                document = process_document(
                    pdf_path, self.settings, progress_cb=on_progress, cancel_token=self.cancel_token
                )
                out_name = os.path.splitext(file_name)[0] + ".docx"
                out_path = os.path.join(self.output_dir, out_name)
                build_docx(document, out_path)
                elapsed = time.monotonic() - start
                warnings = [f"стр. {w.page_number}: {w.message}" for w in document.warnings]
                n_tables = sum(1 for page in document.pages for b in page.blocks if hasattr(b, "n_rows"))
                n_images = sum(
                    1 for page in document.pages for b in page.blocks if type(b).__name__ == "ImageBlock"
                )
                self.file_done.emit(file_name, out_path, elapsed, warnings, n_tables, n_images)
            except CancelledError:
                break
            except BesOcrError as exc:
                self.file_failed.emit(file_name, str(exc))
            except Exception as exc:  # noqa: BLE001
                self.file_failed.emit(file_name, f"Неожиданная ошибка: {exc}")

        self.all_done.emit()
