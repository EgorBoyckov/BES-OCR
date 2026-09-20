"""Внутренняя модель документа.

Не зависит ни от PDF, ни от DOCX: page_processor/pipeline строят эту модель
из источника (текстовый слой или OCR), а docx_writer превращает её в .docx.
Такое разделение позволяет тестировать генерацию DOCX без PDF и наоборот.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


class Alignment(Enum):
    LEFT = auto()
    CENTER = auto()
    RIGHT = auto()
    JUSTIFY = auto()


@dataclass
class BBox:
    """Координаты в точках PDF (72 dpi), left/top/right/bottom."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0


@dataclass
class Run:
    """Непрерывный фрагмент текста с единым форматированием."""

    text: str
    bold: bool = False
    italic: bool = False
    size_pt: float = 11.0
    font_name: Optional[str] = None


@dataclass
class Paragraph:
    runs: list[Run] = field(default_factory=list)
    alignment: Alignment = Alignment.LEFT
    indent_pt: float = 0.0
    space_before_pt: float = 0.0
    bbox: Optional[BBox] = None

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class Heading(Paragraph):
    level: int = 1  # 1..4


@dataclass
class ListItem(Paragraph):
    level: int = 0
    ordered: bool = False
    marker: str = ""


@dataclass
class TableCell:
    blocks: list[object] = field(default_factory=list)  # list[Paragraph]
    row_span: int = 1
    col_span: int = 1
    is_merge_continuation: bool = False  # ячейка, поглощённая другой (merge)
    bbox: Optional[BBox] = None

    @property
    def text(self) -> str:
        return "\n".join(
            b.text for b in self.blocks if isinstance(b, Paragraph)
        )


@dataclass
class TableRow:
    cells: list[TableCell] = field(default_factory=list)


@dataclass
class Table:
    rows: list[TableRow] = field(default_factory=list)
    col_widths_pt: list[float] = field(default_factory=list)
    bbox: Optional[BBox] = None
    source: str = "text"  # "text" (pdfplumber) | "ocr" (opencv grid)

    @property
    def n_rows(self) -> int:
        return len(self.rows)

    @property
    def n_cols(self) -> int:
        return max((len(r.cells) for r in self.rows), default=0)


@dataclass
class ImageBlock:
    data: bytes
    width_px: int
    height_px: int
    caption: Optional[str] = None
    bbox: Optional[BBox] = None
    fmt: str = "png"


@dataclass
class HeaderFooterText:
    text: str
    is_header: bool = True
    has_page_number_field: bool = False


Block = object  # Union[Paragraph, Heading, ListItem, Table, ImageBlock]


@dataclass
class PageWarning:
    page_number: int
    message: str
    is_fatal: bool = False


@dataclass
class Page:
    number: int
    blocks: list[Block] = field(default_factory=list)
    width_pt: float = 595.0
    height_pt: float = 842.0
    page_break_after: bool = True
    used_ocr: bool = False
    had_handwriting_fallback: bool = False


@dataclass
class Document:
    pages: list[Page] = field(default_factory=list)
    header: Optional[HeaderFooterText] = None
    footer: Optional[HeaderFooterText] = None
    warnings: list[PageWarning] = field(default_factory=list)
    source_filename: str = ""

    def add_warning(self, page_number: int, message: str, is_fatal: bool = False) -> None:
        self.warnings.append(PageWarning(page_number, message, is_fatal))
