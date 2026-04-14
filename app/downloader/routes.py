"""API routes для downloader"""
from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import Optional, List
from pydantic import BaseModel, Field
from datetime import datetime
from typing import Optional, List, Union

from .manager import MangaDownloader
from .models import DownloadStatus
from .models import StartDownloadRequest

router = APIRouter(prefix="/download", tags=["Downloader"])

# 🔹 Глобальный экземпляр (инициализируется в main.py)
downloader: Optional[MangaDownloader] = None


class SearchResponse(BaseModel):
    results: List[dict]


class TaskStatusResponse(BaseModel):
    task_id: str
    status: str  # "running", "completed", "error", "cancelled"
    progress: float = 0.0  # 0-100%
    
    # 🔹 🔹 🔹 ИСПРАВЛЕНО: принимаем int И float для номеров глав 🔹 🔹 🔹
    current_chapter: Optional[Union[int, float, str]] = None  # ← Было: Optional[int]
    
    current_page: Optional[int] = None
    total_chapters: Optional[int] = None
    total_pages: Optional[int] = None
    
    # 🔹 🔹 🔹 ИСПРАВЛЕНО: список может содержать дробные номера 🔹 🔹 🔹
    downloaded_chapters: List[Union[int, float, str]] = Field(default_factory=list)  # ← Было: List[int]
    
    errors: List[str] = Field(default_factory=list)
    
    # 🔹 Дополнительная информация
    manga_slug: Optional[str] = None
    manga_title: Optional[str] = None
    
    class Config:
        # 🔹 Разрешаем произвольные типы для совместимости
        arbitrary_types_allowed = True


@router.get("/search", response_model=SearchResponse)
async def search_manga(q: str, limit: int = 10):
    """Поиск манги"""
    if not downloader:
        raise HTTPException(status_code=500, detail="Downloader not initialized")
    
    results = await downloader.search_manga(q, limit)
    return {"results": [r.model_dump() for r in results]}


@router.post("/download/start/{manga_slug}")
async def start_download(
    manga_slug: str, 
    request: StartDownloadRequest,
    background_tasks: BackgroundTasks
):
    chapter_list = None

    
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