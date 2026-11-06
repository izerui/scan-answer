"""OMR：涂黑度计算、局部微调、判定、批次阈值。

涂黑度 fill：格子内（四周内缩 15%）每个像素 clip((底图 - 扫描图) / 200, 0, 1) 的均值。
扣除了空白底图上的印刷内容（[A]、底纹），2B 铅笔满涂约 0.8~1.0，未涂约 0。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..template.model import Rect
from .align import px_per_mm

DEFAULT_MARK = 0.45  # 已涂阈值
UNCERTAIN_RATIO = 0.45  # 存疑阈值 = 已涂阈值 * 该比例
SECOND_RATIO = 0.35  # 单选：第二名 >= 第一名 * 该比例 时判为存疑


@dataclass(frozen=True)
class Thresholds:
    mark: float = DEFAULT_MARK
    uncertain: float = DEFAULT_MARK * UNCERTAIN_RATIO


def _px(rect: Rect, dpi: float, inset: float = 0.0) -> tuple[int, int, int, int]:
    s = px_per_mm(dpi)
    x, y, w, h = rect
    dx, dy = w * inset, h * inset
    return (int(round((x + dx) * s)), int(round((y + dy) * s)),
            int(round((x + w - dx) * s)), int(round((y + h - dy) * s)))


def cell_fill(work: np.ndarray, base: np.ndarray, rect: Rect, dpi: float, shift: tuple[int, int] = (0, 0)) -> float:
    x0, y0, x1, y1 = _px(rect, dpi, inset=0.15)
    dx, dy = shift
    a = work[y0 + dy:y1 + dy, x0 + dx:x1 + dx].astype(np.float32)
    b = base[y0:y1, x0:x1].astype(np.float32)
    if a.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.clip((b - a) / 200.0, 0, 1).mean())


def grid_bbox(cells: list[list[Rect]]) -> Rect:
    xs = [c[0] for row in cells for c in row] + [c[0] + c[2] for row in cells for c in row]
    ys = [c[1] for row in cells for c in row] + [c[1] + c[3] for row in cells for c in row]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def local_shift(work: np.ndarray, base: np.ndarray, bbox: Rect, dpi: float, max_shift_mm: float = 1.5) -> tuple[int, int]:
    """在 ±max_shift_mm 内，把扫描图的 bbox 区域与空白底图对应区域做匹配，返回像素偏移 (dx, dy)。"""
    x0, y0, x1, y1 = _px(bbox, dpi)
    m = int(round(max_shift_mm * px_per_mm(dpi)))
    tpl = base[y0:y1, x0:x1]
    sy0, sx0 = max(0, y0 - m), max(0, x0 - m)
    search = work[sy0:y1 + m, sx0:x1 + m]
    if tpl.size == 0 or search.shape[0] < tpl.shape[0] or search.shape[1] < tpl.shape[1]:
        return (0, 0)
    res = cv2.matchTemplate(cv2.GaussianBlur(search, (3, 3), 0), cv2.GaussianBlur(tpl, (3, 3), 0), cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < 0.3:
        return (0, 0)
    return (loc[0] + sx0 - x0, loc[1] + sy0 - y0)


def grid_fills(work: np.ndarray, base: np.ndarray, cells: list[list[Rect]], dpi: float) -> np.ndarray:
    shift = local_shift(work, base, grid_bbox(cells), dpi)
    return np.array([[cell_fill(work, base, c, dpi, shift) for c in row] for row in cells], dtype=np.float32)


def decide(fills: np.ndarray, labels: list[str], qtype: str, th: Thresholds) -> tuple[str, str]:
    """返回 (answer, status)。answer 为选中标签按顺序拼接；status: ok/blank/multiple/uncertain。"""
    order = np.argsort(-fills)
    top = float(fills[order[0]])
    second = float(fills[order[1]]) if len(fills) > 1 else 0.0
    marked = [i for i in range(len(fills)) if fills[i] >= th.mark]
    ambiguous = [i for i in range(len(fills)) if th.uncertain <= fills[i] < th.mark]
    if not marked:
        if top >= th.uncertain:
            return labels[int(order[0])], "uncertain"
        return "", "blank"
    answer = "".join(labels[i] for i in sorted(marked))
    if qtype in ("single", "judge"):
        if len(marked) > 1:
            return answer, "multiple"
        # 第二名明显偏弱（如擦除残留）不算存疑；与第一名接近才存疑
        return answer, ("uncertain" if second >= max(th.uncertain, SECOND_RATIO * top) else "ok")
    # 多选
    return answer, ("uncertain" if ambiguous else "ok")


def fit_thresholds(all_fills: np.ndarray) -> Thresholds:
    """批次自适应：对全部涂黑度做 Otsu 分割，已涂阈值限制在 [0.30, 0.60]。"""
    v = np.asarray(all_fills, dtype=np.float32).ravel()
    if v.size < 50 or (v >= 0.5).sum() < 5:
        return Thresholds()
    hist, edges = np.histogram(v, bins=100, range=(0, 1))
    hist = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers)
    mu0 = np.divide(m0, w0, out=np.zeros_like(m0), where=w0 > 0)
    mu1 = np.divide(m0[-1] - m0, w1, out=np.zeros_like(m0), where=w1 > 0)
    between = w0 * w1 * (mu0 - mu1) ** 2
    t = float(np.clip(centers[int(np.argmax(between))], 0.30, 0.60))
    return Thresholds(mark=t, uncertain=t * UNCERTAIN_RATIO)
