"""Доступ к PDF через PyMuPDF: открытие, рендер страниц, извлечение текста/изображений."""
from __future__ import annotations

from dataclasses import dataclass

import pymupdf as fitz  # PyMuPDF

from .errors import PdfOpenError


@dataclass
class WordBox:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    font_size: float = 11.0
    font_name: str = ""
    bold: bool = False
    italic: bool = False


class PdfSource:
    """Обёртка над fitz.Document с удобными методами для конвейера."""

    def __init__(self, path: str):
        try:
            self._doc = fitz.open(path)
        except Exception as exc:  # noqa: BLE001 - библиотека кидает разные типы
            raise PdfOpenError(f"Не удалось открыть PDF: {exc}") from exc
        if self._doc.is_encrypted:
            if not self._doc.authenticate(""):
                raise PdfOpenError("PDF защищён паролем")
        if self._doc.page_count == 0:
            raise PdfOpenError("PDF не содержит страниц")

    @property
    def page_count(self) -> int:
        return self._doc.page_count

    def page_size_pt(self, page_number: int) -> tuple[float, float]:
        page = self._doc[page_number]
        return page.rect.width, page.rect.height

    def extract_text_words(self, page_number: int) -> list[WordBox]:
        """Извлекает слова текстового слоя с координатами и форматированием."""
        page = self._doc[page_number]
        result: list[WordBox] = []
        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = span.get("text", "")
                    if not text.strip():
                        continue
                    flags = span.get("flags", 0)
                    bbox = span.get("bbox", (0, 0, 0, 0))
                    result.append(
                        WordBox(
                            text=text,
                            x0=bbox[0],
                            y0=bbox[1],
                            x1=bbox[2],
                            y1=bbox[3],
                            font_size=span.get("size", 11.0),
                            font_name=span.get("font", ""),
                            bold=bool(flags & 2 ** 4) or "Bold" in span.get("font", ""),
                            italic=bool(flags & 2 ** 1) or "Italic" in span.get("font", ""),
                        )
                    )
        return result

    def raw_text(self, page_number: int) -> str:
        return self._doc[page_number].get_text("text")

    def render_page_image(self, page_number: int, dpi: int = 300):
        """Возвращает (numpy BGR array, scale) — растр страницы для OCR/детекции таблиц."""
        import numpy as np

        page = self._doc[page_number]
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        img_bgr = img[:, :, ::-1].copy()
        return img_bgr, zoom

    def extract_images(self, page_number: int) -> list[dict]:
        """Извлекает встроенные растровые изображения страницы (не сам рендер)."""
        page = self._doc[page_number]
        images = []
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                base = self._doc.extract_image(xref)
            except Exception:  # noqa: BLE001
                continue
            rects = page.get_image_rects(xref)
            bbox = rects[0] if rects else None
            images.append(
                {
                    "data": base["image"],
                    "ext": base.get("ext", "png"),
                    "width": base.get("width", 0),
                    "height": base.get("height", 0),
                    "bbox": bbox,
                }
            )
        return images

    def close(self) -> None:
        self._doc.close()

    def __enter__(self) -> "PdfSource":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
