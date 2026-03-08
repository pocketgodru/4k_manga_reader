# app/reader.py
import os
import re
import json
from pathlib import Path
from typing import Optional, List, Dict, Tuple

class MangaReader:
    def __init__(self, data_path: str, manga_folder: str, upscaled_folder: str):
        self.base_path = Path(data_path)
        self.manga_path = self.base_path / manga_folder
        self.upscaled_path = self.base_path / upscaled_folder
    
    def get_manga_list(self) -> List[str]:
        """Возвращает список доступных манги (названия папок)"""
        return [d.name for d in self.manga_path.iterdir() if d.is_dir()]
    
    def get_metadata(self, slug: str, source: str = "manga") -> Optional[Dict]:
        """Читает metadata.json из manga/ или upscaled/"""
        base = self.upscaled_path if source == "upscaled" else self.manga_path
        meta_file = base / slug / "metadata.json"
        if meta_file.exists():
            with open(meta_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None
    
    def save_metadata(self, slug: str, metadata: Dict, source: str = "upscaled") -> None:
        """Сохраняет metadata.json в manga/ или upscaled/"""
        base = self.upscaled_path if source == "upscaled" else self.manga_path
        meta_file = base / slug / "metadata.json"
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)
    
    def parse_chapter_name(self, chapter_name: str) -> Tuple[float, float]:
        """
        Парсит имя главы в формате v{том}c{номер}
        Примеры:
            v1c2      -> (1, 2.0)
            v1c11     -> (1, 11.0)
            v1c111    -> (1, 111.0)
            v28c2790  -> (28, 279.0)  # 4 цифры = 279.0
            v37c3831  -> (37, 383.1)  # 4 цифры = 383.1
            v1c2.1    -> (1, 2.1)     # явная точка
            v120c130.1-> (120, 130.1)
        """
        match = re.match(r'^v(\d+)c(\d+(?:\.\d+)?)$', chapter_name)
        if match:
            volume = int(match.group(1))
            chapter_str = match.group(2)
            
            if '.' in chapter_str:
                # Уже есть точка: v1c2.1 -> 2.1
                chapter = float(chapter_str)
            else:
                chapter_num = int(chapter_str)
                # ✅ 4+ цифры = последняя цифра это версия: 3831 -> 383.1
                if chapter_num >= 1000:
                    chapter = chapter_num / 10.0
                else:
                    # 1-3 цифры = обычный номер: 11 -> 11.0
                    chapter = float(chapter_num)
            
            return (volume, chapter)
        
        return (0, 0.0)
    
    def get_chapters(self, slug: str, source: str = "manga") -> List[str]:
        """Возвращает отсортированный список глав (папок)"""
        base = self.upscaled_path if source == "upscaled" else self.manga_path
        manga_dir = base / slug
        if manga_dir.exists():
            chapters = [d.name for d in manga_dir.iterdir() 
                       if d.is_dir() and not d.name.startswith('.')]
            # ✅ Сортируем по тому и номеру главы
            return sorted(chapters, key=lambda x: self.parse_chapter_name(x))
        return []
    
    def get_chapters_with_info(self, slug: str) -> List[dict]:
        """Возвращает список глав, отсортированный по номеру"""
        
        manga_path = self.manga_path / slug
        #print(manga_path)
        if not manga_path.exists():
            return []
        
        def _parse_chapter_key(chapter_name: str) -> tuple:
            """
            Парсит имя главы формата v{vol}c{num} и возвращает кортеж для сортировки.
            Примеры:
                'v1c7' → (1, 7.0)
                'v1c7.5' → (1, 7.5)
                'v37c3680' → (37, 3680.0)
                'chapter_001' → (0, 1.0)  # фоллбэк
            """
            # 🔹 Основной формат: v{vol}c{num} или v{vol}c{num.sub}
            match = re.match(r'v(\d+)c(\d+(?:\.\d+)?)', chapter_name)
            if match:
                volume = int(match.group(1))
                chapter = float(match.group(2))
                return (volume, chapter)
            
            # 🔹 Фоллбэк: chapter_XXX
            match = re.match(r'chapter_(\d+(?:\.\d+)?)', chapter_name)
            if match:
                return (0, float(match.group(1)))
            
            # 🔹 Неизвестный формат — в конец списка
            return (9999, 9999)
    
        chapters = []
        
        for chapter_dir in manga_path.iterdir():
            if not chapter_dir.is_dir() or chapter_dir.name.startswith('.'):
                continue
            
            chapter_name = chapter_dir.name
            
            # 🔹 Считаем страницы (все форматы)
            pages = (list(chapter_dir.glob("*.png")) + 
                    list(chapter_dir.glob("*.jpg")) + 
                    list(chapter_dir.glob("*.jpeg")) +
                    list(chapter_dir.glob("*.webp")))
            
            # 🔹 Извлекаем номер главы для отображения
            match = re.match(r'v(\d+)c(\d+(?:\.\d+)?)', chapter_name)
            if match:
                display_chapter = match.group(2)  # Показываем только номер главы
            else:
                display_chapter = chapter_name.replace("chapter_", "")
            
            chapters.append({
                "name": chapter_name,
                "chapter": display_chapter,
                "volume": int(match.group(1)) if match else 1,
                "pages_count": len(pages),
                "_sort_key": _parse_chapter_key(chapter_name)  # 🔹 Для сортировки
            })
        
        # 🔹 Сортируем: сначала по тому, потом по главе
        chapters.sort(key=lambda x: x["_sort_key"])
        
        # 🔹 Удаляем служебное поле
        for ch in chapters:
            del ch["_sort_key"]
        
        return chapters
    
    def get_pages(self, slug: str, chapter: str, quality: str = "manga") -> List[str]:
        """Возвращает список путей к изображениям"""
        base = self.upscaled_path if quality == "upscaled" else self.manga_path
        chapter_dir = base / slug / chapter
        if chapter_dir.exists():
            return sorted([str(p) for p in chapter_dir.iterdir() 
                          if p.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']])
        return []
    
    def get_page_path(self, slug: str, chapter: str, page_idx: int, quality: str = "manga") -> Optional[str]:
        """Получает путь к конкретной странице"""
        pages = self.get_pages(slug, chapter, quality)
        if 0 <= page_idx < len(pages):
            return pages[page_idx]
        return None
    
    def is_chapter_upscaled(self, slug: str, chapter: str) -> bool:
        """Проверяет, существует ли уже апскейленная версия главы"""
        upscaled_dir = self.upscaled_path / slug / chapter
        if not upscaled_dir.exists():
            return False
        files = [f for f in upscaled_dir.iterdir() 
                if f.suffix.lower() in ['.jpg', '.jpeg', '.png', '.webp']]
        return len(files) > 0
    
    def get_upscale_status(self, slug: str) -> dict:
        """Возвращает статус глав с учётом metadata.json"""
        
        status = {}
        manga_path =  self.manga_path / slug
        upscaled_path =  self.upscaled_path / slug
        if not manga_path.exists():
            return {}
        
        # 🔹 Загружаем metadata.json (если есть)
        chapters_meta = {}
        meta_file = manga_path / "metadata.json"
        if meta_file.exists():
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    full_meta = json.load(f)
                chapters_meta = full_meta.get("chapters", {})
            except:
                pass
        
        # 🔹 Обходим главы
        for chapter_dir in manga_path.iterdir():
            if not chapter_dir.is_dir() or chapter_dir.name.startswith('.'):
                continue
            
            chapter_name = chapter_dir.name
            
            # 🔹 Считаем страницы
            pages = (list(chapter_dir.glob("*.png")) +
                    list(chapter_dir.glob("*.jpg")) +
                    list(chapter_dir.glob("*.jpeg")) +
                    list(chapter_dir.glob("*.webp")))
            total_pages = len(pages)
            
            # 🔹 Проверяем апскейл
            upscaled_dir = upscaled_path / chapter_name
            is_upscaled = False
            upscaled_count = 0
            
            if upscaled_dir.exists():
                upscaled_pages = list(upscaled_dir.glob("*.png")) + list(upscaled_dir.glob("*.jpg"))
                upscaled_count = len(upscaled_pages)
                is_upscaled = upscaled_count >= total_pages and total_pages > 0
            
            # 🔹 Метаданные скачивания
            dl_meta = chapters_meta.get(chapter_name, {})
            
            status[chapter_name] = {
                "upscaled": is_upscaled,
                "pages_count": total_pages,
                "upscaled_count": upscaled_count,
                "is_downloaded": bool(chapters_meta),  # Если есть metadata.json — скачанная
                "download_completed": dl_meta.get("completed", False) if dl_meta else None,
                "pages_downloaded": dl_meta.get("pages_downloaded", total_pages) if dl_meta else total_pages,
                "pages_expected": dl_meta.get("pages_expected", total_pages) if dl_meta else total_pages,
            }
        
        return status
    
    def create_upscaled_metadata(self, slug: str) -> Dict:
        """Создаёт metadata.json для upscaled/ на основе оригинала"""
        original_meta = self.get_metadata(slug, source="manga")
        if not original_meta:
            raise ValueError(f"Оригинальный metadata.json не найден для {slug}")
        
        upscaled_meta = original_meta.copy()
        upscaled_meta['source'] = 'upscaled'
        upscaled_meta['upscale_info'] = {
            'method': 'cpu_bicubic_unsharp',
            'scale': 2,
            'generated_at': None
        }
        
        chapters_status = self.get_upscale_status(slug)
        upscaled_meta['chapters'] = {}
        
        for chapter, info in chapters_status.items():
            orig_chapter = original_meta.get('chapters', {}).get(chapter, {})
            upscaled_meta['chapters'][chapter] = {
                **orig_chapter,
                'upscaled': info['upscaled'],
                'pages_downloaded': info['pages_count'],
                'pages_expected': info['pages_count'] if info['upscaled'] else orig_chapter.get('pages_expected', 0)
            }
        
        upscaled_meta['upscale_info']['generated_at'] = str(Path.cwd())
        upscaled_meta['total_chapters'] = len(chapters_status)
        upscaled_meta['upscaled_chapters'] = sum(1 for c in chapters_status.values() if c['upscaled'])
        
        return upscaled_meta

    def get_downloaded_chapter_status(self, slug: str) -> Dict[str, dict]:
        """
        Получает статус глав из скачанной манги (metadata.json)
        Возвращает: { "v1c1.0": {"completed": True, "pages_downloaded": 52, "pages_expected": 52}, ... }
        """
        from pathlib import Path
        import json
        
        downloader = None
        # 🔹 Попытка импортировать downloader (если модуль инициализирован)
        try:
            from app.main import downloader
        except:
            pass
        
        if not downloader:
            # 🔹 Фоллбэк: прямой доступ к папке downloads
            downloads_path = Path(self.base_path) / "downloads" / slug
            meta_path = downloads_path / "metadata.json"
        else:
            meta_path = downloader.download_folder / slug / "metadata.json"
        
        if not meta_path.exists():
            return {}
        
        try:
            with open(meta_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            return metadata.get("chapters", {})
        except Exception as e:
            print(f"⚠️ Ошибка чтения metadata {slug}: {e}")
            return {}

    def get_chapter_pages_count(self, slug: str, chapter: str, source: str = "manga") -> int:
        """Возвращает количество страниц в главе из указанного источника"""
        from pathlib import Path
        folder = self.manga_path if source == "manga" else "downloads"
        chapter_path = Path(self.base_path) / folder / slug / chapter
        
        if not chapter_path.exists():
            return 0
        
        pages = (list(chapter_path.glob("*.png")) + 
                list(chapter_path.glob("*.jpg")) + 
                list(chapter_path.glob("*.jpeg")) +
                list(chapter_path.glob("*.webp")))
        return len(pages)

