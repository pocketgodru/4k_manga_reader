"""Pydantic модели для downloader"""
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Union
from datetime import datetime
from enum import Enum
from pydantic import field_validator  

class DownloadStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ERROR = "error"


class MangaSearchResult(BaseModel):
    """Результат поиска манги"""
    name: str
    rus_name: Optional[str] = None
    slug_url: str
    rating: float = 0.0
    release_year: Optional[int] = None
    manga_type: str = Field(..., alias="type")
    status: str
    cover: Optional[str] = None  # 🔹 Только URL как строка
    url: str
    description: Optional[str] = None
    
    class Config:
        populate_by_name = True
    
    # 🔹 НОВОЕ: Валидатор для преобразования Cover → str
    @field_validator('cover', mode='before')
    @classmethod
    def parse_cover(cls, v):
        if v is None:
            return None
        if isinstance(v, str):
            return v
        # 🔹 Если это объект Cover из mangagraph — извлекаем URL
        if hasattr(v, 'md') and v.md:
            return v.md
        if hasattr(v, 'default') and v.default:
            return v.default
        if hasattr(v, 'thumbnail') and v.thumbnail:
            return v.thumbnail
        return None

class ChapterInfo(BaseModel):
    number: Union[int, float]  # 🔹 Может быть 8 или 8.3!
    name: Optional[str] = None
    url: str
    pages_count: int = 0
    volume: Optional[int] = 1  # 🔹 НОВОЕ: том главы
    downloaded: bool = False
    
    class Config:
        arbitrary_types_allowed = True
    
class StartDownloadRequest(BaseModel):
    url: str
    # 🔹 КЛЮЧЕВОЕ: chapters — СТРОКА, а не список!
    chapters: Optional[str] = None  # ← Было: Optional[List[int]] = None ❌
    
    @field_validator('chapters')
    @classmethod
    def validate_chapters(cls, v):
        if v is None or not v.strip():
            return None
        # 🔹 Проверяем, что все элементы — числа (целые или дробные)
        for c in v.split(","):
            c = c.strip()
            if c:
                try:
                    float(c)  # ✅ Принимает 1, 4.5, 8.1, 8.3
                except ValueError:
                    raise ValueError(f"Неверный номер главы: '{c}'")
        return v.strip()
    
    
class DownloadTask(BaseModel):
    """Задача на скачивание"""
    task_id: str
    manga_slug: str
    manga_title: str
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    current_chapter: Optional[int] = None
    current_page: Optional[int] = None
    total_chapters: int = 0
    downloaded_chapters: List[int] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    cancel_requested: bool = False
    
    class Config:
        arbitrary_types_allowed = True


class DownloadMetadata(BaseModel):
    """Метаданные скачанной манги — главы в формате v{vol}c{num}"""
    title: str
    source: str
    source_url: str  # 🔹 Обязательно
    
    # 🔹 Даты с авто-заполнением
    downloaded_at: datetime = Field(default_factory=datetime.now)
    updated_at: Optional[datetime] = None
    
    # 🔹 Главы: dict с ключами в исходном формате!
    chapters: Dict[str, dict] = Field(default_factory=dict)
    
    # 🔹 Статистика
    total_chapters: int = 0
    completed_chapters: int = 0
    
    # 🔹 Доп. поля (все optional)
    original_title: Optional[str] = None
    cover_url: Optional[str] = None
    description: Optional[str] = None
    genres: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    status: Optional[str] = None
    authors: List[str] = Field(default_factory=list)
    artists: List[str] = Field(default_factory=list)
    rating: Optional[float] = None
    
    class Config:
        extra = "allow"  # 🔹 Разрешаем поля из API