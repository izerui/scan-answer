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
    if y0 + dy < 0 or x0 + dx < 0:
        return 0.0
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
    """在 ±max_shift_mm 内，把扫描图的 bbox 区域与空白底图对应区域做匹配，返回像素偏移 (dx, dy)。

    底图区域面积太小或纹理不足时返回 (0, 0)，避免无特征区域的假匹配。
    """
    x0, y0, x1, y1 = _px(bbox, dpi)
    m = int(round(max_shift_mm * px_per_mm(dpi)))
    tpl = base[y0:y1, x0:x1]
    sy0, sx0 = max(0, y0 - m), max(0, x0 - m)
    search = work[sy0:y1 + m, sx0:x1 + m]
    if tpl.size == 0 or search.shape[0] < tpl.shape[0] or search.shape[1] < tpl.shape[1]:
        return (0, 0)
    # 底图区域纹理不足时（几乎全白），模板匹配不可靠
    if float(tpl.std()) < 8.0:
        return (0, 0)
    res = cv2.matchTemplate(cv2.GaussianBlur(search, (3, 3), 0), cv2.GaussianBlur(tpl, (3, 3), 0), cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < 0.3:
        return (0, 0)
    return (loc[0] + sx0 - x0, loc[1] + sy0 - y0)


def grid_fills(work: np.ndarray, base: np.ndarray, cells: list[list[Rect]], dpi: float) -> np.ndarray:
    shift = local_shift(work, base, grid_bbox(cells), dpi)
    return np.array([[cell_fill(work, base, c, dpi, shift) for c in row] for row in cells], dtype=np.float32)


def decide_idx(fills: np.ndarray, qtype: str, th: Thresholds) -> tuple[list[int], str]:
    """返回 (选中下标升序列表, status)。判定规则与 decide 完全相同。"""
    order = np.argsort(-fills)
    top = float(fills[order[0]])
    second = float(fills[order[1]]) if len(fills) > 1 else 0.0
    marked = sorted(i for i in range(len(fills)) if fills[i] >= th.mark)
    ambiguous = [i for i in range(len(fills)) if th.uncertain <= fills[i] < th.mark]
    if not marked:
        if top >= th.uncertain:
            return [int(order[0])], "uncertain"
        return [], "blank"
    if qtype in ("single", "judge"):
        if len(marked) > 1:
            return marked, "multiple"
        return marked, ("uncertain" if second >= max(th.uncertain, SECOND_RATIO * top) else "ok")
    # 多选
    return marked, ("uncertain" if ambiguous else "ok")


def decide(fills: np.ndarray, labels: list[str], qtype: str, th: Thresholds) -> tuple[str, str]:
    """返回 (answer, status)。answer 为选中标签按顺序拼接；status: ok/blank/multiple/uncertain。"""
    idxs, status = decide_idx(fills, qtype, th)
    return "".join(labels[i] for i in idxs), status


def _otsu(v: np.ndarray) -> float:
    hist, edges = np.histogram(v, bins=100, range=(0, 1))
    hist = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers)
    mu0 = np.divide(m0, w0, out=np.zeros_like(m0), where=w0 > 0)
    mu1 = np.divide(m0[-1] - m0, w1, out=np.zeros_like(m0), where=w1 > 0)
    between = w0 * w1 * (mu0 - mu1) ** 2
    return float(centers[int(np.argmax(between))])


LIGHT_MEDIAN = 0.55  # 已涂组涂黑度中位数低于此值视为整批偏浅
MIN_SEPARATION = 0.35  # 已涂组与未涂组均值之差低于此值视为区分度低


def threshold_issues(all_fills: np.ndarray) -> list[dict]:
    """批次涂黑度分布的异常提示（与 fit_thresholds 同一套 Otsu 分组）；样本不足时返回空列表。"""
    v = np.asarray(all_fills, dtype=np.float32).ravel()
    if v.size < 50 or (v >= 0.5).sum() < 5:
        return []
    raw = _otsu(v)
    t = float(np.clip(raw, 0.30, 0.60))
    marked, blank = v[v >= t], v[v < t]
    issues: list[dict] = []
    if marked.size and float(np.median(marked)) < LIGHT_MEDIAN:
        issues.append({"code": "LIGHT_MARKS", "msg": f"本批填涂整体偏浅（已涂选项涂黑度中位数 {float(np.median(marked)):.2f}），"
                                                     "可能是铅笔偏软或扫描偏亮；浅涂会被标为存疑"})
    if marked.size and blank.size and float(marked.mean() - blank.mean()) < MIN_SEPARATION:
        issues.append({"code": "LOW_SEPARATION", "msg": "已涂与未涂区分度低，存疑题可能较多，建议检查扫描参数（亮度、颜色滤除）"})
    if raw < 0.30 or raw > 0.60:
        issues.append({"code": "THRESHOLD_CLAMPED", "msg": f"阈值超出正常范围（{raw:.2f}），已按 {t:.2f} 截断，请抽查识别结果"})
    return issues


def fit_thresholds(all_fills: np.ndarray) -> Thresholds:
    """批次自适应：对全部涂黑度做 Otsu 分割，已涂阈值限制在 [0.30, 0.60]。"""
    v = np.asarray(all_fills, dtype=np.float32).ravel()
    if v.size < 50 or (v >= 0.5).sum() < 5:
        return Thresholds()
    t = float(np.clip(_otsu(v), 0.30, 0.60))
    return Thresholds(mark=t, uncertain=t * UNCERTAIN_RATIO)
