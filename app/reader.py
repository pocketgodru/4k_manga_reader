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
    
    def get_chapters_with_info(self, slug: str, source: str = "manga") -> List[Dict]:
        """Возвращает главы с распарсенной информацией для отображения"""
        chapters = self.get_chapters(slug, source)
        metadata = self.get_metadata(slug, source="manga")
        result = []
        
        for chapter_name in chapters:
            volume, chapter_num = self.parse_chapter_name(chapter_name)
            
            # Пытаемся получить доп. инфо из metadata
            meta_chapter = {}
            if metadata and 'chapters' in metadata:
                meta_chapter = metadata['chapters'].get(chapter_name, {})
            
            result.append({
                'name': chapter_name,
                'volume': int(volume),
                'chapter': chapter_num,
                'display': f"Том {int(volume)}, Глава {chapter_num}",
                'pages': meta_chapter.get('pages_downloaded', 0),
                'upscaled': meta_chapter.get('upscaled', False)
            })
        
        return result
    
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
    
    def get_upscale_status(self, slug: str) -> Dict:
        """Возвращает статус апскейла по всем главам"""
        chapters = self.get_chapters(slug, source="manga")
        status = {}
        for chapter in chapters:
            status[chapter] = {
                "upscaled": self.is_chapter_upscaled(slug, chapter),
                "pages_count": len(self.get_pages(slug, chapter, quality="manga")),
                "volume": self.parse_chapter_name(chapter)[0],
                "chapter_num": self.parse_chapter_name(chapter)[1]
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