# app/enhancer.py

import cv2
import numpy as np
from PIL import Image, ImageEnhance
from pathlib import Path
from typing import Union, Optional, Dict, List
import re
import subprocess
import asyncio
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


class UpscalerEngine:
    """
    🔹 Универсальный движок апскейла
    Поддерживает: ncnn-vulkan (GPU) и CPU fallback
    """
    
    def __init__(self, config: Dict):
        self.config = config
        self.upscaler_config = config.get('upscaler', {})
        self.method = self.upscaler_config.get('method', 'cpu')
        
        # 🔹 Пути к инструментам
        self.ncnn_exe = Path(self.upscaler_config.get('ncnn', {}).get('exe_path', 'tools/realesrgan-ncnn-vulkan.exe'))
        self.models_dir = Path(self.upscaler_config.get('ncnn', {}).get('models_dir', 'tools/models'))
        
        # 🔹 Параметры
        self.ncnn_model = self.upscaler_config.get('ncnn', {}).get('model_name', 'realesr-animevideov3')
        self.scale = self.upscaler_config.get('ncnn', {}).get('scale', 2)
        self.tile = self.upscaler_config.get('ncnn', {}).get('tile', 512)
        self.gpu_id = self.upscaler_config.get('ncnn', {}).get('gpu_id', 0)
        self.threads = self.upscaler_config.get('ncnn', {}).get('threads', '4:8:4')
        self.output_format = self.upscaler_config.get('ncnn', {}).get('output_format', 'jpg')
        
        # 🔹 Пути к данным — ИСПРАВЛЕНО!
        self.data_path = Path(config.get('data', {}).get('path', 'data'))
        self.manga_folder = Path(config.get('data', {}).get('manga_folder', 'manga'))
        self.upscaled_folder = Path(config.get('data', {}).get('upscaled_folder', 'upscaled'))  # 🔹 Отдельная папка!
        
        self.cpu_config = self.upscaler_config.get('cpu', {})
        self.ncnn_available = self._check_ncnn()

        self._cancel_flags: Dict[str, bool] = {}
        
        if self.method == 'ncnn-vulkan' and not self.ncnn_available:
            logger.warning("⚠️ ncnn-vulkan недоступен, переключение на CPU")
            self.method = 'cpu'
    
    def _check_ncnn(self) -> bool:
        """Проверяет наличие ncnn-vulkan exe"""
        return self.ncnn_exe.exists() and self.models_dir.exists()
    
    async def upscale_image(
        self,
        img_path: Union[str, Path],
        output_path: Union[str, Path],
        force_method: Optional[str] = None
    ) -> Dict:
        """
        🔹 Апскейл одного изображения
        
        Args:
            img_path: Путь к исходному изображению
            output_path: Путь для сохранения
            force_method: Принудительный метод ("ncnn-vulkan" или "cpu")
        
        Returns:
            Dict со статусом и метаданными
        """
        img_path = Path(img_path)
        output_path = Path(output_path)
        
        if not img_path.exists():
            return {
                "status": "error",
                "error": f"Файл не найден: {img_path}"
            }
        
        # 🔹 Выбор метода
        method = force_method or self.method
        
        start_time = datetime.now()
        
        try:
            if method == 'ncnn-vulkan' and self.ncnn_available:
                result = await self._upscale_ncnn(img_path, output_path)
            else:
                result = await self._upscale_cpu(img_path, output_path)
            
            end_time = datetime.now()
            processing_time = (end_time - start_time).total_seconds()
            
            result["processing_time_sec"] = processing_time
            result["method"] = method
            
            logger.debug(f"✅ Апскейл завершён: {img_path.name} ({processing_time:.2f}с, {method})")
            
            return result
        
        except Exception as e:
            logger.error(f"❌ Ошибка апскейла {img_path.name}: {e}")
            return {
                "status": "error",
                "error": str(e),
                "method": method
            }
    
    async def _upscale_ncnn(self, img_path: Path, output_path: Path) -> Dict:
        """🔹 Апскейл через ncnn-vulkan (GPU) — с subprocess.run в потоке"""
        
        # 🔹 Резолвим пути в абсолютные
        ncnn_exe_abs = self.ncnn_exe.resolve()
        models_dir_abs = self.models_dir.resolve()
        img_path_abs = img_path.resolve()
        output_path_abs = output_path.resolve()
        
        # 🔹 Создаём родительскую папку
        output_path_abs.parent.mkdir(parents=True, exist_ok=True)
        
        # 🔹 Логи для отладки
        logger.debug(f"🔍 ncnn_exe: {ncnn_exe_abs}")
        logger.debug(f"🔍 models_dir: {models_dir_abs}")
        logger.debug(f"🔍 input: {img_path_abs}")
        logger.debug(f"🔍 output: {output_path_abs}")
        
        # 🔹 Проверка существования
        if not ncnn_exe_abs.exists():
            return {"status": "error", "error": f"exe не найден: {ncnn_exe_abs}"}
        
        if not models_dir_abs.exists():
            return {"status": "error", "error": f"models не найдены: {models_dir_abs}"}
        
        if not img_path_abs.exists():
            return {"status": "error", "error": f"input не найден: {img_path_abs}"}
        
        # 🔹 Формируем команду
        cmd = [
            str(ncnn_exe_abs),
            "-i", str(img_path_abs),
            "-o", str(output_path_abs),
            "-n", self.ncnn_model,
            "-s", str(self.scale),
            "-t", str(self.tile),
            "-g", str(self.gpu_id),
            "-j", self.threads,
            "-m", str(models_dir_abs),
            "-f", self.output_format
        ]
        
        logger.debug(f"🔍 CMD: {' '.join(cmd)}")
        
        # 🔹 Запускаем в отдельном потоке (обход NotImplementedError)
        def run_ncnn_sync():
            import subprocess
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3600,  # 1 час таймаут
                    cwd=str(ncnn_exe_abs.parent),
                    creationflags=subprocess.CREATE_NO_WINDOW  # 🔹 Скрыть консоль на Windows
                )
                return result
            except subprocess.TimeoutExpired:
                logger.error(f"⏰ Таймаут ncnn для {img_path_abs}")
                return None
            except Exception as e:
                logger.error(f"❌ Ошибка subprocess: {e}")
                return None
        
        try:
            # 🔹 Запуск в потоке (обход asyncio ограничений Windows)
            result = await asyncio.to_thread(run_ncnn_sync)
            
            if result is None:
                return {"status": "error", "error": "subprocess не запустился"}
            
            # 🔹 Декодируем вывод
            stdout_text = result.stdout.strip() if result.stdout else ""
            stderr_text = result.stderr.strip() if result.stderr else ""
            
            # 🔹 Попытка decode для Windows кодировки
            if not stderr_text and result.stderr:
                try:
                    stderr_text = result.stderr.decode('cp866', errors='ignore').strip()
                except:
                    pass
            
            logger.debug(f"🔍 STDOUT: {stdout_text[:200] if stdout_text else '(пусто)'}")
            logger.debug(f"🔍 STDERR: {stderr_text[:200] if stderr_text else '(пусто)'}")
            
            if result.returncode == 0:
                if output_path_abs.exists() and output_path_abs.stat().st_size > 0:
                    return {
                        "status": "completed",
                        "output_path": str(output_path_abs),
                        "gpu": True
                    }
                else:
                    return {
                        "status": "error",
                        "error": f"Выходной файл пуст: {output_path_abs}"
                    }
            else:
                error_msg = stderr_text or stdout_text or f"Код возврата: {result.returncode}"
                logger.error(f"❌ ncnn ошибка {result.returncode}: {error_msg}")
                return {"status": "error", "error": error_msg}
        
        except Exception as e:
            logger.error(f"❌ Exception в _upscale_ncnn: {type(e).__name__}: {e}", exc_info=True)
            return {"status": "error", "error": f"{type(e).__name__}: {e}"}
    
    async def _upscale_cpu(self, img_path: Path, output_path: Path) -> Dict:
        """🔹 Апскейл через CPU (fallback)"""
        
        try:
            upscaled = cpu_upscale(
                img_path,
                output_path,
                scale=self.cpu_config.get('scale', self.scale),
                interpolation=self.cpu_config.get('interpolation', 'bicubic'),
                unsharp_mask=self.cpu_config.get('unsharp_mask', True),
                contrast=self.cpu_config.get('contrast', 1.1),
                sharpness=self.cpu_config.get('sharpness', 1.5)
            )
            
            return {
                "status": "completed",
                "output_path": str(output_path),
                "gpu": False
            }
        
        except Exception as e:
            return {
                "status": "error",
                "error": str(e)
            }
    
    async def upscale_chapter(
        self,
        slug: str,
        chapter: str,
        progress_callback=None
    ) -> Dict:
        """
        🔹 Апскейл ЧЕРЕЗ ПАПКУ (быстро!)
        Сохраняет в data/upscaled/{slug}/{chapter}/
        """
        # 🔹 Нормализуем slug
        normalized_slug = slug.replace("--", "_").replace("-", "_")
        
        # 🔹 Пути к исходной и целевой папкам
        original_dir = self.data_path / self.manga_folder / normalized_slug / chapter
        upscaled_dir = self.data_path / self.upscaled_folder / normalized_slug / chapter  # 🔹 Отдельная папка!
        
        if not original_dir.exists():
            return {"status": "error", "error": f"Папка не найдена: {original_dir}"}
        
        # 🔹 Считаем изображения
        supported_ext = {".jpg", ".jpeg", ".png", ".webp"}
        images = []
        for ext in supported_ext:
            images.extend(original_dir.glob(f"*{ext}"))
        images.sort(key=lambda x: x.name)
        
        if not images:
            return {"status": "error", "error": f"Нет изображений: {chapter}"}
        
        total = len(images)
        logger.info(f"🔹 Апскейл {chapter}: {total} изображений (папка: {original_dir})")
        
        # 🔹 Создаём выходную папку
        upscaled_dir.mkdir(parents=True, exist_ok=True)
        
        start_time = datetime.now()
        
        try:
            if self.method == 'ncnn-vulkan' and self.ncnn_available:
                # 🔹 БЫСТРЫЙ МЕТОД: обработка всей папки за 1 запуск!
                result = await self._upscale_ncnn_folder(original_dir, upscaled_dir)
            else:
                # 🔹 Медленный CPU fallback (пофайлово)
                result = await self._upscale_cpu_chapter(original_dir, upscaled_dir)
            
            end_time = datetime.now()
            processing_time = (end_time - start_time).total_seconds()
            
            result["processing_time_sec"] = processing_time
            result["method"] = self.method
            
            logger.info(f"✅ Апскейл завершён: {chapter} ({processing_time:.2f}с, {self.method})")
            
            return result
        
        except Exception as e:
            logger.error(f"❌ Ошибка апскейла {chapter}: {e}")
            return {"status": "error", "error": str(e)}
        
    async def _upscale_ncnn_folder(self, input_dir: Path, output_dir: Path) -> Dict:
        """
        🔹 БЫСТРЫЙ МЕТОД: ncnn обрабатывает всю папку за 1 запуск
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        cmd = [
            str(self.ncnn_exe.resolve()),
            "-i", str(input_dir.resolve()),
            "-o", str(output_dir.resolve()),
            "-n", self.ncnn_model,
            "-s", str(self.scale),
            "-t", str(self.tile),
            "-g", str(self.gpu_id),
            "-j", self.threads,
            "-m", str(self.models_dir.resolve()),
            "-f", self.output_format
        ]
        
        logger.debug(f"🔍 CMD: {' '.join(cmd)}")
        
        def run_ncnn_sync():
            import subprocess
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    cwd=str(self.ncnn_exe.parent),
                    creationflags=subprocess.CREATE_NO_WINDOW
                )
                return result
            except Exception as e:
                logger.error(f"❌ subprocess ошибка: {e}")
                return None
        
        result = await asyncio.to_thread(run_ncnn_sync)
        
        if result is None:
            return {"status": "error", "error": "subprocess не запустился"}
        
        if result.returncode == 0:
            # 🔹 Считаем обработанные файлы
            processed = sum(1 for _ in output_dir.glob(f"*.{self.output_format}"))
            return {
                "status": "completed",
                "processed": processed,
                "total": processed,
                "gpu": True
            }
        else:
            stderr_text = result.stderr.decode('cp866', errors='ignore').strip() if result.stderr else ""
            return {
                "status": "error",
                "error": stderr_text or f"Код возврата: {result.returncode}"
            }
    
    async def _upscale_cpu_chapter(self, input_dir: Path, output_dir: Path) -> Dict:
        """🔹 Медленный CPU fallback (пофайлово)"""
        supported_ext = {".jpg", ".jpeg", ".png", ".webp"}
        images = []
        for ext in supported_ext:
            images.extend(input_dir.glob(f"*{ext}"))
        images.sort(key=lambda x: x.name)
        
        processed = 0
        failed = 0
        
        output_dir.mkdir(parents=True, exist_ok=True)
        
        for img_path in images:
            output_path = output_dir / f"{img_path.stem}.{self.output_format}"
            try:
                cpu_upscale(img_path, output_path, scale=self.cpu_config.get('scale', self.scale))
                processed += 1
            except Exception as e:
                logger.error(f"❌ CPU апскейл ошибка {img_path}: {e}")
                failed += 1
        
        return {
            "status": "completed" if failed == 0 else "partial",
            "processed": processed,
            "failed": failed,
            "total": len(images),
            "gpu": False
        }

    async def upscale_manga(
        self,
        slug: str,
        task_id: str,
        chapters: List[str] = None,
        progress_callback=None
    ) -> Dict:
        """🔹 Массовый апскейл всей манги"""
        
        from app.reader import MangaReader  # или передайте reader как аргумент
        
        normalized_slug = slug.replace("--", "_").replace("-", "_")
        
        # 🔹 Если chapters не переданы — получаем все
        if not chapters:
            # 🔹 Сканируем папку с оригиналами
            manga_dir = self.data_path / self.manga_folder / normalized_slug
            if not manga_dir.exists():
                return {"status": "error", "error": f"Манга не найдена: {manga_dir}"}
            
            chapters = [
                d.name for d in manga_dir.iterdir()
                if d.is_dir() and re.match(r'^v\d+c[\d.]+$', d.name)
            ]
            chapters.sort(key=lambda x: [int(t) if t.isdigit() else t for t in re.split(r'([0-9]+)', x)])
        
        total = len(chapters)
        processed = 0
        failed = 0
        
        for i, chapter in enumerate(chapters):

            if self.is_cancelled(task_id):
                logger.info(f"⏹️ Апскейл {task_id} отменён на главе {i+1}/{total}: {chapter}")
                self.clear_cancel_flag(task_id)  # 🔹 Очистка
                return {
                    "status": "cancelled",
                    "processed": processed,
                    "total": total,
                    "cancelled_at": chapter
                }
            
            if progress_callback:
                await progress_callback(i, total, chapter, "started")
            
            result = await self.upscale_chapter(slug, chapter)
            
            if result.get("status") == "completed":
                processed += 1
            else:
                failed += 1
            
            if progress_callback:
                await progress_callback(i + 1, total, chapter, result.get("status"))
        
        if self.is_cancelled(task_id):
            logger.info(f"⏹️ Апскейл {task_id} отменён после главы {chapter}")
            self.clear_cancel_flag(task_id)
            return {
                "status": "cancelled",
                "processed": processed,
                "total": total,
                "cancelled_at": chapter
            }
    
        # 🔹 Очистка флага при успешном завершении
        self.clear_cancel_flag(task_id)
        
        return {
            "status": "completed",
            "processed": processed,
            "total": total,
            "method": self.method
        }


    async def get_upscale_status(
        self,
        slug: str,
        chapters: List[str] = None
    ) -> Dict[str, any]:
        """
        🔹 Проверяет статус апскейла для каждой главы
        🔹 Возвращает готовые и ожидающие главы для прогресс-бара
        """
        from pathlib import Path
        import json
        import re
        
        normalized_slug = slug.replace("--", "_").replace("-", "_")
        
        # 🔹 Пути
        manga_dir = self.data_path / self.manga_folder / normalized_slug
        upscaled_base = self.data_path / self.upscaled_folder / normalized_slug
        meta_file = manga_dir / "metadata.json"
        
        # 🔹 Загружаем метаданные (если есть)
        meta_upscale = {}
        if meta_file.exists():
            try:
                with open(meta_file, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                    meta_upscale = meta.get("upscale_info", {})
            except:
                pass
        
        # 🔹 Если chapters не переданы — сканируем папку
        if not chapters:
            if not manga_dir.exists():
                return {"ready": [], "pending": [], "total": 0}
            
            chapters = []
            for item in manga_dir.iterdir():
                if item.is_dir() and not item.name.startswith('_'):
                    if re.match(r'^v\d+c[\d.]+$', item.name):
                        chapters.append(item.name)
            chapters.sort(key=lambda x: [int(t) if t.isdigit() else t for t in re.split(r'([0-9]+)', x)])
        
        ready = []      # ✅ Уже апскейлены
        pending = []    # ⏳ Нужно апскейлить
        broken = []     # ❌ Проблемные (для отладки)
        
        for chapter in chapters:
            original_dir = manga_dir / chapter
            upscaled_dir = upscaled_base / chapter
            
            # 🔹 1. Проверяем оригинал — есть ли вообще страницы
            if not original_dir.exists():
                broken.append({"chapter": chapter, "reason": "original_missing"})
                continue
            
            original_pages = list(original_dir.glob("*.png")) + \
                            list(original_dir.glob("*.jpg")) + \
                            list(original_dir.glob("*.jpeg")) + \
                            list(original_dir.glob("*.webp"))
            
            if not original_pages:
                broken.append({"chapter": chapter, "reason": "no_original_pages"})
                continue
            
            original_count = len(original_pages)
            
            # 🔹 2. Проверяем upscaled папку
            upscaled_pages = []
            if upscaled_dir.exists():
                upscaled_pages = list(upscaled_dir.glob(f"*.{self.output_format}"))
            
            upscaled_count = len(upscaled_pages)
            
            # 🔹 3. Проверяем metadata
            meta_info = meta_upscale.get(chapter, {})
            meta_upscaled = meta_info.get("upscaled", False)
            meta_expected = meta_info.get("total_images", original_count)
            
            # 🔹 🔹 🔹 ЛОГИКА ОПРЕДЕЛЕНИЯ СТАТУСА 🔹 🔹 🔹
            
            # ✅ Готова: есть файлы И их количество >= оригинала
            if upscaled_count > 0 and upscaled_count >= original_count:
                ready.append({
                    "chapter": chapter,
                    "original": original_count,
                    "upscaled": upscaled_count,
                    "source": "files"  # Подтверждено файлами
                })
            
            # ✅ Готова: нет файлов, но metadata говорит что готово (резервная проверка)
            elif meta_upscaled and upscaled_count == 0:
                # 🔹 Проверяем, не битая ли запись
                if meta_expected <= original_count:
                    ready.append({
                        "chapter": chapter,
                        "original": original_count,
                        "upscaled": meta_expected,
                        "source": "metadata"  # Только по metadata
                    })
                else:
                    broken.append({
                        "chapter": chapter,
                        "reason": "metadata_mismatch",
                        "meta_expected": meta_expected,
                        "original": original_count
                    })
                    pending.append({
                        "chapter": chapter,
                        "original": original_count,
                        "upscaled": 0,
                        "reason": "metadata_mismatch"
                    })
            
            # ⏳ Нужно апскейлить
            else:
                pending.append({
                    "chapter": chapter,
                    "original": original_count,
                    "upscaled": upscaled_count,
                    "progress": round((upscaled_count / original_count) * 100, 1) if original_count > 0 else 0
                })
        
        return {
            "slug": slug,
            "total": len(chapters),
            "ready": ready,
            "ready_count": len(ready),
            "pending": pending,
            "pending_count": len(pending),
            "broken": broken,
            "progress_percent": round((len(ready) / max(len(chapters), 1)) * 100, 1),
            "method": self.method
        }


    # 🔹 Метод для установки флага отмены
    def set_cancel_flag(self, task_id: str, value: bool):
        """Устанавливает флаг отмены для задачи"""
        self._cancel_flags[task_id] = value
        logger.info(f"{'✅' if value else '❌'} Флаг отмены {task_id}: {value}")

    # 🔹 Метод для проверки флага
    def is_cancelled(self, task_id: str) -> bool:
        """Проверяет, запрошена ли отмена"""
        return self._cancel_flags.get(task_id, False)

    # 🔹 Метод для очистки флага
    def clear_cancel_flag(self, task_id: str):
        """Очищает флаг после завершения задачи"""
        self._cancel_flags.pop(task_id, None)

# 🔹 Старые функции (для обратной совместимости)

def load_image(path: Union[str, Path]) -> np.ndarray:
    """Загружает изображение через OpenCV"""
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def save_image(img: np.ndarray, path: Union[str, Path]) -> None:
    """Сохраняет изображение через OpenCV"""
    cv2.imwrite(str(path), img)


def cpu_upscale(
    img_path: Union[str, Path],
    output_path: Union[str, Path],
    scale: int = 2,
    interpolation: str = "bicubic",
    unsharp_mask: bool = True,
    contrast: float = 1.1,
    sharpness: float = 1.5
) -> Optional[np.ndarray]:
    """
    🔹 CPU fallback upscale pipeline:
    - Bicubic/Lanczos interpolation
    - Unsharp mask
    - Contrast enhancement
    """
    img = load_image(img_path)
    if img is None:
        raise ValueError(f"Не удалось загрузить изображение: {img_path}")
    
    h, w = img.shape[:2]
    new_w, new_h = w * scale, h * scale
    
    # 🔹 Выбор интерполяции
    interp_method = cv2.INTER_LANCZOS4 if interpolation == "lanczos" else cv2.INTER_CUBIC
    
    # Bicubic upscale
    upscaled = cv2.resize(img, (new_w, new_h), interpolation=interp_method)
    
    # Unsharp mask
    if unsharp_mask:
        gaussian = cv2.GaussianBlur(upscaled, (0, 0), 2.0)
        upscaled = cv2.addWeighted(upscaled, 1.5, gaussian, -0.5, 0)
    
    # Contrast enhancement via PIL
    pil_img = Image.fromarray(cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB))
    enhancer = ImageEnhance.Contrast(pil_img) 
    pil_img = enhancer.enhance(contrast)
    sharpener = ImageEnhance.Sharpness(pil_img)
    pil_img = sharpener.enhance(sharpness)
    
    upscaled = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    save_image(upscaled, output_path)
    return upscaled


def enhance_for_display(img: Image.Image, config: dict) -> Image.Image:
    """Лёгкие улучшения для отображения (без ресайза)"""
    if config.get('sharpen', 0) > 0:
        img = ImageEnhance.Sharpness(img).enhance(1 + config['sharpen'])
    if config.get('contrast', 1) != 1:
        img = ImageEnhance.Contrast(img).enhance(config['contrast'])
    if config.get('brightness', 1) != 1:
        img = ImageEnhance.Brightness(img).enhance(config['brightness'])
    return img


# 🔹 Фабрика для создания движка
def create_upscaler(config: Dict) -> UpscalerEngine:
    """Создаёт экземпляр UpscalerEngine из конфига"""
    return UpscalerEngine(config)