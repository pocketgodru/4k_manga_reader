# app/main.py
from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import asyncio
import yaml
import os
from pathlib import Path
from datetime import datetime
from PIL import Image
import io
import tempfile
from tqdm import tqdm
import logging as logging
import atexit
from typing import Dict



# 🔹 В САМОЕ НАЧАЛО main.py, ДО импорта asyncio/uvicorn:

import sys
import asyncio

# 🔹 Для Windows: устанавливаем ProactorEventLoop для поддержки subprocess
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())



from app.downloader.manager import MangaDownloader
from app.downloader.routes import router as downloader_router
from app.downloader.models import StartDownloadRequest
from app.downloader.routes import downloader
from app.reader import MangaReader

from app.enhancer import UpscalerEngine, enhance_for_display
# Загрузка конфига
with open("config.yaml", 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("UpscaleTracker")

app = FastAPI(title="MangaReader 4K")
upscale_tasks = {}

if os.getenv("UVICORN_WORKERS", "1") != "1":
    logger.warning("⚠️ ВНИМАНИЕ: Запущено несколько воркеров! Status polling не будет работать без Redis.")


templates = Jinja2Templates(directory="app/templates")
reader = MangaReader(
    config['data_path'], 
    config['manga_folder'], 
    config['upscaled_folder']
)


# Инициализация downloader
downloader = MangaDownloader(
    data_path=reader.base_path,  # ✅ Используем путь из reader
)


upscaler = UpscalerEngine(config)

# 🔹 Глобальное хранилище задач апскейла
upscale_tasks: Dict[str, dict] = {}


# Подключаем роуты downloader
app.include_router(downloader_router)

# Обновляем глобальную переменную в routes
from app.downloader import routes
routes.downloader = downloader


# Глобальное хранилище статуса апскейла
upscale_tasks = {}

logger.info(f"📚 Reader manga_path: {reader.manga_path}")
logger.info(f"📥 Downloader manga_path: {downloader.manga_path}")

@app.get("/")
async def home(request: Request):
    # 🔹 Локальные манги
    manga_list = reader.get_manga_list()
    
    # 🔹 Скачанные манги
    downloaded_list = []
    for manga_dir in downloader.data_path.iterdir():
        if manga_dir.is_dir():
            meta_path = manga_dir / "metadata.json"
            if meta_path.exists():
                import json
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
                downloaded_list.append({
                    "slug": manga_dir.name,
                    "title": meta.get("title", manga_dir.name),
                    "cover": meta.get("cover"),
                    "source": "downloaded",
                    "downloaded": True,
                    "genres": meta.get("genres", []),
                })
    
    # 🔹 Объединяем (скачанные в начало или конец — по желанию)
    all_manga = downloaded_list + manga_list
    
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "manga_list": all_manga
    })

@app.get("/download")
async def download_page(request: Request):
    return templates.TemplateResponse("download.html", {"request": request})

@app.get("/download/search")
async def search_downloads(q: str, limit: int = 10):
    results = await downloader.search_manga(q, limit)
    return {"results": [r.model_dump() for r in results]}

@app.get("/download/chapters/{manga_slug}")
async def get_chapters_list(manga_slug: str):
    from app.downloader.services.mangalib import MangaLibService
    service = MangaLibService()
    try:
        chapters = await service.get_chapters(manga_slug)
        return {"chapters": [{"number": c.number, "name": c.name} for c in chapters]}
    except Exception as e:
        return {"chapters": [], "error": str(e)}
    finally:
        await service.close()

@app.post("/download/start/{manga_slug}")
async def start_download(
    manga_slug: str, 
    request: StartDownloadRequest,
    background_tasks: BackgroundTasks
):
    chapter_list = None
    logger.info(request)
    logger.info(request.chapters)
    
    if request.chapters:

            chapter_list = []
            # 🔹 request.chapters — это строка "4.5,8.1,8.3"
            for c in request.chapters.split(","):
                c = c.strip()
                if not c:
                    continue
                num = float(c)
                # 🔹 Целые числа храним как int, дробные — как float
                chapter_list.append(int(num) if num == int(num) else num)

    
    task_id = f"dl_{manga_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    background_tasks.add_task(
        downloader.download_manga_smart,
        manga_slug,
        request.url,
        manga_slug,
        chapter_list,  # 🔹 Теперь может содержать float!
        task_id
    )
    
    return {"status": "ok", "task_id": task_id}

@app.post("/download/cancel/{task_id}")
async def cancel_download(task_id: str):
    if downloader.cancel_task(task_id):
        return {"status": "ok"}
    raise HTTPException(status_code=404, detail="Задача не найдена")

@app.get("/download/status/{task_id}")
async def get_download_status(task_id: str):
    task = downloader.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return {
        "task_id": task.task_id,
        "status": task.status.value,
        "progress": task.progress,
        "current_chapter": task.current_chapter,
        "current_page": task.current_page,
        "total_chapters": task.total_chapters,
        "downloaded_chapters": task.downloaded_chapters,
        "errors": task.errors,
        "manga_slug": task.manga_slug,
        "manga_title": task.manga_title,
    }

@app.get("/download/list")
async def list_downloads():
    downloads = downloader.get_downloaded_manga()
    return {"downloads": [d.model_dump() for d in downloads]}


@app.get("/manga/{slug}")
async def manga_info(request: Request, slug: str):
    metadata = reader.get_metadata(slug)
    chapters_info = reader.get_chapters_with_info(slug)
    upscale_status = reader.get_upscale_status(slug)
    
    # 🔹 Проверяем, есть ли metadata.json (скачанная манга)
    manga_dir = downloader.manga_path / slug
    is_downloaded = (manga_dir / "metadata.json").exists()
    
    if not metadata and not is_downloaded:
        raise HTTPException(status_code=404, detail="Манга не найдена")
    
    # 🔹 Если нет локальных метаданных, но есть metadata.json — используем его
    if not metadata and is_downloaded:
        import json
        meta_path = manga_dir / "metadata.json"
        metadata = json.loads(meta_path.read_text(encoding='utf-8'))
    
    
    return templates.TemplateResponse("manga.html", {
        "request": request,
        "metadata": metadata or {"title": slug},
        "chapters": chapters_info,
        "upscale_status": upscale_status,
        "slug": slug,
        "is_downloaded": is_downloaded,
    })

@app.get("/manga/{slug}/{chapter}")
async def read_chapter(request: Request, slug: str, chapter: str, quality: str = "manga"):
    pages = reader.get_pages(slug, chapter, quality)
    if not pages:
        raise HTTPException(status_code=404, detail="Глава не найдена")
    metadata = reader.get_metadata(slug)
    is_upscaled = reader.is_chapter_upscaled(slug, chapter)
    
    return templates.TemplateResponse("reader.html", {
        "request": request,
        "slug": slug,
        "chapter": chapter,
        "quality": quality,
        "total_pages": len(pages),
        "metadata": metadata,
        "is_upscaled": is_upscaled
    })

@app.get("/image/{slug}/{chapter}/{page_idx}")
async def serve_image(
    slug: str, 
    chapter: str, 
    page_idx: int, 
    quality: str = "manga"
):
    """🔹 Раздача изображений — с обработкой str/Path"""
    from pathlib import Path
    
    # 🔹 Получаем путь (может быть str или Path)
    page_path = reader.get_page_path(slug, chapter, page_idx, quality)
    
    # 🔹 Приводим к Path для надёжности
    page_path = Path(page_path) if page_path else None
    
    # 🔹 Проверка существования
    if not page_path or not page_path.exists():
        logger.warning(f"⚠️ Изображение не найдено: {page_path}")
        raise HTTPException(status_code=404, detail="Изображение не найдено")
    
    # 🔹 Отдаём файл
    from fastapi.responses import FileResponse
    
    # 🔹 Определяем MIME-тип
    suffix = page_path.suffix.lower()
    media_type = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.webp': 'image/webp',
        '.gif': 'image/gif'
    }.get(suffix, 'image/jpeg')
    
    return FileResponse(
        str(page_path),
        media_type=media_type,
        headers={'Cache-Control': 'public, max-age=31536000'}
    )

@app.get("/api/manga/list")
async def api_manga_list():
    """API для списка манги — ВСЯ манга локальная"""
    manga_list = []
    
    # 🔹 Сканируем data/manga/
    manga_path = Path(config['data_path']) / config['manga_folder']
    if manga_path.exists():
        for manga_dir in manga_path.iterdir():
            if not manga_dir.is_dir() or manga_dir.name.startswith('.'):
                continue
            
            meta_file = manga_dir / "metadata.json"
            
            # 🔹 Загружаем метаданные
            if meta_file.exists():
                import json
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
            else:
                meta = reader.get_metadata(manga_dir.name) or {}
            
            # 🔹 Статус глав
            status = reader.get_upscale_status(manga_dir.name)
            total_chapters = len(status)
            completed_chapters = sum(1 for s in status.values() if s.get('download_completed') or s.get('pages_downloaded', 0) >= s.get('pages_expected', 0))
            
            manga_list.append({
                "slug": manga_dir.name,
                "title": meta.get("title", manga_dir.name),
                "cover": meta.get("cover"),
                "genres": meta.get("genres", []),
                "status": meta.get("status"),
                "rating": meta.get("rating"),
                "upscaled": any(s.get('upscaled') for s in status.values()),
                "total_chapters": total_chapters,
                "completed_chapters": completed_chapters,
                "completed": completed_chapters >= total_chapters and total_chapters > 0,
                "in_progress": completed_chapters > 0 and completed_chapters < total_chapters,
                "progress": (completed_chapters / max(total_chapters, 1)) * 100 if total_chapters > 0 else 0,
            })
    
    return {"manga_list": manga_list}

@atexit.register
def cleanup_resources():
    """Очистка при завершении"""
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(downloader.cleanup())
        else:
            loop.run_until_complete(downloader.cleanup())
    except:
        pass

def _update_progress(task_id: str, done: int, total: int, chapter: str, result: dict):
    """🔹 Коллбэк для обновления прогресса"""
    if task_id in upscale_tasks:
        upscale_tasks[task_id].update({
            "current_chapter": chapter,
            "processed": done,
            "total": total,
            "last_result": result
        })

@app.get("/upscale/method")
async def get_upscale_method():
    """🔹 Возвращает текущий метод апскейла и его доступность"""
    return {
        "method": upscaler.method,
        "ncnn_available": upscaler.ncnn_available,
        "scale": upscaler.scale,
        "gpu_id": upscaler.gpu_id,
        "output_format": upscaler.output_format
    }

@app.get("/upscale/status-by-chapters/{slug}")
async def get_upscale_status_by_chapters(slug: str, chapters: str = None):
    """
    🔹 Возвращает детальный статус по каждой главе
    🔹 ?chapters=v1c1,v2c8.1,v2c8.2 — опционально фильтр
    """
    chapter_list = None
    if chapters:
        chapter_list = [c.strip() for c in chapters.split(",") if c.strip()]
    
    status = await upscaler.get_upscale_status(slug, chapter_list)
    return status

# Endpoint для polling прогресса
@app.get("/upscale/status/{task_id}")
async def get_upscale_task_status(task_id: str):
    """🔹 Статус задачи + ETA"""
    
    if task_id not in upscale_tasks:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    task = upscale_tasks[task_id]
    
    total = task.get("total_chapters", task.get("total", 1))
    completed = task.get("completed_chapters", task.get("processed", 0))
    
    # 🔹 Расчёт времени
    started_at = task.get("started_at")  # ISO-строка
    elapsed_sec = 0
    eta_sec = None
    chapters_per_min = None
    
    if started_at:
        from datetime import datetime
        start_dt = datetime.fromisoformat(started_at)
        elapsed_sec = (datetime.now() - start_dt).total_seconds()
        
        # 🔹 Скорость: глав в минуту
        if completed > 0 and elapsed_sec > 0:
            chapters_per_min = round((completed / elapsed_sec) * 60, 2)
            remaining = max(0, total - completed)
            eta_sec = int((remaining / max(chapters_per_min, 0.01)) * 60)
    
    return {
        "task_id": task_id,
        "status": task.get("status", "pending"),
        "completed_chapters": completed,
        "total_chapters": total,
        "current_chapter": task.get("current_chapter", ""),
        "progress": round((completed / max(total, 1)) * 100, 1),
        # 🔹 Новые поля для ETA:
        "elapsed_sec": round(elapsed_sec, 1),
        "chapters_per_min": chapters_per_min,
        "eta_sec": eta_sec,  # ← Это используем на фронтенде
        "method": task.get("method", upscaler.method)
    }

@app.get("/upscale/active/{slug}")
async def get_active_upscale_task(slug: str):
    """Возвращает последнюю активную задачу для манги"""
    # Ищем задачу по slug (можно хранить в отдельном словаре для скорости)
    for task_id, task in upscale_tasks.items():
        if task.get("slug") == slug and task.get("status") == "running":
            return {
                "task_id": task_id,
                "status": task["status"],
                "processed": task["processed"],
                "total": task["total"],
                "current_chapter": task.get("current_chapter", "")
            }
    return {"active": False}

# Эндпоинт для отмены задачи
@app.post("/upscale/cancel/{task_id}")
async def cancel_upscale_task(task_id: str):
    """🔹 Отмена задачи — устанавливаем флаг в upscaler"""
    
    if task_id not in upscale_tasks:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    # 🔹 1. Обновляем статус в глобальном хранилище
    upscale_tasks[task_id]["status"] = "cancelling"  # 🔹 Промежуточный статус
    upscale_tasks[task_id]["cancel_requested"] = True
    
    # 🔹 2. 🔥 УСТАНАВЛИВАЕМ ФЛАГ В UPSCALER 🔥
    upscaler.set_cancel_flag(task_id, True)
    
    logger.info(f"⏹️ Запрошена отмена задачи: {task_id}")
    
    return {"status": "ok", "message": "Отмена запрошена"}

@app.post("/upscale/all/{slug}")
async def trigger_upscale_all(slug: str, background_tasks: BackgroundTasks):
    """🔹 Массовый апскейл — с корректным начальным прогрессом"""
    
    # 🔹 1. Сначала проверяем, что уже готово
    initial_status = await upscaler.get_upscale_status(slug)
    
    ready_count = initial_status["ready_count"]
    total = initial_status["total"]
    to_process = initial_status["pending_count"]
    
    # 🔹 Если всё уже готово — сразу возвращаем успех
    if to_process == 0:
        return {
            "status": "ok",
            "message": "Все главы уже апскейлены",
            "ready": ready_count,
            "total": total,
            "skipped": total
        }
    
    task_id = f"up_{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # 🔹 2. Инициализируем задачу с УЧЁТОМ уже готовых глав
    upscale_tasks[task_id] = {
        "status": "running",
        "completed_chapters": ready_count,  # 🔹 Начинаем с уже готовых!
        "total_chapters": total,
        "skipped_chapters": ready_count,    # 🔹 Для статистики
        "current_chapter": "",
        "slug": slug,
        "started_at": datetime.now().isoformat(),
        "cancel_requested": False,
        "method": upscaler.method
    }
    
    # 🔹 3. Получаем список глав для обработки (только pending)
    chapters_to_process = [ch["chapter"] for ch in initial_status["pending"]]
    
    async def run_batch():
        async def on_progress(completed: int, total: int, chapter: str, status: str, skipped: bool = False):
            if task_id in upscale_tasks:
                upscale_tasks[task_id].update({
                    "completed_chapters": completed,  # 🔹 Уже включает готовые + обработанные
                    "total_chapters": total,
                    "current_chapter": chapter,
                    "last_status": status
                })
        
        result = await upscaler.upscale_manga(
            slug=slug,
            task_id=task_id,
            chapters=chapters_to_process,  # 🔹 Только те, что нужно!
            progress_callback=on_progress
        )
        if task_id in upscale_tasks:
            upscale_tasks[task_id].update({
                "status": result.get("status", "completed"),
                "completed_chapters": result["processed"],
                "total_chapters": result["total"]
            })
    
    background_tasks.add_task(lambda: asyncio.run(run_batch()))
    
    return {
        "status": "ok",
        "task_id": task_id,
        "total_chapters": total,
        "already_ready": ready_count,      # 🔹 Для фронтенда
        "to_process": to_process,          # 🔹 Сколько реально обрабатывать
        "initial_progress": initial_status["progress_percent"],  # 🔹 Начальный %
        "message": f"Найдено {ready_count} готовых глав. Обработка {to_process} глав..."
    }

@app.post("/upscale/{slug}/{chapter}")
async def trigger_upscale(slug: str, chapter: str, scale: int = 2):
    """🔹 Апскейл одной главы"""
    
    pages = reader.get_pages(slug, chapter, quality="manga")
    if not pages:
        raise HTTPException(status_code=404, detail="Глава не найдена")
    
    # 🔹 Запускаем апскейл
    result = await upscaler.upscale_chapter(slug, chapter)
    
    # 🔹 Обновляем метаданные
    try:
        upscaled_meta = reader.create_upscaled_metadata(slug)
        upscaled_meta['upscale_info'] = {
            'generated_at': datetime.now().isoformat(),
            'method': upscaler.method,
            'scale': scale,
            **result
        }
        reader.save_metadata(slug, upscaled_meta, source="upscaled")
    except Exception as e:
        logger.error(f"❌ Ошибка meta: {e}")
    
    return {
        "status": "ok", 
        **result,
        "method": upscaler.method
    }



@app.get("/status/{slug}")
async def get_upscale_status(slug: str):
    status = reader.get_upscale_status(slug)
    metadata = reader.get_metadata(slug, source="upscaled")
    return {
        "slug": slug,
        "chapters": status,
        "total_chapters": len(status),
        "upscaled_chapters": sum(1 for c in status.values() if c['upscaled']),
        "metadata_exists": metadata is not None
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=config['host'], port=config['port'])