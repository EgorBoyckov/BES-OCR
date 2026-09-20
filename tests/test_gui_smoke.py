"""GUI smoke-тесты (headless, QT_QPA_PLATFORM=offscreen)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QApplication

from bes_ocr import __app_name__


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def test_main_window_creates(qapp):
    from bes_ocr.gui.main_window import MainWindow

    window = MainWindow()
    assert __app_name__ in window.windowTitle()
    assert window.btn_convert.isEnabled()
    assert not window.btn_cancel.isEnabled()


def test_add_files_updates_list(qapp, text_pdf):
    from bes_ocr.gui.main_window import MainWindow

    window = MainWindow()
    window._add_files([text_pdf])
    assert window.selected_files == [text_pdf]
    assert window.file_list.count() == 1


def test_convert_button_requires_files(qapp, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from bes_ocr.gui.main_window import MainWindow

    window = MainWindow()
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    window._start_conversion()
    assert window.worker is None


def test_full_conversion_via_worker(qapp, text_pdf, tmp_path):
    from bes_ocr.gui.main_window import MainWindow

    window = MainWindow()
    window._add_files([text_pdf])
    window.output_dir = str(tmp_path)
    window._start_conversion()
    assert window.worker is not None

    import time

    start = time.time()
    while window.worker.isRunning() and time.time() - start < 30:
        QCoreApplication.processEvents()
        time.sleep(0.05)

    assert not window.worker.isRunning()
    produced = list(tmp_path.glob("*.docx"))
    assert len(produced) == 1
