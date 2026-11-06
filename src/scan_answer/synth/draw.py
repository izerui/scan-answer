"""在模板坐标系（template.dpi）的底图上绘制：铅笔涂卡、手写文字、条码。"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

from ..engine.align import px_per_mm
from ..template.model import Rect

# 涂卡种类 -> 铅笔灰度范围（越小越黑）
LEVELS = {"normal": (35, 90), "light": (160, 182), "erased": (215, 232)}

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "C:/Windows/Fonts/simkai.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def find_cjk_font() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def pencil_mark(img: np.ndarray, rect: Rect, dpi: float, kind: str, rng: np.random.Generator) -> None:
    """在 rect（mm）内画一个铅笔涂块，原地修改 img（灰度，取较暗值合成）。"""
    s = px_per_mm(dpi)
    x, y, w, h = rect
    cov = rng.uniform(0.82, 1.0)
    cx, cy = (x + w / 2) * s, (y + h / 2) * s
    hw, hh = w * cov / 2 * s, h * cov / 2 * s
    cx += rng.uniform(-0.08, 0.08) * w * s
    cy += rng.uniform(-0.08, 0.08) * h * s
    x0, y0 = int(cx - hw), int(cy - hh)
    x1, y1 = int(cx + hw), int(cy + hh)
    lo, hi = LEVELS[kind]
    level = rng.uniform(lo, hi)
    patch = np.full((y1 - y0, x1 - x0), level, np.float32)
    patch += rng.normal(0, 12, patch.shape)  # 石墨颗粒
    for _ in range(int(rng.integers(2, 6))):  # 涂抹笔触
        yy = int(rng.integers(0, max(1, patch.shape[0])))
        patch[yy:yy + 2, :] += rng.uniform(-25, 25)
    mask = np.zeros(patch.shape, np.uint8)
    cv2.rectangle(mask, (0, 0), (patch.shape[1] - 1, patch.shape[0] - 1), 255, -1)
    r = max(1, int(min(patch.shape) * 0.25))  # 圆角
    for (px, py) in ((0, 0), (patch.shape[1] - 1, 0), (0, patch.shape[0] - 1), (patch.shape[1] - 1, patch.shape[0] - 1)):
        cv2.circle(mask, (px, py), r, 0, -1)
    patch = np.clip(patch, 0, 255).astype(np.uint8)
    # 裁剪到图像边界，防止负索引 wrap-around 或越界
    img_h, img_w = img.shape[:2]
    cx0, cy0 = max(x0, 0), max(y0, 0)
    cx1, cy1 = min(x1, img_w), min(y1, img_h)
    if cx0 >= cx1 or cy0 >= cy1:
        return
    patch = patch[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    mask = mask[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0]
    region = img[cy0:cy1, cx0:cx1]
    blended = np.where(mask > 0, np.minimum(region, patch), region)
    img[cy0:cy1, cx0:cx1] = blended


def handwrite(img: np.ndarray, rect: Rect, dpi: float, text: str, rng: np.random.Generator,
              char_mm: float = 6.0, pitch_mm: float | None = None, line_mm: float | None = None) -> None:
    """在 rect 内逐字写入文字（带随机抖动），模拟手写；无中文字体时画随机笔画。"""
    s = px_per_mm(dpi)
    x, y, w, h = rect
    pitch = (pitch_mm or char_mm * 1.15) * s
    line = (line_mm or char_mm * 1.4) * s
    font_path = find_cjk_font()
    pil = Image.fromarray(img)
    draw = ImageDraw.Draw(pil)
    font = ImageFont.truetype(font_path, int(char_mm * s)) if font_path else None
    cx, cy = x * s + pitch * 0.15, y * s + line * 0.15
    for ch in text:
        if cx + pitch > (x + w) * s:
            cx, cy = x * s + pitch * 0.15, cy + line
        if cy + line > (y + h) * s:
            break
        jx, jy = rng.uniform(-0.12, 0.12) * pitch, rng.uniform(-0.12, 0.12) * line
        ink = int(rng.uniform(20, 60))
        if font:
            draw.text((cx + jx, cy + jy), ch, font=font, fill=ink)
        else:
            pts = [(cx + jx + rng.uniform(0, pitch * 0.8), cy + jy + rng.uniform(0, line * 0.8)) for _ in range(4)]
            draw.line(pts, fill=ink, width=max(1, int(0.3 * s)))
        cx += pitch
    img[:] = np.asarray(pil)


def paste_barcode(img: np.ndarray, rect: Rect, dpi: float, text: str) -> None:
    """在 rect 中央贴一张白底 Code128 条码标签（约 rect 宽的 80%）。"""
    s = px_per_mm(dpi)
    bc = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.Code128)
    code = np.asarray(zxingcpp.write_barcode_to_image(bc, scale=4, add_quiet_zones=True))
    x, y, w, h = rect
    tw, th = int(w * 0.8 * s), int(min(h * 0.6, 14) * s)
    code = cv2.resize(code, (tw, th), interpolation=cv2.INTER_NEAREST)
    ox, oy = int((x + w * 0.1) * s), int((y + (h - min(h * 0.6, 14)) / 2) * s)
    img_h, img_w = img.shape[:2]
    # 条码本体必须完整可读，否则报错
    if ox < 0 or oy < 0 or ox + tw > img_w or oy + th > img_h:
        raise ValueError("条码区域超出图像边界，无法完整绘制")
    # 白底标签：裁剪到图像边界
    lx0, ly0 = max(ox - 8, 0), max(oy - 8, 0)
    lx1, ly1 = min(ox + tw + 8, img_w), min(oy + th + 8, img_h)
    img[ly0:ly1, lx0:lx1] = 255
    img[oy:oy + th, ox:ox + tw] = code
