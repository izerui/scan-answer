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


def find_barcodes(gray: np.ndarray) -> list[tuple[str, str, tuple[float, float]]]:
    """整页搜索：返回所有读到的码 [(文本, 码制, 中心像素坐标)]。原图读不到时再试二值化。"""
    out: dict[str, tuple[str, str, tuple[float, float]]] = {}
    for img in (gray, cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]):
        for r in zxingcpp.read_barcodes(img):
            if not (r.valid and r.text) or r.text in out:
                continue
            pos = r.position
            pts = [pos.top_left, pos.top_right, pos.bottom_right, pos.bottom_left]
            cx = sum(pt.x for pt in pts) / 4
            cy = sum(pt.y for pt in pts) / 4
            out[r.text] = (r.text, r.format.name, (float(cx), float(cy)))
        if out:
            break
    return list(out.values())
