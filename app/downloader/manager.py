"""Основной менеджер загрузок"""
import asyncio
import json
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict
from .models import DownloadTask, DownloadStatus, DownloadMetadata, ChapterInfo
from .services.mangalib import MangaLibService, BETWEEN_REQUESTS_DELAY 
import logging
import asyncio
import re



# 🔹 Логгер
logger = logging.getLogger(__name__)


BETWEEN_REQUESTS_DELAY = 0.5 
BETWEEN_PAGES_DELAY = BETWEEN_REQUESTS_DELAY # Пауза между страницами
BETWEEN_CHAPTERS_DELAY = 2.0  # Пауза между главами

class MangaDownloader:
    def __init__(self, data_path: str, manga_folder: str = "manga"):
        self.data_path = Path(data_path).resolve()
        
        # 🔹 ИСПРАВЛЕНО: преобразуем в Path для поддержки оператора /
        self.manga_folder = Path(manga_folder)
        self.manga_path = (self.data_path / self.manga_folder).resolve()
        
        self.manga_path.mkdir(parents=True, exist_ok=True)
        
        self.mangalib = MangaLibService()
        self.tasks: Dict[str, DownloadTask] = {}
        
        logger.info(f"📥 MangaDownloader: manga_path={self.manga_path}")
    
    async def search_manga(self, query: str, limit: int = 10):
        """Поиск манги"""
        return await self.mangalib.search(query, limit)

    async def download_manga_smart(
        self,
        manga_slug: str,
        manga_url: str,
        manga_title: str,
        chapters: Optional[List[int]] = None,
        task_id: Optional[str] = None
    ) -> DownloadTask:
        """Умная докачка прямо в manga/ с проверкой существующих файлов"""
        
        if not task_id:
            task_id = f"dl_{manga_slug}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        task = DownloadTask(
            task_id=task_id,
            manga_slug=manga_slug,
            manga_title=manga_title,
            status=DownloadStatus.RUNNING
        )
        self.tasks[task_id] = task
        
        try:
            # 🔹 1. Получаем список глав с сервера
            available_chapters = await self.mangalib.get_chapters(manga_slug)
            if not available_chapters:
                raise ValueError("Не удалось получить список глав")
            
            # 🔹 2. Фильтруем запрошенные главы
            if chapters:
                to_process = [c for c in available_chapters if c.number in chapters]
            else:
                to_process = available_chapters
            
            task.total_chapters = len(to_process)
            logger.info(f"📋 Запланировано: {len(to_process)} глав")
            
            # 🔹 3. Папка манги (прямо в manga/, не в downloads/)
            normalized_slug = manga_slug.replace("--", "_").replace("-", "_")
            manga_dir = self.data_path / self.manga_folder / normalized_slug
            manga_dir.mkdir(parents=True, exist_ok=True)
            #print(manga_dir)
            # 🔹 4. Обрабатываем каждую главу
            for idx, ch in enumerate(to_process, 1):
                if task.cancel_requested:
                    task.status = DownloadStatus.CANCELLED
                    break
                
                task.current_chapter = ch.number
                logger.info(f"📖 [{idx}/{len(to_process)}] Глава {ch.number}: проверка...")
                
                # 🔹 Умная докачка одной главы
                result = await self._download_chapter_smart(
                    manga_dir, ch, manga_slug, task
                )
                
                if result["status"] == "skipped":
                    logger.info(f"✅ Глава {ch.number}: уже скачана ({result['pages']}/{result['pages']})")
                elif result["status"] == "partial":
                    logger.info(f"🔄 Глава {ch.number}: докачано {result['new_pages']} новых страниц")
                elif result["status"] == "completed":
                    logger.info(f"✅ Глава {ch.number}: скачана ({result['pages']} страниц)")
                else:
                    task.errors.append(f"Глава {ch.number}: {result.get('error', 'unknown')}")
                    logger.warning(f"❌ Глава {ch.number}: ошибка — {result.get('error')}")
                
                task.downloaded_chapters.append(ch.number)
                task.progress = (idx / len(to_process)) * 100
                
                # 🔹 Пауза между главами
                await asyncio.sleep(BETWEEN_CHAPTERS_DELAY)
            
            # 🔹 5. Завершение
            if task.status != DownloadStatus.CANCELLED:
                task.status = DownloadStatus.COMPLETED
                await self._save_or_update_metadata(manga_dir, manga_title, manga_url, task)
            
            return task
            
        except Exception as e:
            task.status = DownloadStatus.ERROR
            task.errors.append(str(e))
            logger.error(f"❌ Ошибка задачи {task_id}: {e}", exc_info=True)
            raise

    async def _download_chapter_smart(
        self,
        manga_dir: Path,
        chapter: ChapterInfo,
        manga_slug: str,
        task: DownloadTask
    ) -> Dict[str, any]:
        """
        🔹 Умная докачка: сначала ищем на диске, потом лезем в API
        🔹 Сохраняем имена в исходном формате: v{vol}c{num}
        """
        
        try:
            # 🔹 1. Сначала ищем УЖЕ СУЩЕСТВУЮЩУЮ папку на диске
            # Парсим номер главы из задачи (ch.number = 384)
            target_chapter_num = int(chapter.number) if chapter.number == int(chapter.number) else chapter.number
            
            found_chapter_name = None
            found_volume = None
            found_chap_str = None
            
            # 🔹 Сканируем папку манги на наличие подходящей главы
            for item in manga_dir.iterdir():
                if item.is_dir() and not item.name.startswith('.'):
                    # 🔹 Парсим имя: v{vol}c{num}
                    match = re.match(r'^v(\d+)c(\d+(?:\.\d+)?)$', item.name)
                    if match:
                        vol = int(match.group(1))
                        chap_str = match.group(2)  # "130", "7.5", "3680"
                        chap_num = float(chap_str)
                        
                        # 🔹 Если номер главы совпадает — это наша папка!
                        if chap_num == target_chapter_num:
                            found_chapter_name = item.name
                            found_volume = vol
                            found_chap_str = chap_str
                            break
            
            # 🔹 2. Если нашли папку — проверяем, скачана ли она
            if found_chapter_name:
                chapter_dir = manga_dir / found_chapter_name
                
                # 🔹 Считаем страницы
                pages = (list(chapter_dir.glob("*.png")) + 
                        list(chapter_dir.glob("*.jpg")) + 
                        list(chapter_dir.glob("*.jpeg")) +
                        list(chapter_dir.glob("*.webp")))
                
                # 🔹 Если страниц достаточно — пропускаем (не лезем в API!)
                if len(pages) > 0:
                    # 🔹 Пытаемся получить expected из metadata (опционально)
                    expected = 10  # дефолт, если не знаем
                    meta_file = manga_dir / "metadata.json"
                    if meta_file.exists():
                        try:
                            import json
                            with open(meta_file, 'r', encoding='utf-8') as f:
                                meta = json.load(f)
                                ch_meta = meta.get("chapters", {}).get(found_chapter_name, {})
                                expected = ch_meta.get("pages_expected", len(pages))
                        except:
                            pass
                    
                    return {
                        "status": "skipped",
                        "pages": len(pages),
                        "expected": expected,
                        "chapter_name": found_chapter_name,
                        "volume": found_volume,
                        "chapter_number": found_chap_str
                    }
            
            # 🔹 3. Если не нашли на диске — лезем в API
            # Парсим URL: mangagraph://slug/v37c3680
            m = re.match(r"mangagraph://([^/]+)/v(\d+)c([\d.]+)", chapter.url)
            if not m:
                return {"status": "error", "error": "Неверный формат URL главы"}
            
            slug, vol, ch_num_str = m.group(1), int(m.group(2)), m.group(3)
            
            # 🔹 Имя главы в исходном формате
            chapter_name = f"v{vol}c{ch_num_str}"
            chapter_dir = manga_dir / chapter_name
            chapter_dir.mkdir(parents=True, exist_ok=True)
            
            # 🔹 Получаем URL страниц со сервера
            server_urls = await self.mangalib.get_chapter_image_urls(slug, vol, float(ch_num_str))
            if not server_urls:
                # 🔹 Если API не ответил, но папка уже есть — считаем ок
                if chapter_dir.exists() and any(chapter_dir.iterdir()):
                    pages = list(chapter_dir.glob("*.png")) + list(chapter_dir.glob("*.jpg"))
                    return {
                        "status": "skipped",
                        "pages": len(pages),
                        "expected": len(pages),
                        "chapter_name": chapter_name,
                        "volume": vol,
                        "chapter_number": ch_num_str
                    }
                return {"status": "error", "error": "Не удалось получить список страниц"}
            
            expected_count = len(server_urls)
            
            # 🔹 Считаем уже скачанные файлы
            existing_files = {
                f.name for f in chapter_dir.iterdir()
                if f.suffix.lower() in ['.png', '.jpg', '.jpeg', '.webp']
            }
            downloaded_count = len(existing_files)
            
            # 🔹 Если всё уже есть — пропускаем
            if downloaded_count >= expected_count and downloaded_count > 0:
                return {
                    "status": "skipped",
                    "pages": downloaded_count,
                    "expected": expected_count,
                    "chapter_name": chapter_name,
                    "volume": vol,
                    "chapter_number": ch_num_str
                }
            
            # 🔹 Определяем, какие страницы скачать
            pages_to_download = []
            for idx, url in enumerate(server_urls, 1):
                ext = "png"
                if ".jpg" in url.lower() or ".jpeg" in url.lower():
                    ext = "jpg"
                elif ".webp" in url.lower():
                    ext = "webp"
                
                filename = f"{idx:04d}.{ext}"
                file_path = chapter_dir / filename
                
                if filename not in existing_files or file_path.stat().st_size == 0:
                    pages_to_download.append((idx, url, filename))
            
            if not pages_to_download:
                return {
                    "status": "skipped",
                    "pages": downloaded_count,
                    "expected": expected_count,
                    "chapter_name": chapter_name,
                    "volume": vol,
                    "chapter_number": ch_num_str
                }
            
            logger.info(f"📥 Глава {chapter_name}: нужно скачать {len(pages_to_download)}/{expected_count}")
            
            # 🔹 Скачиваем недостающие
            success_count = 0
            for page_idx, url, filename in pages_to_download:
                if task.cancel_requested:
                    return {"status": "cancelled", "downloaded": success_count}
                
                file_path = chapter_dir / filename
                ok = await self.mangalib.download_image(url, file_path)
                
                if ok:
                    success_count += 1
                    task.current_page = page_idx
                    chapter_progress = (downloaded_count + success_count) / expected_count * 100
                    task.progress = min(100, chapter_progress)
                else:
                    logger.warning(f"⚠️ Не удалось скачать страницу {page_idx}")
                
                await asyncio.sleep(BETWEEN_PAGES_DELAY)
            
            final_downloaded = downloaded_count + success_count
            if final_downloaded >= expected_count:
                status = "completed"
            elif success_count > 0:
                status = "partial"
            else:
                status = "error"
            
            return {
                "status": status,
                "pages": final_downloaded,
                "expected": expected_count,
                "new_pages": success_count,
                "chapter_name": chapter_name,
                "volume": vol,
                "chapter_number": ch_num_str
            }
            
        except Exception as e:
            logger.error(f"❌ Ошибка в _download_chapter_smart: {e}", exc_info=True)
            return {"status": "error", "error": str(e)}
    
    async def _save_or_update_metadata(
        self,
        manga_dir: Path,
        title: str,
        url: str,
        task: DownloadTask
    ):
        """
        🔹 Сохраняет metadata.json в конце скачивания
        🔹 Использует ТОЛЬКО обычные dict (без Pydantic)
        🔹 Обрабатывает повреждённые/пустые файлы
        """
        
        meta_file = manga_dir / "metadata.json"
        
        # 🔹 1. Загружаем существующие метаданные (с обработкой ошибок)
        existing_meta = {}
        if meta_file.exists() and meta_file.stat().st_size > 0:
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:  # 🔹 Проверяем, что файл не пустой
                        existing_meta = json.loads(content)
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ metadata.json повреждён: {e}. Создаём новый.")
                # 🔹 Бэкап повреждённого файла
                backup = manga_dir / f"metadata.json.bak.{int(datetime.now().timestamp())}"
                meta_file.rename(backup)
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения metadata: {e}")
        
        # 🔹 2. Сканируем ВСЕ папки глав на диске
        chapters_meta = {}
        
        for item in manga_dir.iterdir():
            if not item.is_dir() or item.name.startswith('.'):
                continue
            
            # 🔹 Парсим имя папки: v{vol}c{num}
            match = re.match(r'^v(\d+)c(\d+(?:\.\d+)?)$', item.name)
            if match:
                vol = int(match.group(1))
                chap_str = match.group(2)  # "130", "7.5", "13"
                
                # 🔹 Считаем страницы в папке
                pages = (list(item.glob("*.png")) + 
                        list(item.glob("*.jpg")) + 
                        list(item.glob("*.jpeg")) +
                        list(item.glob("*.webp")))
                
                # 🔹 Получаем ожидаемое количество (из кэша или дефолт)
                existing_chapter = existing_meta.get("chapters", {}).get(item.name, {})
                expected = existing_chapter.get("pages_expected", len(pages) if len(pages) > 0 else 10)
                
                # 🔹 Сохраняем статус главы (обычный dict!)
                chapters_meta[item.name] = {
                    "volume": vol,
                    "number": float(chap_str) if '.' in chap_str else int(chap_str),
                    "number_api": chap_str,  # сохраняем как строку "130"
                    "pages_expected": expected,
                    "pages_downloaded": len(pages),
                    "completed": len(pages) >= expected and len(pages) > 0,
                    "updated_at": datetime.now().isoformat()
                }
        
        logger.info(f"📊 Найдено глав на диске: {len(chapters_meta)}")
        
        # 🔹 3. Получаем свежие метаданные манги с сервера (опционально)
        fresh_meta = {}
        try:
            fresh_meta = await self.mangalib.get_metadata(task.manga_slug)
        except Exception as e:
            logger.warning(f"⚠️ Не удалось получить метаданные с сервера: {e}")
            # 🔹 Используем старые данные как фоллбэк
            fresh_meta = {
                "title": existing_meta.get("title"),
                "cover": existing_meta.get("cover_url"),
                "genres": existing_meta.get("genres", []),
            }
        
        # 🔹 4. Формируем финальные метаданные (ОБЫЧНЫЙ DICT, не Pydantic!)
        metadata = {
            # 🔹 Основная информация
            "title": fresh_meta.get("title") or existing_meta.get("title") or title,
            "source": "mangalib",
            "source_url": url,  # 🔹 Обязательно: строка
            
            # 🔹 Даты
            "downloaded_at": existing_meta.get("downloaded_at") or datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            
            # 🔹 Главы — ВСЕ что есть на диске
            "chapters": chapters_meta,
            
            # 🔹 Статистика
            "total_chapters": len(chapters_meta),
            "completed_chapters": sum(1 for c in chapters_meta.values() if c.get("completed")),
            
            # 🔹 Дополнительная информация
            "cover": fresh_meta.get("cover") or existing_meta.get("cover_url"),
            "cover_url": fresh_meta.get("cover") or existing_meta.get("cover_url"),
            "description": fresh_meta.get("description") or existing_meta.get("description"),
            "genres": fresh_meta.get("genres", []) or existing_meta.get("genres", []),
            "tags": fresh_meta.get("tags", []) or existing_meta.get("tags", []),
            "status": fresh_meta.get("status") or existing_meta.get("status"),
            "authors": fresh_meta.get("authors", []) or existing_meta.get("authors", []),
            "artists": fresh_meta.get("artists", []) or existing_meta.get("authors", []),
            "rating": fresh_meta.get("rating") or existing_meta.get("rating"),
        }
        
        # 🔹 5. Сохраняем (обычный dict → json)
        try:
            with open(meta_file, 'w', encoding='utf-8') as f:
                # 🔹 ensure_ascii=False для кириллицы, indent=2 для читаемости
                json.dump(metadata, f, ensure_ascii=False, indent=2)
            logger.info(f"💾 metadata.json сохранён: {len(chapters_meta)} глав, {metadata['completed_chapters']} завершено")
        except Exception as e:
            logger.error(f"❌ Ошибка сохранения metadata: {e}")
            # 🔹 Попытка записать минимальные данные
            try:
                minimal = {
                    "title": title,
                    "source": "mangalib",
                    "source_url": url,
                    "chapters": chapters_meta,
                    "updated_at": datetime.now().isoformat()
                }
                with open(meta_file, 'w', encoding='utf-8') as f:
                    json.dump(minimal, f, ensure_ascii=False, indent=2)
                logger.info("💾 Сохранён минимальный metadata.json")
            except Exception as e2:
                logger.error(f"❌ Критическая ошибка: {e2}")


    def cancel_task(self, task_id: str) -> bool:
        if task_id in self.tasks:
            self.tasks[task_id].cancel_requested = True
            return True
        return False
    
    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        return self.tasks.get(task_id)
    
    def get_downloaded_manga(self) -> List[DownloadMetadata]:
        """Сканирует manga/ и возвращает метаданные скачанных манг"""
        downloads = []
        
        for manga_dir in self.manga_path.iterdir():
            if not manga_dir.is_dir() or manga_dir.name.startswith('.'):
                continue
            
            meta_file = manga_dir / "metadata.json"
            if not meta_file.exists():
                continue
            
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta_dict = json.load(f)
                
                # 🔹 Создаём модель с валидацией
                meta = DownloadMetadata(**meta_dict)
                downloads.append(meta)
                
            except Exception as e:
                logger.warning(f"⚠️ Ошибка чтения {manga_dir.name}: {e}")
        
        return downloads
    
    async def cleanup(self):
        """Закрытие ресурсов"""
        await self.mangalib.close()


