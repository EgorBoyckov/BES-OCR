"""Исключения конвейера обработки."""


class BesOcrError(Exception):
    """Базовое исключение приложения."""


class PdfOpenError(BesOcrError):
    """PDF повреждён или не может быть открыт."""


class PageProcessingError(BesOcrError):
    """Ошибка обработки одной страницы (не фатальна для всего документа)."""

    def __init__(self, page_number: int, message: str):
        self.page_number = page_number
        super().__init__(f"Страница {page_number}: {message}")


class DocxWriteError(BesOcrError):
    """Не удалось сформировать итоговый .docx."""


class DiskSpaceError(BesOcrError):
    """Недостаточно места на диске для временных файлов или результата."""


class CancelledError(BesOcrError):
    """Обработка отменена пользователем."""
