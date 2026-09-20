# Сборка для Astra Linux 1.7

## 1. Автономная сборка (PyInstaller)

```bash
source .venv/bin/activate
pip install -r requirements.txt pyinstaller
pyinstaller --name bes-ocr --onedir --windowed \
    --add-data "bes_ocr:bes_ocr" \
    bes_ocr/__main__.py
```

Результат — каталог `dist/bes-ocr/` со всеми Python-зависимостями и
интерпретатором. Tesseract в сборку не включается — он вызывается как
внешний бинарь и ставится системным пакетом (см. ниже), это соответствует
стандартной практике Debian/Astra и не требует статической линковки движка
и его моделей в приложение.

Проверка автономной сборки:

```bash
dist/bes-ocr/bes-ocr
```

## 2. Пакет .deb

Скелет пакета — в `packaging/deb/`. Общая схема:

```
packaging/deb/
  DEBIAN/control          метаданные, зависимости (tesseract-ocr, tesseract-ocr-rus, tesseract-ocr-eng)
  opt/bes-ocr/             сюда копируется dist/bes-ocr/* после сборки PyInstaller
  usr/share/applications/  bes-ocr.desktop — ярлык в меню приложений
```

Сборка:

```bash
cp -r dist/bes-ocr/* packaging/deb/opt/bes-ocr/
dpkg-deb --build --root-owner-group packaging/deb bes-ocr_0.1.0_amd64.deb
```

Установка:

```bash
sudo apt install ./bes-ocr_0.1.0_amd64.deb
```

`apt`/`dpkg` сам подтянет зависимости `tesseract-ocr`, `tesseract-ocr-rus`,
`tesseract-ocr-eng` из штатных репозиториев Astra Linux/Debian.

## 3. Проверка на целевой платформе

Сборка и тесты в этом репозитории выполнялись на Ubuntu 24.04 (см.
`docs/RESEARCH.md`) как на ближайшем доступном приближении к Astra Linux SE
1.7. Перед боевым развёртыванием необходимо на реальном стенде Astra Linux
1.7 проверить как минимум:

1. `dist/bes-ocr/bes-ocr` (или установленный `.deb`) запускается без ошибок
   загрузчика/линковщика (`ldd`, отсутствующие `.so`).
2. Отрисовка GUI в графическом окружении Fly/MATE корректна.
3. `tesseract-ocr`, `tesseract-ocr-rus`, `tesseract-ocr-eng` ставятся из
   штатных репозиториев Astra Linux Directory без конфликтов версий.
4. Полный цикл: выбор PDF → конвертация → открытие DOCX в LibreOffice
   Writer (штатный офисный пакет Astra Linux).
