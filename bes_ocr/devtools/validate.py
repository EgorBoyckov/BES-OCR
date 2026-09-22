"""Объективная проверка визуального сходства DOCX-результата с исходным PDF.

Дев-инструмент, не часть поставляемого приложения (LibreOffice не является
рантайм-зависимостью bes_ocr — см. `docs/RESEARCH.md`, раздел про
python-docx). Раньше в этой работе сходство проверялось вручную, разово,
скриптами в scratch-директории (рендер страниц в PNG и просмотр глазами) —
воспроизводимого, объективного числа не было. Этот модуль формализует ровно
ту же проверку, которую мы вручную гоняли весь сеанс (LibreOffice
headless → PDF → сравнение растров), вдохновлено разделом 9
(`visual validation`) документа scan2docx-architecture: page_count_match +
постраничный SSIM как «ssim_layout». Метрики вроде `block_iou`/`text_cer`/
`drift_y` из того документа сюда сознательно не перенесены — они требуют
текстового слоя в рендере (LibreOffice это даёт) и Hungarian-сопоставления
блоков, что уже отдельная, более тяжёлая задача; SSIM+page_count уже ловит
именно тот класс регрессий, который случался на практике в этом проекте
(раздутие на лишние страницы из-за неверного размера шрифта в таблице,
съехавшая геометрия страницы).
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pymupdf

logger = logging.getLogger("bes_ocr.devtools.validate")

_RENDER_DPI = 150.0
_SSIM_C1 = (0.01 * 255) ** 2
_SSIM_C2 = (0.03 * 255) ** 2


class LibreOfficeUnavailable(RuntimeError):
    """soffice не найден в PATH — сравнение невозможно на этой машине."""


@dataclass
class PageSimilarity:
    page_index: int
    ssim: float


@dataclass
class SimilarityReport:
    source_page_count: int
    rendered_page_count: int
    page_count_match: bool
    page_scores: list[PageSimilarity] = field(default_factory=list)

    @property
    def mean_ssim(self) -> float:
        if not self.page_scores:
            return 0.0
        return sum(p.ssim for p in self.page_scores) / len(self.page_scores)

    @property
    def visual_score(self) -> float:
        """Единая сводная оценка 0..1. Раздутие/сжатие числа страниц —
        это уже само по себе серьёзная потеря сходства (весь текст после
        точки расхождения сдвинут), поэтому штрафуется отдельно от SSIM
        совпадающих страниц, а не просто «недостающие страницы не
        сравнивались»."""
        if self.source_page_count == 0:
            return 0.0
        penalty = abs(self.rendered_page_count - self.source_page_count) / self.source_page_count
        return max(0.0, self.mean_ssim - penalty)


def _find_soffice() -> str:
    path = shutil.which("soffice") or shutil.which("libreoffice")
    if not path:
        raise LibreOfficeUnavailable(
            "soffice не найден в PATH — установите пакет libreoffice-writer "
            "для этой проверки (не требуется для самого приложения)."
        )
    return path


def docx_to_pdf(docx_path: str, out_dir: str, timeout: float = 90.0) -> str:
    """Конвертирует DOCX в PDF через LibreOffice headless.

    Первый вызов `soffice.bin` после чистого профиля иногда завершается
    кодом выхода 81 — это внутренний сигнал "перезапуститься после
    инициализации профиля", который обычно обрабатывает сам скрипт-обёртка
    `soffice`, но не гарантированно в headless-режиме с изолированным
    `-env:UserInstallation`. Один повтор с тем же профилем воспроизводимо
    решает это (проверено эмпирически в этом проекте) — второй вызов уже не
    видит "холодный" профиль и отрабатывает с первого раза.
    """
    soffice = _find_soffice()
    profile_dir = tempfile.mkdtemp(prefix="bes_ocr_lo_profile_")
    out_pdf = Path(out_dir) / (Path(docx_path).stem + ".pdf")

    cmd = [
        soffice,
        "--headless",
        f"-env:UserInstallation=file://{profile_dir}",
        "--convert-to",
        "pdf",
        "--outdir",
        out_dir,
        docx_path,
    ]
    for attempt in (1, 2):
        try:
            subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"LibreOffice не ответил за {timeout} с") from exc
        if out_pdf.is_file():
            return str(out_pdf)
        logger.warning("LibreOffice: попытка %d не создала PDF, повтор", attempt)
        time.sleep(0.5)
    raise RuntimeError(f"LibreOffice не смог сконвертировать {docx_path} в PDF")


def _render_pages_gray(pdf_path: str, dpi: float = _RENDER_DPI) -> list[np.ndarray]:
    doc = pymupdf.open(pdf_path)
    try:
        zoom = dpi / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pages = []
        for page in doc:
            pix = page.get_pixmap(matrix=matrix, colorspace=pymupdf.csGRAY)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            pages.append(arr)
        return pages
    finally:
        doc.close()


def _ssim(img_a: np.ndarray, img_b: np.ndarray) -> float:
    """Windowed SSIM (Wang et al.) на паре grayscale-изображений одного
    размера. Реализовано вручную через `cv2.boxFilter` вместо добавления
    зависимости от scikit-image (проект держит минимальный набор
    зависимостей ради офлайн-поставки на Astra Linux, см. CLAUDE.md) —
    формула стандартная, окно 7×7, без гауссова взвешивания (упрощение,
    для сравнения общей раскладки этого достаточно)."""
    if img_a.shape != img_b.shape:
        h = min(img_a.shape[0], img_b.shape[0])
        w = min(img_a.shape[1], img_b.shape[1])
        img_a = cv2.resize(img_a, (w, h))
        img_b = cv2.resize(img_b, (w, h))
    a = img_a.astype(np.float64)
    b = img_b.astype(np.float64)

    ksize = (7, 7)
    mu_a = cv2.boxFilter(a, -1, ksize)
    mu_b = cv2.boxFilter(b, -1, ksize)
    mu_a_sq, mu_b_sq, mu_ab = mu_a * mu_a, mu_b * mu_b, mu_a * mu_b

    sigma_a_sq = cv2.boxFilter(a * a, -1, ksize) - mu_a_sq
    sigma_b_sq = cv2.boxFilter(b * b, -1, ksize) - mu_b_sq
    sigma_ab = cv2.boxFilter(a * b, -1, ksize) - mu_ab

    numerator = (2 * mu_ab + _SSIM_C1) * (2 * sigma_ab + _SSIM_C2)
    denominator = (mu_a_sq + mu_b_sq + _SSIM_C1) * (sigma_a_sq + sigma_b_sq + _SSIM_C2)
    ssim_map = numerator / denominator
    return float(np.clip(ssim_map.mean(), -1.0, 1.0))


def compare(source_pdf: str, docx_path: str, workdir: str | None = None) -> SimilarityReport:
    """Основная точка входа: DOCX → PDF (LibreOffice) → постраничный SSIM
    против исходного PDF. Поднимает `LibreOfficeUnavailable`, если soffice
    не установлен — вызывающий код (CLI/тесты) решает, пропустить проверку
    или считать это ошибкой окружения."""
    own_tmp = workdir is None
    workdir = workdir or tempfile.mkdtemp(prefix="bes_ocr_validate_")
    try:
        rendered_pdf = docx_to_pdf(docx_path, workdir)
        source_pages = _render_pages_gray(source_pdf)
        rendered_pages = _render_pages_gray(rendered_pdf)

        scores = [
            PageSimilarity(page_index=i, ssim=_ssim(src, rendered_pages[i]))
            for i, src in enumerate(source_pages)
            if i < len(rendered_pages)
        ]
        return SimilarityReport(
            source_page_count=len(source_pages),
            rendered_page_count=len(rendered_pages),
            page_count_match=len(source_pages) == len(rendered_pages),
            page_scores=scores,
        )
    finally:
        if own_tmp:
            shutil.rmtree(workdir, ignore_errors=True)
