# app/enhancer.py
import cv2
import numpy as np
from PIL import Image , ImageEnhance
from pathlib import Path
from typing import Union

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
) -> None:
    """
    CPU fallback upscale pipeline:
    - Bicubic interpolation
    - Unsharp mask
    - Contrast enhancement
    """
    img = load_image(img_path)
    if img is None:
        raise ValueError(f"Не удалось загрузить изображение: {img_path}")
    
    h, w = img.shape[:2]
    new_w, new_h = w * scale, h * scale
    
    # Bicubic upscale
    upscaled = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_CUBIC)
    
    # Unsharp mask
    gaussian = cv2.GaussianBlur(upscaled, (0, 0), 2.0)
    upscaled = cv2.addWeighted(upscaled, 1.5, gaussian, -0.5, 0)
    
    # Contrast enhancement via PIL
    pil_img = Image.fromarray(cv2.cvtColor(upscaled, cv2.COLOR_BGR2RGB))
    enhancer = ImageEnhance.Contrast(pil_img) 
    pil_img = enhancer.enhance(1.1)
    
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