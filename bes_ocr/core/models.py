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
    # Межбуквенный интервал (pt, отрицательный — уплотнённый): подгоняет
    # ширину текста под ширину строк оригинала, чтобы текстовый процессор
    # переносил строки там же, где они перенесены в оригинале.
    char_spacing_pt: float = 0.0


@dataclass
class Paragraph:
    """Абзац. Перевод строки внутри `Run.text` ("\n") — принудительный
    разрыв строки оригинала (строка закончилась раньше, чем её вынудила бы
    перенести ширина текста — типично для шапок/заголовков по центру или
    справа); обычные (естественные) переносы не сохраняются — их заново
    делает текстовый процессор.

    Геометрия абзаца хранится в АБСОЛЮТНЫХ координатах исходной страницы
    (точки PDF): `indent_pt` — левая граница строк (кроме первой),
    `first_line_indent_pt` — сдвиг первой строки относительно неё
    (отрицательный — выступ, как у пунктов списка), `right_edge_pt` —
    правая граница, до которой текст переносится (None — до правого поля).
    Пересчёт в отступы конкретного контейнера (страница или ячейка
    таблицы) — задача генератора документа.
    """

    runs: list[Run] = field(default_factory=list)
    alignment: Alignment = Alignment.LEFT
    indent_pt: float = 0.0
    space_before_pt: float = 0.0
    bbox: Optional[BBox] = None
    first_line_indent_pt: float = 0.0
    right_edge_pt: Optional[float] = None
    # Базовая линия первой строки (абсолютная y) и измеренный шаг между
    # базовыми линиями соседних строк абзаца — позволяют воспроизвести
    # вертикальное положение текста точно, а не приблизительно.
    baseline_pt: Optional[float] = None
    line_pitch_pt: Optional[float] = None
    n_lines: int = 1

    @property
    def text(self) -> str:
        return "".join(r.text for r in self.runs)


@dataclass
class Heading(Paragraph):
    level: int = 1  # 1..4


@dataclass
class ListItem(Paragraph):
    """Пункт списка. Маркер ("1.", "а)", "-") хранится отдельно от текста
    пункта и воспроизводится буквально — как в оригинале, а не
    автонумерацией текстового процессора (та продолжает нумерацию через
    весь документ и заменяет "-" на "•")."""

    level: int = 0
    ordered: bool = False
    marker: str = ""
    # Абсолютная x начала текста пункта (после маркера).
    text_x_pt: Optional[float] = None


@dataclass
class TableCell:
    blocks: list[object] = field(default_factory=list)  # list[Paragraph]
    row_span: int = 1
    col_span: int = 1
    is_merge_continuation: bool = False  # ячейка, поглощённая другой (merge)
    bbox: Optional[BBox] = None
    v_align: str = "top"  # "top" | "center" | "bottom"

    @property
    def text(self) -> str:
        return "\n".join(
            b.text for b in self.blocks if isinstance(b, Paragraph)
        )


@dataclass
class TableRow:
    cells: list[TableCell] = field(default_factory=list)
    height_pt: Optional[float] = None  # высота строки в оригинале


@dataclass
class Table:
    rows: list[TableRow] = field(default_factory=list)
    col_widths_pt: list[float] = field(default_factory=list)
    bbox: Optional[BBox] = None
    source: str = "text"  # "text" (pdfplumber) | "ocr" (img2table) | "layout"
    # Таблица-раскладка без рамок: так передаются многоколоночные блоки
    # текста (например, реквизиты сторон договора в две колонки), которые
    # в оригинале не являются таблицей, но должны остаться рядом друг с
    # другом, а не слиться построчно в одну колонку.
    borderless: bool = False
    # Внутренний отступ текста от границ ячеек (измерен по оригиналу).
    cell_padding_pt: Optional[float] = None

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
    # Плавающее изображение: ставится точно в координаты bbox на странице
    # (за текстом) и не участвует в потоке текста — печати, подписи,
    # отметки поверх напечатанного текста.
    floating: bool = False


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
