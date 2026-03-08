"""API routes для downloader"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Optional, List
from pydantic import BaseModel
from datetime import datetime

from .manager import MangaDownloader
from .models import DownloadStatus

router = APIRouter(prefix="/download", tags=["Downloader"])

# 🔹 Глобальный экземпляр (инициализируется в main.py)
downloader: Optional[MangaDownloader] = None


class SearchResponse(BaseModel):
    results: List[dict]


class StartDownloadRequest(BaseModel):
    url: str
    chapters: Optional[str] = None  # "1,2,5" или null


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str
    progress: float
    current_chapter: Optional[int]
    current_page: Optional[int]
    total_chapters: int
    downloaded_chapters: List[int]
    errors: List[str]


@router.get("/search", response_model=SearchResponse)
async def search_manga(q: str, limit: int = 10):
    """Поиск манги"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    results = await downloader.search_manga(q, limit)
    return {"results": [r.model_dump() for r in results]}


@router.post("/start/{manga_slug}")
async def start_download(
    manga_slug: str,
    request: StartDownloadRequest,
    background_tasks: BackgroundTasks
):
    """Запускает скачивание в фоне"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    chapter_list = None
    if request.chapters:
        try:
            chapter_list = [int(c.strip()) for c in request.chapters.split(",")]
        except:
            raise HTTPException(status_code=400, detail="Неверный формат глав")
    
    task_id = f"dl_{manga_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # 🔹 Запуск в фоне
    background_tasks.add_task(
        downloader.download_manga_smart,
        manga_slug,
        request.url,
        manga_slug,  # title (можно улучшить)
        chapter_list,
        task_id
    )
    
    return {"status": "ok", "task_id": task_id}


@router.post("/cancel/{task_id}")
async def cancel_download(task_id: str):
    """Отменяет скачивание"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    if downloader.cancel_task(task_id):
        return {"status": "ok", "message": "Отмена запрошена"}
    
    raise HTTPException(status_code=404, detail="Задача не найдена")


@router.get("/status/{task_id}", response_model=TaskStatusResponse)
async def get_task_status(task_id: str):
    """Получает статус задачи"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    task = downloader.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Задача не найдена")
    
    return TaskStatusResponse(
        task_id=task.task_id,
        status=task.status.value,
        progress=task.progress,
        current_chapter=task.current_chapter,
        current_page=task.current_page,
        total_chapters=task.total_chapters,
        downloaded_chapters=task.downloaded_chapters,
        errors=task.errors
    )


@router.get("/list")
async def list_downloads():
    """Список скачанных манг"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    downloads = downloader.get_downloaded_manga()
    return {"downloads": [d.model_dump() for d in downloads]}