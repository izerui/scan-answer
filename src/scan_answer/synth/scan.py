"""模拟扫描：把模板坐标系下的"纸面图"变成一张扫描图（几何扰动 + 成像噪声）。"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..engine.align import px_per_mm


@dataclass
class ScanParams:
    rotate_deg: float = 0.0
    shift_mm: tuple[float, float] = (0.0, 0.0)
    scale: float = 1.0
    corner_jitter_mm: float = 0.0  # 轻微透视
    wave_mm: float = 0.0  # 非刚性起伏幅度
    blur_sigma: float = 0.0
    noise_sigma: float = 0.0
    brightness: float = 0.0
    contrast: float = 1.0
    jpeg_quality: int = 95
    margin_mm: float = 15.0  # 扫描图在纸张四周留白（扫描仪背板）


def random_params(rng: np.random.Generator) -> ScanParams:
    return ScanParams(
        rotate_deg=float(rng.uniform(-3, 3)),
        shift_mm=(float(rng.uniform(-8, 8)), float(rng.uniform(-8, 8))),
        scale=float(rng.uniform(0.98, 1.02)),
        corner_jitter_mm=float(rng.uniform(0, 0.8)),
        wave_mm=float(rng.uniform(0, 0.4)),
        blur_sigma=float(rng.uniform(0, 0.8)),
        noise_sigma=float(rng.uniform(0, 5)),
        brightness=float(rng.uniform(-12, 12)),
        contrast=float(rng.uniform(0.92, 1.08)),
        jpeg_quality=int(rng.integers(80, 96)),
    )


def scanify(paper: np.ndarray, paper_dpi: float, out_dpi: float, p: ScanParams, rng: np.random.Generator) -> np.ndarray:
    s_in, s_out = px_per_mm(paper_dpi), px_per_mm(out_dpi)
    h_mm, w_mm = paper.shape[0] / s_in, paper.shape[1] / s_in
    W, H = int((w_mm + 2 * p.margin_mm) * s_out), int((h_mm + 2 * p.margin_mm) * s_out)
    # 纸面四角（输入像素）-> 扫描图位置（输出像素）
    src = np.float32([[0, 0], [paper.shape[1], 0], [paper.shape[1], paper.shape[0]], [0, paper.shape[0]]])
    corners_mm = np.float32([[0, 0], [w_mm, 0], [w_mm, h_mm], [0, h_mm]])
    c = corners_mm.mean(0)
    th = np.radians(p.rotate_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]], np.float32)
    moved = (corners_mm - c) @ R.T * p.scale + c + np.float32(p.shift_mm) + p.margin_mm
    moved += rng.uniform(-p.corner_jitter_mm, p.corner_jitter_mm, moved.shape).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, moved * s_out)
    out = cv2.warpPerspective(paper, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=245)
    if p.wave_mm > 0:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        amp = p.wave_mm * s_out
        mapx = xx + amp * np.sin(yy / H * np.pi * 2)
        mapy = yy + amp * np.sin(xx / W * np.pi * 1.5)
        out = cv2.remap(out, mapx, mapy, cv2.INTER_LINEAR, borderValue=245)
    if p.blur_sigma > 0.05:
        out = cv2.GaussianBlur(out, (0, 0), p.blur_sigma)
    f = out.astype(np.float32) * p.contrast + p.brightness + (p.contrast - 1) * -128
    if p.noise_sigma > 0:
        f += rng.normal(0, p.noise_sigma, f.shape).astype(np.float32)
    out = np.clip(f, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, p.jpeg_quality])
    return cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)


def show_through(back: np.ndarray, front: np.ndarray, strength: float = 0.10) -> np.ndarray:
    """背面叠加正面的镜像淡影（透印）。"""
    ghost = 255 - (255 - cv2.flip(front, 1).astype(np.float32)) * strength
    return np.minimum(back, ghost.astype(np.uint8))


def fold_corner(paper: np.ndarray, dpi: float, size_mm: float = 35.0) -> np.ndarray:
    """右下角折角：用背板颜色的三角形盖住右下角（会遮住定位块）。"""
    s = px_per_mm(dpi)
    h, w = paper.shape
    k = int(size_mm * s)
    out = paper.copy()
    tri = np.array([[w, h - k], [w, h], [w - k, h]], np.int32)
    cv2.fillPoly(out, [tri], 245)
    return out
