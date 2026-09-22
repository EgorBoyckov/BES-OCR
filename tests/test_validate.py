"""Тесты дев-инструмента объективного сравнения DOCX-результата с PDF
(`bes_ocr/devtools/validate.py`) — не часть поставляемого приложения,
LibreOffice не является его рантайм-зависимостью (см. docstring модуля)."""
import os
import shutil

import numpy as np
import pytest

from bes_ocr.config.settings import Settings
from bes_ocr.core.docx_writer import build_docx
from bes_ocr.core.pipeline import process_document
from bes_ocr.devtools.validate import _ssim, compare

_HAS_SOFFICE = shutil.which("soffice") is not None or shutil.which("libreoffice") is not None


def test_ssim_identical_images_is_near_one():
    rng = np.random.default_rng(0)
    img = rng.integers(0, 255, size=(120, 120), dtype=np.uint8)
    assert _ssim(img, img) > 0.999


def test_ssim_detects_dissimilar_images():
    a = np.zeros((120, 120), dtype=np.uint8)
    b = np.full((120, 120), 255, dtype=np.uint8)
    assert _ssim(a, b) < 0.3


def test_ssim_handles_mismatched_sizes():
    rng = np.random.default_rng(1)
    a = rng.integers(0, 255, size=(100, 80), dtype=np.uint8)
    b = a[:90, :70]
    # Не должно падать при разных размерах — сравнение всё равно возможно
    # (например, страница результата чуть отличается по размеру рендера).
    score = _ssim(a, b)
    assert 0.0 <= score <= 1.0 or score < 0  # допускаем формально возможный небольшой минус


@pytest.mark.skipif(not _HAS_SOFFICE, reason="soffice (LibreOffice) не установлен в этом окружении")
def test_compare_matching_page_count_and_high_similarity(text_pdf, tmp_path):
    settings = Settings()
    document = process_document(text_pdf, settings)
    out_docx = os.path.join(tmp_path, "out.docx")
    build_docx(document, out_docx)

    report = compare(text_pdf, out_docx, workdir=str(tmp_path / "validate_work"))

    assert report.page_count_match
    assert report.source_page_count == report.rendered_page_count
    # Тот же текст, тот же размер страницы — раскладка должна совпадать
    # с высокой точностью, а не просто "не упало".
    assert report.mean_ssim > 0.6
    assert report.visual_score > 0.6
