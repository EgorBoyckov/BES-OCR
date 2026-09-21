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


def test_full_conversion_via_worker(qapp, text_pdf, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from bes_ocr.gui.main_window import MainWindow
    from bes_ocr.gui.widgets import FileStatus

    # _on_all_done показывает модальный QMessageBox — в headless-тесте без
    # взаимодействия пользователя это блокирует поток навсегда (у самого
    # диалога свой вложенный event loop, который наш processEvents() не
    # прерывает). В реальном приложении это нормально (пользователь
    # нажимает «ОК»), здесь просто убеждаемся, что диалог был бы показан.
    shown = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: shown.append(a))

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
    # QThread.isRunning() становится False сразу после выхода из run(), но
    # поставленные в очередь сигналы (file_done/all_done) в GUI-поток могут
    # быть ещё не обработаны — дадим циклу событий добрать их.
    for _ in range(20):
        QCoreApplication.processEvents()
        time.sleep(0.02)

    produced = list(tmp_path.glob("*.docx"))
    assert len(produced) == 1
    assert window.file_list.status_of(text_pdf) == FileStatus.DONE
    assert shown  # сводка "Готово" была бы показана пользователю


def test_file_list_widget_status_transitions(qapp):
    from bes_ocr.gui.widgets import FileListWidget, FileStatus

    widget = FileListWidget()
    widget.add_file("/tmp/a.pdf", "a.pdf")
    assert widget.status_of("/tmp/a.pdf") == FileStatus.PENDING

    widget.set_status("/tmp/a.pdf", "a.pdf", FileStatus.DONE, "1.2 с, таблиц: 2")
    assert widget.status_of("/tmp/a.pdf") == FileStatus.DONE
    assert "Готово" in widget.item(0).text()
    assert "таблиц: 2" in widget.item(0).text()
