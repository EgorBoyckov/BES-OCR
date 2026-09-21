# Архитектура BES OCR

## 1. Стек

Python 3.11 · PySide6 (GUI) · PyMuPDF + pdfplumber (PDF) · Tesseract 5 / pytesseract (OCR)
· OpenCV + NumPy + Pillow (обработка изображений) · img2table (детекция таблиц на сканах) · python-docx (генерация DOCX)

## 2. Структура каталогов

```
bes_ocr/
  __main__.py            точка входа (python -m bes_ocr)
  app.py                  запуск QApplication + MainWindow
  logging_setup.py        настройка логирования (файл + in-memory буфер для GUI)
  config/
    settings.py           Settings: языки OCR, DPI, число воркеров, пути
  core/
    models.py             модель документа (Document/Page/Block/...)
    errors.py             исключения, PageError
    pdf_source.py         открытие PDF, рендер страниц, извлечение текстового слоя/изображений
    text_layer.py         оценка качества текстового слоя страницы
    ocr_engine.py          препроцессинг + Tesseract (слова, строки, bbox, confidence)
    layout_analysis.py    сборка слов/строк в параграфы/заголовки/списки
    table_detection.py    поиск таблиц (pdfplumber для текстового слоя, img2table для сканов) + объединённые ячейки
    page_processor.py     обработка одной страницы (оркестрация всех модулей выше)
    pipeline.py           обработка документа целиком: страницы, колонтитулы, сборка Document
    docx_writer.py        генерация .docx из модели Document
  jobs/
    worker.py             фоновый воркер (QThread) с прогрессом/отменой, работает поверх pipeline
  gui/
    main_window.py        главное окно
    widgets.py             область drag&drop, панель прогресса, журнал
tests/
  conftest.py             генерация тестовых PDF-фикстур (reportlab/PyMuPDF)
  test_*.py               unit- и end-to-end тесты
docs/                     документация
packaging/                PyInstaller spec, скелет .deb
```

## 3. Модель документа (`core/models.py`)

Внутреннее представление не зависит ни от PDF, ни от DOCX:

* `Document` — список `Page`, метаданные.
* `Page` — список `Block` в порядке чтения + `page_break_after`.
* `Block` (union): `Heading`, `Paragraph` (список `Run` с bold/italic/size/цвет),
  `ListItem` (уровень, маркер/номер), `Table` (список `TableRow` → `TableCell`
  с `row_span`/`col_span`, список `Block` внутри ячейки), `ImageBlock`
  (байты изображения + размер + подпись), `HeaderFooter` (текст, номер страницы).
  Каждый `Block` несёт `BoxStyle`: bbox, выравнивание, отступы, номер страницы
  источника — это и есть "восстановленная структура" из п.4 ТЗ.

Это разделение — ключевое архитектурное решение: `page_processor.py` строит
модель независимо от способа получения текста (текстовый слой или OCR),
а `docx_writer.py` не знает вообще ничего о PDF/OCR — только о модели.
Это позволяет протестировать генерацию DOCX без PDF и наоборот.

## 4. Конвейер обработки (per-page decision)

```
PDF (PyMuPDF)
  → для каждой страницы:
      1. text_layer.assess(page)         → есть ли пригодный текстовый слой
      2. если слой хороший:  извлечь текст+шрифты (PyMuPDF), таблицы (pdfplumber)
         если слоя нет/плохой: растрировать страницу (DPI по config),
             ocr_engine.recognize() → слова+bbox+confidence,
             table_detection через img2table (см. docs/RESEARCH.md, п.5.1) на растре
      3. layout_analysis: слова/строки → параграфы/заголовки/списки/выравнивание
      4. обнаружение изображений (PyMuPDF: встроенные растры; для сканов —
         страница уже является изображением, поэтому шаг 4 неактуален)
      5. рукописный текст: если Tesseract даёt низкий confidence на блоке и
         эвристика (высокая доля "мусорных" слов) — блок сохраняется как
         изображение вместо текста (п.3 ТЗ)
      6. сборка Page (модель)
  → pipeline: сведение повторяющихся верхних/нижних строк на страницах в
      HeaderFooter, нумерация страниц, разрывы страниц
  → docx_writer: Document → .docx
  → верификация результата (docx открывается, счётчики блоков/таблиц)
```

Обработка страницы обёрнута в try/except на уровне `page_processor`: ошибка
одной страницы превращает её в placeholder-блок с сообщением об ошибке и не
прерывает документ (п.13 ТЗ).

## 5. Производительность и большие документы

* Страницы обрабатываются **потоково**: рендер/OCR одной страницы, немедленная
  сборка блоков, освобождение растра — весь документ разом в памяти не
  держится.
* `ThreadPoolExecutor` (по умолчанию `min(4, cpu_count)`) распараллеливает
  OCR/анализ страниц; результаты собираются по номеру страницы, чтобы порядок
  в DOCX не зависел от порядка завершения потоков.
* Фоновая обработка выполняется в `QThread` (`jobs/worker.py`), GUI получает
  прогресс через сигналы Qt (`page_done(i, total)`, `stage_changed(str)`) —
  интерфейс не блокируется.
* Поддержка отмены: `CancellationToken`, проверяется между страницами и внутри
  цикла OCR по страницам.

## 6. Обработка ошибок

`core/errors.py`: `PdfOpenError`, `PageProcessingError`, `DocxWriteError`,
`DiskSpaceError`. Все ошибки страниц собираются в `Document.warnings` и
отображаются в журнале GUI, не прерывая весь прогон (кроме фатальных:
файл не открывается, диск заполнен).

## 7. Логирование

`logging_setup.py` настраивает стандартный `logging` на два handler'а: файл
(`~/.local/share/bes-ocr/logs/`) и `QtLogHandler`, кладущий записи в
потокобезопасную очередь, которую GUI отображает в панели "Журнал". В лог не
пишется содержимое документа пользователя — только метаданные (номер
страницы, тип блока, коды ошибок), см. `docs/LIMITATIONS.md` и п.17 ТЗ.

## 8. Тестирование

Фикстуры (`tests/conftest.py`) генерируются программно (reportlab + PyMuPDF),
без бинарных PDF в репозитории — воспроизводимо и без внешних файлов.
Уровни тестов: unit (по каждому core-модулю), end-to-end (PDF → DOCX →
проверка структуры результата через python-docx), smoke GUI (offscreen Qt
platform). Подробности в `docs/TESTING.md`.

## 9. Упаковка

PyInstaller `--onedir` + `.deb`, `Depends: tesseract-ocr, tesseract-ocr-rus,
tesseract-ocr-eng`. Подробности — `docs/BUILD.md`.
