"""Ошибка обработки одной страницы не должна прерывать весь документ (п.13 ТЗ)."""
from unittest.mock import patch

from bes_ocr.config.settings import Settings
from bes_ocr.core.errors import PageProcessingError
from bes_ocr.core.pipeline import process_document


def test_single_page_error_does_not_abort_document(multipage_pdf):
    settings = Settings(max_workers=1)
    call_count = {"n": 0}

    from bes_ocr.core import pipeline as pipeline_mod

    original = pipeline_mod.process_page

    def flaky_process_page(pdf_path, pdf, page_number, settings, ocr_engine):
        call_count["n"] += 1
        if page_number == 2:
            raise PageProcessingError(page_number + 1, "simulated failure on page 3")
        return original(pdf_path, pdf, page_number, settings, ocr_engine)

    with patch("bes_ocr.core.pipeline.process_page", side_effect=flaky_process_page):
        document = process_document(multipage_pdf, settings)

    assert len(document.pages) == 5
    assert any(w.page_number == 3 for w in document.warnings)
    assert "не распознана" in document.pages[2].blocks[0].text
