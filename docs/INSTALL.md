# Установка и запуск

## Системные зависимости (Astra Linux SE 1.7 / Debian / Ubuntu)

```bash
sudo apt update
sudo apt install python3 python3-venv tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng \
    libegl1 libgl1 libxkbcommon0 libxcb-cursor0 libopengl0
```

`tesseract-ocr-rus`/`tesseract-ocr-eng` — языковые пакеты, необходимые для
распознавания русского и английского текста. `libegl1`/`libgl1`/… — системные
библиотеки, нужные Qt (PySide6) для отрисовки интерфейса; на Astra Linux с
уже установленным графическим окружением Fly/MATE они, как правило, уже
присутствуют.

Если системный Python в Astra Linux 1.7 старше 3.9 (см. `docs/RESEARCH.md`),
используйте собственную сборку Python 3.11 в виртуальном окружении или
готовую PyInstaller-сборку (`docs/BUILD.md`) — не полагайтесь на системный
`python3` для запуска из исходников.

## Установка приложения из исходников

```bash
git clone <repo> bes-ocr
cd bes-ocr
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Запуск

```bash
source .venv/bin/activate
python -m bes_ocr
```

Откроется главное окно BES OCR. Дальнейший сценарий: перетащите PDF (или
нажмите «Выбрать PDF…»), при необходимости смените папку сохранения, нажмите
«Конвертировать в Word».

## Запуск из готовой сборки (после `docs/BUILD.md`)

```bash
/opt/bes-ocr/bes-ocr
```
или через ярлык в меню приложений.

## Логи

Логи пишутся в `~/.local/share/bes-ocr/logs/`. Содержимое пользовательских
документов в логи не попадает (см. `docs/LIMITATIONS.md`).
