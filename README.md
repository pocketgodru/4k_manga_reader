
# MangaReader 4K

Веб-приложение для локальной библиотеки манги с тремя основными возможностями:
- поиск и скачивание манги с MangaLib,
- чтение глав в браузере,
- апскейл страниц (4K) через `realesrgan-ncnn-vulkan` или CPU fallback.

Проект работает на `FastAPI` и хранит данные локально в папке `data/`.

## Что делает проект

- Показывает библиотеку манги из локального хранилища.
- Даёт UI для поиска манги на MangaLib и выборочной загрузки глав.
- Умеет “умно” докачивать недостающие страницы без повторной загрузки уже скачанного.
- Даёт режим чтения главы с переключением качества: оригинал / upscaled.
- Запускает апскейл по одной главе или пакетно по всей манге, с прогрессом, ETA и отменой.

## Стек технологий

### Backend
- `Python 3`
- `FastAPI` — HTTP API и сервер страниц
- `Uvicorn` — ASGI сервер
- `Pydantic` — модели запросов/ответов и валидация
- `aiohttp` — асинхронные HTTP запросы к MangaLib/CDN
- `mangagraph` — поиск и часть данных по манге

### Обработка изображений
- `realesrgan-ncnn-vulkan` (через `subprocess`) — GPU апскейл
- `OpenCV (opencv-python-headless)` + `Pillow` + `numpy` — CPU fallback апскейл и постобработка

### Frontend
- Базовый HTML, CSS, JS

## Архитектура и структура проекта

```text
4k_manga_reader/
├─ app/
│  ├─ main.py                      # Точка входа FastAPI и все основные endpoint'ы
│  ├─ reader.py                    # Доступ к локальной библиотеке, главы, страницы, статус апскейла
│  ├─ enhancer.py                  # UpscalerEngine (ncnn-vulkan + CPU fallback)
│  ├─ downloader/
│  │  ├─ manager.py                # Логика задач скачивания и докачки
│  │  ├─ models.py                 # Pydantic-модели downloader
│  │  ├─ routes.py                 # Router для download API
│  │  └─ services/mangalib.py      # Клиент MangaLib API/CDN с retry/rate-limit handling
│  └─ templates/
│     ├─ index.html                # Главная библиотека
│     ├─ download.html             # Поиск/скачивание
│     ├─ manga.html                # Страница манги + массовый апскейл
│     └─ reader.html               # Ридер главы
├─ config.yaml                     # Конфигурация путей, сервера и upscaler
├─ requirements.txt                # Python зависимости
├─ tools/                          # Бинарник/модели Real-ESRGAN (локально)
└─ data/
   ├─ manga/                       # Оригинальные страницы глав + metadata.json
   └─ upscaled/                    # Апскейленные страницы
```

## Как это работает (по шагам)

### 1) Библиотека и чтение
- `MangaReader` (`app/reader.py`) читает локальные папки манги из `data/manga`.
- Главы сортируются по формату `v{том}c{глава}` с поддержкой дробных номеров (`8.1`, `8.3`).
- Для ридера используется endpoint `/image/{slug}/{chapter}/{page_idx}` с отдачей реального файла.

### 2) Поиск и скачивание
- Поиск в UI (`/download`) вызывает `/download/search`.
- `MangaDownloader` (`manager.py`) создаёт задачу и скачивает главы напрямую в `data/manga/{slug}`.
- “Умная” докачка:
  - сначала смотрит, что уже есть на диске,
  - затем запрашивает недостающие страницы,
  - сохраняет/обновляет `metadata.json`.
- Статус скачивания доступен через `/download/status/{task_id}`, отмена — `/download/cancel/{task_id}`.

### 3) Апскейл
- `UpscalerEngine` (`enhancer.py`) выбирает метод:
  - `ncnn-vulkan` (если exe + модели доступны),
  - иначе CPU pipeline (bicubic/lanczos + unsharp + contrast/sharpness).
- Для пакетной обработки используется `/upscale/all/{slug}` и polling `/upscale/status/{task_id}`.
- Результат пишется в  папку `data/upscaled/{slug}/{chapter}`.
- Для одиночной главы: `/upscale/{slug}/{chapter}`.

## Конфигурация

Основные параметры в `config.yaml`:
- `data_path`, `manga_folder`, `upscaled_folder` — где хранятся данные.
- `host`, `port` — запуск API.
- `upscaler.method` — `ncnn-vulkan` или `cpu`.
- `upscaler.ncnn.*` — путь к exe, модели, scale/tile/gpu/threads/output_format.
- `upscaler.cpu.*` — параметры CPU fallback.

Важно: `ncnn-vulkan` режим ожидает, что в `tools/` уже лежат бинарник и модели.

## Установка и запуск

### 1. Подготовка окружения
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Проверка конфигурации
- Убедитесь, что `config.yaml` корректен для вашей машины.
- Если нет GPU/бинарника, поставьте:
  - `upscaler.method: "cpu"`.

### 3. Запуск
```bash
python -m app.main
```

или:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Откройте в браузере:
- `http://localhost:8000/` — библиотека,
- `http://localhost:8000/download` — поиск и скачивание.

## Основные endpoint'ы

### Страницы
- `GET /` — библиотека
- `GET /download` — страница скачивания
- `GET /manga/{slug}` — страница манги
- `GET /manga/{slug}/{chapter}?quality=manga|upscaled` — ридер
- `GET /image/{slug}/{chapter}/{page_idx}?quality=...` — выдача изображения

### Download API
- `GET /download/search?q=...&limit=...` — поиск
- `GET /download/chapters/{manga_slug}` — список глав из источника
- `POST /download/start/{manga_slug}` — старт скачивания
- `GET /download/status/{task_id}` — статус задачи
- `POST /download/cancel/{task_id}` — отмена
- `GET /download/list` — список скачанных манг

### Upscale API
- `GET /upscale/method` — активный метод/настройки
- `GET /upscale/status-by-chapters/{slug}` — статус по главам
- `POST /upscale/all/{slug}` — массовый апскейл
- `GET /upscale/status/{task_id}` — прогресс массовой задачи
- `GET /upscale/active/{slug}` — текущая активная задача
- `POST /upscale/cancel/{task_id}` — отмена апскейла
- `POST /upscale/{slug}/{chapter}` — апскейл одной главы

## Формат данных на диске

```text
data/
├─ manga/
│  └─ <slug>/
│     ├─ metadata.json
│     ├─ v1c1/
│     │  ├─ 0001.jpg
│     │  └─ ...
│     └─ v1c1.5/
│        └─ ...
└─ upscaled/
   └─ <slug>/
      └─ v1c1/
         ├─ 0001.jpg
         └─ ...
```

`metadata.json` хранит:
- общую инфу о тайтле (title, cover, genres, status, rating и т.д.),
- `chapters` со статусом по каждой главе (`pages_expected`, `pages_downloaded`, `completed`),
- статистику (`total_chapters`, `completed_chapters`).

## Что и для чего используется (короткая карта)

- `app/main.py` — связывает всё вместе: reader, downloader, upscaler, шаблоны и API.
- `app/reader.py` — слой доступа к файловой структуре библиотеки.
- `app/downloader/manager.py` — бизнес-логика загрузки/докачки и управление задачами.
- `app/downloader/services/mangalib.py` — устойчивый HTTP-клиент источника с retry/backoff.
- `app/enhancer.py` — движок апскейла и выбор GPU/CPU пути.
- `app/templates/*.html` — интерфейс пользователя и polling прогресса.

## Известные особенности

- Проект ориентирован на Windows (есть настройка `WindowsProactorEventLoopPolicy` в `main.py`).
- Polling задач хранится в памяти процесса; при нескольких воркерах/рестарте активные задачи могут “потеряться”.
- Для стабильного `ncnn-vulkan` нужен корректный GPU-драйвер и валидные файлы в `tools/`.
- В `download.html` есть повторное объявление `startPolling` (в JS берётся последняя версия функции).

## Идеи для развития

- Вынести long-running задачи в очередь (`RQ`/`Celery`) и внешний storage статусов (`Redis`).
- Добавить тесты на парсинг глав и валидацию `metadata.json`.
- Вынести часть endpoint'ов в отдельные routers (сейчас `main.py` уже довольно большой).
- Добавить docker-сборку и healthcheck endpoint.
