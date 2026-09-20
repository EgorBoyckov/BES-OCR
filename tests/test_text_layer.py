from bes_ocr.config.settings import Settings
from bes_ocr.core.pdf_source import PdfSource
from bes_ocr.core.text_layer import assess_text_layer


def test_text_layer_usable_for_text_pdf(text_pdf):
    settings = Settings()
    with PdfSource(text_pdf) as pdf:
        result = assess_text_layer(pdf, 0, settings)
    assert result.usable


def test_text_layer_unusable_for_scanned_pdf(russian_scanned_pdf):
    settings = Settings()
    with PdfSource(russian_scanned_pdf) as pdf:
        result = assess_text_layer(pdf, 0, settings)
    assert not result.usable
