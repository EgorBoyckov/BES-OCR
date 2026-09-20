"""Настройка логирования: файл + потокобезопасная очередь для GUI.

В лог не пишется содержимое пользовательских документов — только
метаданные (номер страницы, тип операции, коды ошибок), см. docs/LIMITATIONS.md.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import queue
from datetime import datetime

LOGGER_NAME = "bes_ocr"


def build_logger(log_dir: str) -> tuple[logging.Logger, "queue.Queue[str]"]:
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    try:
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, f"bes-ocr-{datetime.now():%Y%m%d}.log")
        file_handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)
    except OSError:
        # Нет доступа к диску для логов — продолжаем работу без файлового лога.
        pass

    log_queue: "queue.Queue[str]" = queue.Queue()

    class QueueHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                log_queue.put_nowait(self.format(record))
            except Exception:
                pass

    qh = QueueHandler()
    qh.setFormatter(fmt)
    logger.addHandler(qh)

    return logger, log_queue


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)
