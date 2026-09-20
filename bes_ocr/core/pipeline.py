"""Обработка документа целиком: страницы (потоково/параллельно), колонтитулы,
сборка итоговой модели Document. Ошибка одной страницы не прерывает документ.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Optional

from ..config.settings import Settings
from .errors import CancelledError, PageProcessingError, PdfOpenError
from .models import Document, Page, Paragraph, Run
from .ocr_engine import OcrEngine
from .page_processor import process_page
from .pdf_source import PdfSource

logger = logging.getLogger("bes_ocr")


@dataclass
class ProgressEvent:
    stage: str
    current_page: int
    total_pages: int


ProgressCallback = Callable[[ProgressEvent], None]


class CancellationToken:
    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled:
            raise CancelledError("Обработка отменена пользователем")


def _placeholder_page(page_number: int, message: str) -> Page:
    page = Page(number=page_number)
    page.blocks.append(Paragraph(runs=[Run(text=f"[Страница не распознана: {message}]")]))
    return page


def _detect_repeated_line(pages: list[Page], take_first: bool) -> Optional[str]:
    """Ищет строку, повторяющуюся на большинстве страниц в начале/конце — колонтитул."""
    counter: Counter[str] = Counter()
    for page in pages:
        if not page.blocks:
            continue
        block = page.blocks[0] if take_first else page.blocks[-1]
        text = getattr(block, "text", "").strip()
        if text and len(text) < 120:
            counter[text] += 1
    if not counter:
        return None
    text, count = counter.most_common(1)[0]
    if count >= max(2, int(len(pages) * 0.6)):
        return text
    return None


def process_document(
    pdf_path: str,
    settings: Settings,
    progress_cb: Optional[ProgressCallback] = None,
    cancel_token: Optional[CancellationToken] = None,
) -> Document:
    if not os.path.isfile(pdf_path):
        raise PdfOpenError(f"Файл не найден: {pdf_path}")

    cancel_token = cancel_token or CancellationToken()
    ocr_engine = OcrEngine(settings)

    def emit(stage: str, current: int, total: int) -> None:
        if progress_cb:
            progress_cb(ProgressEvent(stage=stage, current_page=current, total_pages=total))

    emit("Открытие PDF", 0, 0)
    with PdfSource(pdf_path) as pdf:
        total_pages = pdf.page_count
        document = Document(source_filename=os.path.basename(pdf_path))

        pages: list[Optional[Page]] = [None] * total_pages
        emit("Обработка страниц", 0, total_pages)

        def worker(page_index: int) -> tuple[int, Optional[Page], Optional[str]]:
            cancel_token.raise_if_cancelled()
            try:
                page = process_page(pdf_path, pdf, page_index, settings, ocr_engine)
                return page_index, page, None
            except PageProcessingError as exc:
                logger.warning("Ошибка обработки страницы %d: %s", page_index + 1, exc)
                return page_index, _placeholder_page(page_index + 1, str(exc)), str(exc)

        completed = 0
        with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
            futures = [executor.submit(worker, i) for i in range(total_pages)]
            try:
                for future in futures:
                    cancel_token.raise_if_cancelled()
                    idx, page, err = future.result()
                    pages[idx] = page
                    if err:
                        document.add_warning(idx + 1, err)
                    completed += 1
                    emit("Обработка страниц", completed, total_pages)
            except CancelledError:
                for f in futures:
                    f.cancel()
                raise

        document.pages = [p for p in pages if p is not None]

    emit("Анализ колонтитулов", total_pages, total_pages)
    header_text = _detect_repeated_line(document.pages, take_first=True)
    footer_text = _detect_repeated_line(document.pages, take_first=False)
    if header_text:
        from .models import HeaderFooterText

        document.header = HeaderFooterText(text=header_text, is_header=True)
        for page in document.pages:
            if page.blocks and getattr(page.blocks[0], "text", "").strip() == header_text:
                page.blocks.pop(0)
    if footer_text:
        from .models import HeaderFooterText

        document.footer = HeaderFooterText(text=footer_text, is_header=False)
        for page in document.pages:
            if page.blocks and getattr(page.blocks[-1], "text", "").strip() == footer_text:
                page.blocks.pop()

    emit("Готово", total_pages, total_pages)
    return document
