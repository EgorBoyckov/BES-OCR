# Тестирование

## Запуск

```bash
pip install -r requirements-dev.txt
QT_QPA_PLATFORM=offscreen pytest -q
```

`QT_QPA_PLATFORM=offscreen` нужен только для GUI-тестов (`test_gui_smoke.py`)
на машинах/CI без дисплея.

## Структура тестов

Фикстуры (`tests/conftest.py`) генерируются программно через `reportlab` и
`PyMuPDF` — в репозитории нет бинарных PDF, тесты воспроизводимы на любой
машине. Покрыты все 15 сценариев из ТЗ:

| Сценарий | Тест |
|---|---|
| Текстовый слой | `test_pipeline.py::test_text_pdf_end_to_end` |
| Скан | `test_pipeline.py::test_scanned_russian_pdf_end_to_end` |
| Русский язык | `test_pipeline.py::test_scanned_russian_pdf_end_to_end` |
| Английский язык | `test_pipeline.py::test_english_pdf_end_to_end` |
| Ru+En | `test_pipeline.py::test_mixed_language_pdf` |
| Простая таблица | `test_table_detection.py::test_pdfplumber_simple_table` |
| Объединённые ячейки | `test_table_detection.py::test_pdfplumber_merged_header`, `test_opencv_table_with_merged_cells` |
| Разные типы данных в таблице | `test_pipeline.py::test_table_with_merged_cells` (числа/даты/текст) |
| Таблица на нескольких страницах | `test_table_detection.py::test_pdfplumber_multipage_table` |
| Изображения | `test_pipeline.py::test_images_pdf` |
| Сложная смешанная страница | `test_pipeline.py::test_complex_mixed_page` |
| Многостраничный документ | `test_pipeline.py::test_multipage_document` |
| 100+ страниц | `test_pipeline.py::test_large_document_100_plus_pages` |
| Повреждённый PDF | `test_pipeline.py::test_corrupted_pdf_raises` |
| Без текстового слоя | `test_pipeline.py::test_no_text_layer_pdf_uses_ocr` |

Дополнительно:

* `test_text_layer.py` — оценка качества текстового слоя.
* `test_layout_analysis.py` — заголовки/списки/абзацы из "сырых" слов.
* `test_docx_writer.py` — генерация DOCX из модели напрямую (без PDF):
  реальные объединённые ячейки таблицы, вставка изображений.
* `test_error_isolation.py` — ошибка одной страницы не прерывает документ.
* `test_font_metrics.py` — кегль по ширине и высоте слова с учётом глифов.
* `test_layout_fidelity.py` — единый кегль строки, полужирный по толщине
  штриха, принудительные переносы у шапки справа, выравнивание по ширине,
  список с выступом, две колонки становятся таблицей-раскладкой (и
  ложные срабатывания не возникают), переносы строк в ячейках.
* `test_docx_fidelity.py` — точный межстрочный интервал и отступ по
  базовым линиям, позиция абзаца после таблицы, плавающие изображения,
  таблица без рамок, нейтральные стили заголовков, разрыв страницы.
* `test_color_marks.py` — вырезание синей печати (прозрачный фон, чёрный
  текст под ней сохраняется), цветной текст не принимается за печать.
* `test_form_scan.py` — сквозной тест на программно сгенерированном скане
  бланка (`official_form_scan_pdf`): 12pt у всего текста, шапка справа
  с переносом, полужирный заголовок по центру, список, две колонки,
  печать как плавающее изображение.
* `test_gui_smoke.py` — создание главного окна, добавление файлов, полный
  прогон конвертации через фоновый `ConversionWorker` в offscreen-режиме Qt.

## Проверка качества конвертации

Модель проверки заложена прямо в тесты: после конвертации PDF → DOCX тесты
открывают полученный `.docx` через `python-docx` и проверяют:

* количество и содержимое таблиц (`len(doc.tables)`, `table.cell(r, c).text`);
* факт объединения ячеек (`cell(0,0).text == cell(0,1).text` после merge);
* наличие текста нужных языков;
* наличие вставленных изображений (`doc.inline_shapes`);
* стили абзацев (заголовки, списки).

Это и есть механизм регрессионного контроля качества (п.16 ТЗ) в рамках
автоматических тестов.

## Объективное визуальное сравнение (`bes_ocr/devtools/validate.py`)

Дев-инструмент (не часть поставляемого приложения — LibreOffice не является
его рантайм-зависимостью) сравнивает исходный PDF с результатом объективно,
а не на глаз: `DOCX → PDF` через LibreOffice headless → рендер страниц в
растр (PyMuPDF) → постраничный SSIM + проверка совпадения числа страниц.
Вдохновлено разделом «visual validation» документа scan2docx-architecture
(см. `docs/ARCHITECTURE.md`, раздел 10 «Идеи на будущее из scan2docx-architecture»),
но без scikit-image
(SSIM реализован вручную через `cv2.boxFilter`, чтобы не добавлять
зависимость — см. docstring модуля) и без сопоставления отдельных блоков
(`block_iou`/`text_cer`) — тех метрик, что реально ловили регрессии в этом
проекте (раздутие на лишние страницы, съехавшая геометрия), достаточно уже
от SSIM + числа страниц.

```python
from bes_ocr.devtools.validate import compare
report = compare("original.pdf", "result.docx")
print(report.page_count_match, report.mean_ssim, report.visual_score)
```

Тесты — `tests/test_validate.py`; тест полного цикла с реальным вызовом
LibreOffice пропускается (`skipif`), если `soffice` не установлен в
окружении (не требуется для сборки/использования самого приложения).
