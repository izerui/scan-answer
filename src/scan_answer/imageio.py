"""图片读写：兼容 Windows 上的中文路径。

OpenCV 的 imread / imwrite 在 Windows 上不支持非 ASCII 路径，这里改为先在内存中编解码，再用 numpy 读写文件。
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def imread_gray(path: str | Path) -> np.ndarray | None:
    """以灰度读取图片；文件不存在或无法解码时返回 None。"""
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, cv2.IMREAD_GRAYSCALE)


def imwrite(path: str | Path, img: np.ndarray, params: list[int] | None = None) -> bool:
    """按扩展名编码后写入文件；编码或写入失败返回 False。"""
    ext = Path(path).suffix or ".png"
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        return False
    try:
        buf.tofile(str(path))
    except OSError:
        return False
    return True
