# app/main.py
from fastapi import FastAPI, Request, HTTPException, Response, BackgroundTasks
from fastapi.templating import Jinja2Templates
import asyncio
import yaml
import os
from pathlib import Path
from datetime import datetime
from app.reader import MangaReader
from app.enhancer import enhance_for_display, cpu_upscale
from PIL import Image
import io
import tempfile
from tqdm import tqdm
import logging as logging
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

# Глобальное хранилище статуса апскейла
upscale_tasks = {}

@app.get("/")
async def home(request: Request):
    manga_list = reader.get_manga_list()
    return templates.TemplateResponse("index.html", {
        "request": request, 
        "manga_list": manga_list
    })

@app.get("/manga/{slug}")
async def manga_info(request: Request, slug: str):
    metadata = reader.get_metadata(slug)
    chapters_info = reader.get_chapters_with_info(slug)
    upscale_status = reader.get_upscale_status(slug)
    
    if not metadata:
        raise HTTPException(status_code=404, detail="Манга не найдена")
    
    return templates.TemplateResponse("manga.html", {
        "request": request, 
        "metadata": metadata, 
        "chapters": chapters_info,
        "upscale_status": upscale_status,
        "slug": slug
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
    quality: str = "manga",
    upscale: bool = False
):
    page_path = reader.get_page_path(slug, chapter, page_idx, quality)
    if not page_path or not os.path.exists(page_path):
        raise HTTPException(status_code=404, detail="Изображение не найдено")
    
    if upscale:
        upscaled_path = reader.get_page_path(slug, chapter, page_idx, quality="upscaled")
        if upscaled_path and os.path.exists(upscaled_path):
            with open(upscaled_path, 'rb') as f:
                return Response(content=f.read(), media_type="image/png")
        
        buf = io.BytesIO()
        img = Image.open(page_path)
        img.save(buf, format='PNG')
        buf.seek(0)
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_in:
            tmp_in.write(buf.getvalue())
            tmp_in_path = tmp_in.name
        
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp_out:
            tmp_out_path = tmp_out.name
        
        try:
            cpu_upscale(tmp_in_path, tmp_out_path, scale=2)
            with open(tmp_out_path, 'rb') as f:
                response_data = f.read()
            return Response(content=response_data, media_type="image/png")
        finally:
            os.unlink(tmp_in_path)
            os.unlink(tmp_out_path)
    else:
        img = Image.open(page_path)
        img = enhance_for_display(img, config.get('enhancements', {}))
        
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=90, optimize=True)
        buf.seek(0)
        
        return Response(content=buf.getvalue(), media_type="image/jpeg")


# Функция апскейла (выносится в отдельный метод)
def _run_upscale_sync(slug: str, scale: int, task_id: str, chapters: list, chapter_pages: dict, config: dict, reader):
    from pathlib import Path
    from tqdm import tqdm
    from app.enhancer import cpu_upscale
    import re
    
    # ...   подсчёт pages_to_process  ...
    pages_to_process = []
    for chapter in chapters:
        upscaled_dir = Path(config['data_path']) / config['upscaled_folder'] / slug / chapter
        upscaled_dir.mkdir(parents=True, exist_ok=True)
        for page_path in chapter_pages[chapter]:
            original_name = Path(page_path).name
            clean_name = re.sub(r'(_4k|_upscaled|_x2|_2x)$', '', original_name, flags=re.IGNORECASE)
            output_path = upscaled_dir / clean_name
            if not output_path.exists():
                pages_to_process.append((chapter, page_path, output_path))
    
    total_to_process = len(pages_to_process)
    processed_total = 0
    
    with tqdm(total=total_to_process, desc=f"🚀 Апскейл {slug}", unit="стр", colour="green") as pbar:
        for chapter, page_path, output_path in pages_to_process:
            # 🔹 НОВОЕ: Проверка флага отмены перед каждой страницей
            if upscale_tasks.get(task_id, {}).get("cancel_requested", False):
                logger.info(f"⏹️ Апскейл остановлен пользователем на {chapter}")
                upscale_tasks[task_id]["status"] = "cancelled"
                break  # 🔹 Выход из цикла
            
            upscale_tasks[task_id]["current_chapter"] = chapter
            
            try:
                cpu_upscale(page_path, output_path, scale=scale)
                processed_total += 1
                pbar.update(1)
                upscale_tasks[task_id]["processed"] = processed_total
                upscale_tasks[task_id]["total"] = total_to_process
            except Exception as e:
                print(f"❌ Ошибка апскейла {page_path}: {e}")
                pbar.update(1)
    
    # 🔹 НОВОЕ: Если не отменено - помечаем как завершено
    if upscale_tasks[task_id]["status"] != "cancelled":
        upscale_tasks[task_id]["status"] = "completed"
    # Генерация metadata...
    try:
        upscaled_meta = reader.create_upscaled_metadata(slug)
        upscaled_meta['upscale_info']['generated_at'] = datetime.now().isoformat()
        reader.save_metadata(slug, upscaled_meta, source="upscaled")
    except Exception as e:
        print(f"Ошибка сохранения meta: {e}")
    
    upscale_tasks[task_id]["status"] = "completed"

# Endpoint для polling прогресса
@app.get("/upscale/status/{task_id}")
async def get_upscale_task_status(task_id: str):
    # 🔹 ЛОГ: Фиксируем запрос статуса
    if task_id not in upscale_tasks:
        logger.error(f"❌ Задача не найдена: {task_id}. Доступные: {list(upscale_tasks.keys())}")
        raise HTTPException(status_code=404, detail="Задача не найдена (возможно, сервер перезагружен)")
    
    task = upscale_tasks[task_id]
    return {
        "task_id": task_id,
        "status": task.get("status", "pending"),
        "processed": task.get("processed", 0),
        "total": task.get("total", 1),
        "current_chapter": task.get("current_chapter", ""),
        "progress": round((task.get("processed", 0) / max(task.get("total", 1), 1)) * 100, 2)
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
    if task_id not in upscale_tasks:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    upscale_tasks[task_id]["status"] = "cancelled"
    upscale_tasks[task_id]["cancel_requested"] = True  # Флаг остановки
    
    logger.info(f"⏹️ Запрошена отмена задачи: {task_id}")
    return {"status": "ok", "message": "Отмена запрошена"}

@app.post("/upscale/all/{slug}")
async def trigger_upscale_all(slug: str, background_tasks: BackgroundTasks, scale: int = 2):
    chapters = reader.get_chapters(slug, source="manga")
    if not chapters:
        raise HTTPException(status_code=404, detail="Манга не найдена")
    
    # 🔹 НОВОЕ: Собираем страницы для проверки (но не считаем total здесь)
    chapter_pages = {}
    for chapter in chapters:
        chapter_pages[chapter] = reader.get_pages(slug, chapter, quality="manga")
    
    task_id = f"{slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # 🔹 ЛОГ: Фиксируем создание задачи
    logger.info(f"🚀 Создаю задачу: {task_id}")

    # 🔹 НОВОЕ: Инициализируем с placeholder, реальные значения установит воркер
    upscale_tasks[task_id] = {
        "status": "running",
        "processed": 0,
        "total": -1,  # 🔹 Будет обновлено воркером после подсчёта
        "current_chapter": "",
        "slug": slug
    }
    
    background_tasks.add_task(
        asyncio.to_thread,
        _run_upscale_sync,
        slug, scale, task_id, chapters, chapter_pages, config, reader
    )
    
    already_done = 0
    for chapter in chapters:
        upscaled_dir = Path(config['data_path']) / config['upscaled_folder'] / slug / chapter
        already_done += sum(1 for p in chapter_pages[chapter] 
                        if (upscaled_dir / Path(p).name).exists())

    return {
        "status": "ok", 
        "task_id": task_id, 
        "already_upscaled": already_done,  # 🔹 Для отображения на фронтенде
        "message": f"Найдено {already_done} уже готовых страниц"
    }

@app.post("/upscale/{slug}/{chapter}")
async def trigger_upscale(slug: str, chapter: str, scale: int = 2):
    """Апскейл одной главы с tqdm"""
    pages = reader.get_pages(slug, chapter, quality="manga")
    if not pages:
        raise HTTPException(status_code=404, detail="Глава не найдена")
    
    upscaled_dir = Path(config['data_path']) / config['upscaled_folder'] / slug / chapter
    upscaled_dir.mkdir(parents=True, exist_ok=True)
    
    processed = 0
    skipped = 0
    
    # ✅ tqdm для одной главы
    with tqdm(total=len(pages), desc=f"📖 Апскейл {chapter}", unit="стр", colour="cyan") as pbar:
        for page_path in pages:
            page_name = Path(page_path).name
            output_path = upscaled_dir / page_name
            
            if output_path.exists():
                skipped += 1
                pbar.update(1)
                continue
            
            try:
                cpu_upscale(page_path, output_path, scale=scale)
                processed += 1
                pbar.update(1)
            except Exception as e:
                print(f"❌ Ошибка апскейла {page_path}: {e}")
                pbar.update(1)
    
    try:
        upscaled_meta = reader.create_upscaled_metadata(slug)
        upscaled_meta['upscale_info']['generated_at'] = datetime.now().isoformat()
        reader.save_metadata(slug, upscaled_meta, source="upscaled")
    except Exception as e:
        pass
    
    return {"status": "ok", "processed": processed, "skipped": skipped, "total": len(pages)}

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