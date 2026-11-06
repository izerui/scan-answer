"""条码 / 二维码识别（zxing-cpp）。"""
from __future__ import annotations

import cv2
import numpy as np
import zxingcpp


def _try(img: np.ndarray) -> tuple[str, str] | None:
    for r in zxingcpp.read_barcodes(img):
        if r.valid and r.text:
            return r.text, r.format.name
    return None


def read_barcode(gray: np.ndarray) -> tuple[str, str] | None:
    """依次尝试：原图、Otsu 二值化、放大 2 倍。返回 (文本, 码制) 或 None。"""
    hit = _try(gray)
    if hit:
        return hit
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    hit = _try(bw)
    if hit:
        return hit
    return _try(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
