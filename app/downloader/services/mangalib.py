"""Улучшенный сервис для mangalib.me с retry, rate limiting и CDN fallback"""
import asyncio
import logging
import re
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse, urlunparse

import aiohttp

from ..models import MangaSearchResult, ChapterInfo


API_BASE = "https://api.cdnlibs.org/api/".strip()
IMG_BASE = "https://img3.mixlib.me".strip()
WARMUP_URL = "https://api.cdnlibs.org/".strip()

# 🔹 CDN хосты для ротации при 429
CDN_HOSTS = ["img3.mixlib.me"]

# 🔹 Таймауты и повторные попытки
DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=280, connect=100)
MAX_RETRIES = 5
RETRY_DELAY = 5.0          # Экспоненциальная задержка между ретраями
RATE_LIMIT_DELAY = 10.0    # Задержка при 429
BETWEEN_REQUESTS_DELAY = 0.5  # Пауза между запросами страниц

# 🔹 Заголовки
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://mangalib.me/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru,en;q=0.9",
    "site-id": "1",
    "X-Requested-With": "XMLHttpRequest",
}

logger = logging.getLogger(__name__)


def _normalize_url(url: str) -> str:
    """Убирает пробелы, двойные слеши и нормализует путь"""
    url = url.strip()  # 🔹 НОВОЕ
    parsed = urlparse(url)
    path = parsed.path.replace("//", "/")
    return urlunparse((parsed.scheme, parsed.netloc, path, parsed.params, parsed.query, parsed.fragment))


def _rotate_cdn(url: str, current_host: str) -> List[str]:
    """Возвращает альтернативные URL с другими CDN хостами"""
    parsed = urlparse(url)
    return [
        urlunparse((parsed.scheme, host, parsed.path, parsed.params, parsed.query, parsed.fragment))
        for host in CDN_HOSTS if host != current_host
    ]


def _is_valid_image(data: bytes) -> bool:
    """Проверка валидности изображения по magic bytes"""
    if len(data) < 12:
        return False
    if data.startswith(b"\xff\xd8\xff"):      # JPEG
        return True
    if data.startswith(b"\x89PNG\r\n\x1a\n"): # PNG
        return True
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):  # GIF
        return True
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":  # WebP
        return True
    return False


def _slug_to_folder(slug: str) -> str:
    """715--black-clover → black-clover"""
    return slug.split("--", 1)[1] if "--" in slug else slug


class MangaLibService:
    """Надёжный клиент для Mangalib API с retry и CDN fallback"""
    
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        self._cookies_warmed = False
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Ленивая инициализация сессии с прогревом cookies"""
        if self._session is None or self._session.closed:
            jar = aiohttp.CookieJar(unsafe=True)
            self._session = aiohttp.ClientSession(headers=HEADERS, cookie_jar=jar, timeout=DOWNLOAD_TIMEOUT)
        
        if not self._cookies_warmed:
            try:
                await self._session.get(WARMUP_URL)
                self._cookies_warmed = True
            except Exception as e:
                logger.warning(f"Cookie warmup failed: {e}")
        
        return self._session
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    async def _request_with_retry(
        self,
        url: str,
        method: str = "GET",
        params: Optional[dict] = None,
        is_image: bool = False
    ) -> Optional[bytes]:
        """Универсальный запрос с retry, rate limit handling и CDN fallback"""
        #url = _normalize_url(url)
        #print(url)
        url = url.strip()
    
        # 🔹 Также чистим параметры (если есть)
        if params and isinstance(params, list):
            params = [(k.strip() if isinstance(k, str) else k, 
                    v.strip() if isinstance(v, str) else v) for k, v in params]
        urls_to_try = [url]

        for try_url in urls_to_try:
            last_error = None
            
            for attempt in range(MAX_RETRIES):
                try:
                    session = await self._get_session()
                    async with session.request(method, try_url, params=params) as resp:
                        
                        # 🔹 Обработка 429 — экспоненциальная задержка + смена CDN
                        if resp.status == 429:
                            retry_after = int(resp.headers.get('Retry-After', RATE_LIMIT_DELAY))
                            logger.warning(f"⚠️ HTTP 429: ждём {retry_after}с, пробуем другой CDN")
                            await asyncio.sleep(retry_after)
                            break  # Переход к следующему CDN
                        
                        if resp.status == 200:
                            data = await resp.read()
                            if is_image and not _is_valid_image(data):
                                logger.warning(f"⚠️ Не изображение: {try_url}")
                                break
                            return data
                        
                        # 🔹 Другие ошибки клиента — не ретраим
                        if 400 <= resp.status < 500 and resp.status != 429:
                            logger.warning(f"HTTP {resp.status}: {try_url}")
                            break
                        
                        # 🔹 Серверные ошибки — ретрай
                        logger.warning(f"HTTP {resp.status}, попытка {attempt+1}/{MAX_RETRIES}")
                        if attempt < MAX_RETRIES - 1:
                            delay = RETRY_DELAY * (2 ** attempt)  # 🔹 Экспоненциальный бэкофф
                            await asyncio.sleep(delay)
                            continue
                        break
                        
                except (asyncio.TimeoutError, aiohttp.ClientError, ConnectionError) as e:
                    last_error = e
                    logger.warning(f"⚠️ Попытка {attempt+1}/{MAX_RETRIES} не удалась: {e}")
                    if attempt < MAX_RETRIES - 1:
                        delay = RETRY_DELAY * (2 ** attempt)
                        await asyncio.sleep(delay)
                        continue
            
            logger.error(f"❌ Не удалось получить {url} после всех попыток")
        
        return None
    
    async def search(self, query: str, limit: int = 10) -> List[MangaSearchResult]:
        """🔍 Поиск манги через mangagraph"""
        try:
            from mangagraph import Mangagraph
            mg = Mangagraph()
            results = await mg.search_manga(query, limit=limit)
            
            return [
                MangaSearchResult(
                    name=r.name,
                    rus_name=r.rus_name,
                    slug_url=r.slug_url,
                    rating=r.rating.raw_average if r.rating else 0.0,
                    release_year=r.release_year,
                    type=r.type,
                    status=r.status,
                    cover=r.cover,
                    url=f"https://mangalib.me/ru/manga/{r.slug_url}",
                )
                for r in results
            ]
        except Exception as e:
            logger.error(f"❌ Поиск Mangalib: {e}")
            return []
    
    async def get_chapters(self, manga_slug: str) -> List[ChapterInfo]:
        """📚 Получение списка глав с retry"""
        url = f"{API_BASE}manga/{manga_slug}/chapters"
        #print(url)
        data_bytes = await self._request_with_retry(url)
        if not data_bytes:
            return []
        
        try:
            import json
            data = json.loads(data_bytes.decode('utf-8'))
        except Exception as e:
            logger.error(f"❌ Парсинг ответа глав: {e}")
            return []
        
        chapters = data.get("data") or []
        result = []
        
        for i, ch in enumerate(chapters):
            vol = ch.get("volume") or 1
            num = ch.get("number") or (i + 1)
            
            try:
                vol = int(vol)
                num = int(num)
            except (TypeError, ValueError):
                vol, num = 1, i + 1
            
            result.append(ChapterInfo(
                number=int(num) if num == int(num) else i + 1,
                name=ch.get("name") or f"Том {vol}, Глава {num}",
                url=f"mangagraph://{manga_slug}/v{vol}c{num}",
                pages_count=0
            ))
        
        return sorted(result, key=lambda x: x.number)
    
    async def get_page_urls(self, manga_slug: str, volume: int, chapter: str) -> List[str]:
        """🖼️ Получение ссылок на изображения с retry"""
        #print(chapter)
        ch = int(chapter) if chapter == str(chapter) else chapter
        url = f"{API_BASE}manga/{manga_slug}/chapter"
        params = {"number": int(ch), "volume": volume}
        #print(params)
        data_bytes = await self._request_with_retry(url, params=params)
        if not data_bytes:
            return []
        
        try:
            import json
            data = json.loads(data_bytes.decode('utf-8'))
        except:
            return []
        
        chapter_data = data.get("data") or {}
        pages = chapter_data.get("pages") or []
        chapter_slug = chapter_data.get("slug", f"{volume}-{ch}")
        manga_folder = _slug_to_folder(manga_slug)
        
        urls = []
        for p in pages:
            img_name = p.get("image") if isinstance(p, dict) else p
            if img_name:
                urls.append(f"{IMG_BASE}/manga/{manga_folder}/chapters/{chapter_slug}/{img_name}")
        
        return urls
    
    async def download_image(self, url: str, save_path: Path) -> bool:
        """📥 Скачивает изображение с retry, CDN fallback и валидацией"""
        
        # 🔹 Если файл уже есть и не пустой — пропускаем
        if save_path.exists() and save_path.stat().st_size > 0:
            return True
        
        data = await self._request_with_retry(url, is_image=True)
        if not data:    
            return False
        
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            save_path.write_bytes(data)
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения {save_path}: {e}")
            return False
        
    async def get_metadata(self, manga_slug: str) -> dict:
        """📋 Получение метаданных БЕЗ fields[] — максимальная совместимость"""
        
        manga_slug = manga_slug.strip()
        url = f"{API_BASE}manga/{manga_slug}?fields[]=background&fields[]=eng_name&fields[]=otherNames&fields[]=summary&fields[]=releaseDate&fields[]=type_id&fields[]=caution&fields[]=views&fields[]=close_view&fields[]=rate_avg&fields[]=rate&fields[]=genres&fields[]=tags&fields[]=teams&fields[]=user&fields[]=franchise&fields[]=authors&fields[]=publisher&fields[]=userRating&fields[]=moderated&fields[]=metadata&fields[]=metadata.count&fields[]=metadata.close_comments&fields[]=manga_status_id&fields[]=chap_count&fields[]=status_id&fields[]=artists&fields[]=format"
        
        # 🔹 Никаких params — получаем полный ответ
        data_bytes = await self._request_with_retry(url, params=None)
        
        if not data_bytes:
            logger.warning(f"⚠️ Не удалось получить метаданные для {manga_slug}")
            return {"title": manga_slug, "source": "mangalib"}
        
        try:
            import json
            payload = json.loads(data_bytes.decode('utf-8'))
            data = payload.get("data") or {}
        except Exception as e:
            logger.error(f"❌ Парсинг метаданных: {e}")
            return {"title": manga_slug, "source": "mangalib"}
        
        if not data:
            return {"title": manga_slug, "source": "mangalib"}
        #print(data.get("genres"))
        # 🔹 Безопасное извлечение обложки (учитываем Cover объект и dict)
        cover = data.get("cover")
        cover_url = None
        if isinstance(cover, dict):
            cover_url = cover.get("default") or cover.get("md") or cover.get("thumbnail")
        elif hasattr(cover, 'default'):  # Объект Cover
            cover_url = cover.default or cover.md or cover.thumbnail
        elif isinstance(cover, str):
            cover_url = cover.strip() or None
        
        # 🔹 Жанры: извлекаем имена из списка объектов
        genres = []
        genres_data = data.get("genres")
        if genres_data and isinstance(genres_data, list):
            genres = [g["name"] for g in genres_data if isinstance(g, dict) and g.get("name")]
        #print(genres)
        # 🔹 Статус: может быть объектом с полем 'label'
        status = data.get("status")
        status_label = None
        if isinstance(status, dict):
            status_label = status.get("label")
        elif isinstance(status, str):
            status_label = status

        
        return {
            "title": data.get("rus_name") or data.get("name") or manga_slug,
            "eng_name": data.get("eng_name"),
            "description": data.get("summary"),
            "cover": cover_url,
            "genres": genres,
            "status": status_label,
            "release_year": data.get("releaseDate"),
            "source": "mangalib",
            "source_url": f"https://mangalib.me/ru/manga/{manga_slug}",
            # 🔹 Дополнительные поля для будущего
            "rating": data.get("rate_avg") or data.get("rating", {}).get("average"),
            "views": data.get("views", {}).get("total") if isinstance(data.get("views"), dict) else None,
            "authors": [a.get("name") for a in (data.get("authors") or []) if isinstance(a, dict)],
            "artists": [a.get("name") for a in (data.get("artists") or []) if isinstance(a, dict)],
        }

    async def get_chapter_page_count(self, manga_slug: str, volume: int, chapter: str) -> int:
        """🔹 Возвращает ожидаемое количество страниц на сервере"""
        urls = await self.get_page_urls(manga_slug, volume, chapter)
        return len(urls)
    
    async def get_chapter_image_urls(self, manga_slug: str, volume: int, chapter: str) -> List[str]:
        """🔹 Возвращает список всех URL изображений главы (для сверки)"""
        return await self.get_page_urls(manga_slug, volume, chapter)

__all__ = [
    "MangaLibService",
    "BETWEEN_REQUESTS_DELAY",
    "MAX_RETRIES",
    "RETRY_DELAY",
    "RATE_LIMIT_DELAY",
    "DOWNLOAD_TIMEOUT",
]
    