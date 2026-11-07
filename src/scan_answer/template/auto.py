"""自动检测定位块、外框、同步头；选项框吸附；底图导入与 PDF/图片加载。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .model import Rect, SyncSpec


# ── 结果类型 ──────────────────────────────────────────────────


@dataclass
class SnapResult:
    cells: list[list[Rect]]  # 吸附后的格子（未通过时为原格子）
    ok: bool
    inliers: int
    total: int
    max_residual_mm: float
    message: str  # 中文
    # 结果代码：ok / dense（格子太密，结构上不能吸附，换起点也没用）/ blank / few / residual /
    # misaligned（可能整体错位一行或一列）/ shape / unsupported
    code: str = ""


@dataclass
class ImportedBase:
    image: np.ndarray  # 灰度，300 DPI，已纠偏
    paper: tuple[float, float]  # mm，保留 1 位小数
    skew_deg: float  # 纠正掉的角度
    method: str  # "anchors" / "frames" / "none"


# ── 内部工具 ──────────────────────────────────────────────────


def _blobs(
    base: np.ndarray,
    dpi: float,
    min_mm: float,
    max_mm: float,
    min_solid: float,
) -> list[Rect]:
    """Otsu 反二值化后提取连通域，返回满足尺寸和实心度的矩形列表。"""
    s = dpi / 25.4
    _, bw = cv2.threshold(base, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
    n, _lab, stats, _cen = cv2.connectedComponentsWithStats(bw, 8)
    out: list[Rect] = []
    for i in range(1, n):
        x, y, w, h, area = (int(stats[i, j]) for j in range(5))
        wm, hm = w / s, h / s
        if min_mm <= wm <= max_mm and min_mm <= hm <= max_mm and area / (w * h) >= min_solid:
            out.append((x / s, y / s, wm, hm))
    return out


def _rect_center(r: Rect) -> tuple[float, float]:
    return r[0] + r[2] / 2, r[1] + r[3] / 2


# ── 自动检测 ──────────────────────────────────────────────────


def auto_anchors(
    base: np.ndarray,
    dpi: float,
    paper: tuple[float, float],
) -> list[Rect] | None:
    """在底图上检测四角定位块。返回 [左上, 右上, 右下, 左下] 或 None。"""
    W, H = paper
    bl = [b for b in _blobs(base, dpi, 2.5, 12, 0.75) if 0.5 <= b[2] / b[3] <= 2.0]
    corners = [(0, 0), (W, 0), (W, H), (0, H)]
    result: list[Rect] = []
    for cx, cy in corners:
        cands = [b for b in bl if abs(b[0] + b[2] / 2 - cx) < W * 0.2 and abs(b[1] + b[3] / 2 - cy) < H * 0.2]
        if not cands:
            return None
        result.append(min(cands, key=lambda b: (b[0] + b[2] / 2 - cx) ** 2 + (b[1] + b[3] / 2 - cy) ** 2))
    return result


def auto_frames(
    base: np.ndarray,
    dpi: float,
    paper: tuple[float, float],
    max_n: int = 4,
) -> list[Rect]:
    """自适应阈值后检测四边形外框，返回按面积从大到小的矩形列表。"""
    s = dpi / 25.4
    paper_area_px = paper[0] * paper[1] * s * s
    bw = cv2.adaptiveThreshold(base, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
    cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects: list[Rect] = []
    for c in cnts:
        x, y, w, h = cv2.boundingRect(c)
        area = w * h
        if area < 0.05 * paper_area_px or area > 0.9 * paper_area_px:
            continue
        approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
        if len(approx) != 4:
            continue
        rects.append((x / s, y / s, w / s, h / s))
    # 按面积从大到小排序
    rects.sort(key=lambda r: -(r[2] * r[3]))
    # 去掉被其他框包含的（容差 2 mm）
    keep: list[Rect] = []
    for r in rects:
        contained = any(
            r[0] >= k[0] - 2 and r[1] >= k[1] - 2 and r[0] + r[2] <= k[0] + k[2] + 2 and r[1] + r[3] <= k[1] + k[3] + 2
            for k in keep
        )
        if not contained:
            keep.append(r)
    return keep[:max_n]


def auto_sync(
    base: np.ndarray,
    dpi: float,
    min_count: int = 5,
) -> list[SyncSpec]:
    """检测同步头（长条形黑块按行或列分组）。face 固定填 "front"，由调用方修改。"""
    bl = [b for b in _blobs(base, dpi, 1.0, 8, 0.7) if max(b[2], b[3]) / min(b[2], b[3]) > 1.3]

    # 按 x 中心聚类为竖列（vertical）
    v_groups: list[list[Rect]] = []
    for b in sorted(bl, key=lambda b: b[0]):
        cx = b[0] + b[2] / 2
        placed = False
        for g in v_groups:
            gcx = g[0][0] + g[0][2] / 2
            if abs(gcx - cx) < 1.0 and abs(g[0][2] - b[2]) < 1.0 and abs(g[0][3] - b[3]) < 1.0:
                g.append(b)
                placed = True
                break
        if not placed:
            v_groups.append([b])

    # 按 y 中心聚类为横行（horizontal）
    h_groups: list[list[Rect]] = []
    for b in sorted(bl, key=lambda b: b[1]):
        cy = b[1] + b[3] / 2
        placed = False
        for g in h_groups:
            gcy = g[0][1] + g[0][3] / 2
            if abs(gcy - cy) < 1.0 and abs(g[0][2] - b[2]) < 1.0 and abs(g[0][3] - b[3]) < 1.0:
                g.append(b)
                placed = True
                break
        if not placed:
            h_groups.append([b])

    # 过滤出至少 min_count 个的组；每个块只归入较大的那组
    all_candidates: list[tuple[str, list[Rect]]] = []
    for g in v_groups:
        if len(g) >= min_count:
            all_candidates.append(("vertical", g))
    for g in h_groups:
        if len(g) >= min_count:
            all_candidates.append(("horizontal", g))

    # 按组大小从大到小排，每个块只归入最大的组
    all_candidates.sort(key=lambda x: -len(x[1]))
    used: set[tuple[float, float, float, float]] = set()
    result: list[SyncSpec] = []
    for direction, group in all_candidates:
        filtered = [b for b in group if b not in used]
        if len(filtered) < min_count:
            continue
        for b in filtered:
            used.add(b)
        if direction == "vertical":
            filtered.sort(key=lambda b: b[1])
        else:
            filtered.sort(key=lambda b: b[0])
        result.append(SyncSpec(face="front", direction=direction, marks=filtered))

    return result


# ── grid_in_rect ──────────────────────────────────────────────


def grid_in_rect(
    rect: Rect,
    n_groups: int,
    n_options: int,
    options_horizontal: bool,
    block_every: int = 0,
    gap_ratio: float = 0.5,
    cell_ratio: float = 0.62,
) -> list[list[Rect]]:
    """在矩形内铺满网格，第一格和最后一格外沿贴着矩形边。

    组间额外间距 = gap_ratio * group_pitch，每 block_every 组插入一次。
    格子尺寸 = cell_ratio * 对应方向的格距。
    """
    rx, ry, rw, rh = rect
    if options_horizontal:
        group_span = rh
        option_span = rw
    else:
        group_span = rw
        option_span = rh

    n_extra = (n_groups - 1) // block_every if block_every > 0 else 0

    # group_pitch: 第一格和最后一格外沿贴矩形边
    # group_span = (n_groups - 1 + n_extra * gap_ratio) * gp + cell_ratio * gp
    if n_groups > 1:
        group_pitch = group_span / (n_groups - 1 + n_extra * gap_ratio + cell_ratio)
    else:
        group_pitch = group_span
    cell_size_group = cell_ratio * group_pitch

    if n_options > 1:
        option_pitch = option_span / (n_options - 1 + cell_ratio)
    else:
        option_pitch = option_span
    cell_size_opt = cell_ratio * option_pitch if n_options > 1 else option_span

    cells: list[list[Rect]] = []
    for g in range(n_groups):
        n_extra_before = (g // block_every) if block_every > 0 else 0
        group_offset = g * group_pitch + n_extra_before * gap_ratio * group_pitch
        row: list[Rect] = []
        for o in range(n_options):
            opt_offset = o * option_pitch
            if options_horizontal:
                cx = rx + cell_size_opt / 2 + opt_offset
                cy = ry + cell_size_group / 2 + group_offset
                cw, ch = cell_size_opt, cell_size_group
            else:
                cx = rx + cell_size_group / 2 + group_offset
                cy = ry + cell_size_opt / 2 + opt_offset
                cw, ch = cell_size_group, cell_size_opt
            row.append((round(cx - cw / 2, 3), round(cy - ch / 2, 3), round(cw, 3), round(ch, 3)))
        cells.append(row)
    return cells


# ── snap_grid ─────────────────────────────────────────────────


def snap_grid(
    base: np.ndarray,
    cells: list[list[Rect]],
    dpi: float,
    radius_mm: float = 1.5,
    max_residual_mm: float = 0.4,
) -> SnapResult:
    """在底图上吸附选项网格。"""
    s = dpi / 25.4
    # 墨迹图
    ink = cv2.GaussianBlur((255 - base).astype(np.float32), (0, 0), 1.0)

    G = len(cells)
    O = len(cells[0]) if G > 0 else 0

    # 预计算格子像素尺寸（整个网格格子尺寸相同）
    c0 = cells[0][0]
    cw = max(1, int(round(c0[2] * s)))
    ch = max(1, int(round(c0[3] * s)))
    half_cw_px = max(cw, ch) // 2
    r = int(round(radius_mm * s))

    total = G * O

    # 结构检查：网格间距是否足够大，使 boxFilter 能区分相邻格子。
    # 间距 < 格子宽度的一半时，相邻括号会互相干扰，吸附不可靠。
    if G >= 2 and O >= 2:
        c00, c10, c01 = _rect_center(cells[0][0]), _rect_center(cells[1][0]), _rect_center(cells[0][1])
        # 组方向 (g): cells[0][0] → cells[1][0]
        g_dx, g_dy = c10[0] - c00[0], c10[1] - c00[1]
        g_pitch = (g_dx ** 2 + g_dy ** 2) ** 0.5
        # 该方向上的格子尺寸 = |w·cos + h·sin|
        if g_pitch > 0:
            g_cos, g_sin = abs(g_dx / g_pitch), abs(g_dy / g_pitch)
            g_dim = c0[2] * g_cos + c0[3] * g_sin
        else:
            g_dim = max(c0[2], c0[3])
        # 选项方向 (o): cells[0][0] → cells[0][1]
        o_dx, o_dy = c01[0] - c00[0], c01[1] - c00[1]
        o_pitch = (o_dx ** 2 + o_dy ** 2) ** 0.5
        if o_pitch > 0:
            o_cos, o_sin = abs(o_dx / o_pitch), abs(o_dy / o_pitch)
            o_dim = c0[2] * o_cos + c0[3] * o_sin
        else:
            o_dim = max(c0[2], c0[3])
        g_gap = g_pitch - g_dim
        o_gap = o_pitch - o_dim
        min_gap_ratio = min(g_gap / g_dim if g_dim > 0 else 999,
                            o_gap / o_dim if o_dim > 0 else 999)
        if min_gap_ratio < 0.5:
            return SnapResult(
                cells=cells, ok=False, inliers=0, total=total, max_residual_mm=0.0,
                message="格子间距过小，无法可靠吸附", code="dense",
            )

    # 边框形状校验参数（后验校验使用）
    ew = max(2, min(5, int(round(cw * 0.07))))  # 左/右边缘条宽 ≈3px
    center_start = max(ew, cw // 4)
    center_end = min(cw - ew, 3 * cw // 4)

    # 逐格搜索：用 boxFilter（墨迹均值最大）定位
    found: list[tuple[int, int, float, float]] = []  # (g, o, new_x, new_y)
    for g in range(G):
        for o in range(O):
            c = cells[g][o]
            x, y, w, h = c
            x0 = int(round(x * s)) - r
            y0 = int(round(y * s)) - r
            x0c = max(0, x0)
            y0c = max(0, y0)
            win = ink[y0c: y0 + ch + 2 * r, x0c: x0 + cw + 2 * r]
            if win.shape[0] < ch or win.shape[1] < cw:
                continue
            integ = cv2.boxFilter(win, -1, (cw, ch), normalize=True, anchor=(0, 0))
            integ = integ[: win.shape[0] - ch + 1, : win.shape[1] - cw + 1]
            if integ.size == 0:
                continue
            _, mx, _, loc = cv2.minMaxLoc(integ)
            base_level = float(np.median(integ))
            if mx - base_level < 8:
                continue
            # (a) 窗口边缘检测：峰值在最外 1 像素时视为未找到
            search_h, search_w = integ.shape[:2]
            if loc[0] == 0 or loc[0] >= search_w - 1 or loc[1] == 0 or loc[1] >= search_h - 1:
                continue
            # (b) 双峰检测
            mask = integ.copy()
            my1 = max(0, loc[1] - half_cw_px)
            my2 = min(mask.shape[0], loc[1] + half_cw_px + 1)
            mx1 = max(0, loc[0] - half_cw_px)
            mx2 = min(mask.shape[1], loc[0] + half_cw_px + 1)
            mask[my1:my2, mx1:mx2] = 0
            _, mx2, _, loc2 = cv2.minMaxLoc(mask)
            peak_dist = ((loc2[0] - loc[0]) ** 2 + (loc2[1] - loc[1]) ** 2) ** 0.5
            contrast = mx - base_level
            contrast2 = mx2 - base_level
            if peak_dist >= half_cw_px and contrast2 >= 0.8 * contrast:
                continue
            new_x = (x0c + loc[0]) / s
            new_y = (y0c + loc[1]) / s
            found.append((g, o, new_x, new_y))

    if len(found) < max(3, 0.3 * total):
        if total > 0 and len(found) < 0.1 * total:
            msg, code = "未检测到印刷格子", "blank"
        else:
            msg, code = "可吸附的格子太少", "few"
        return SnapResult(cells=cells, ok=False, inliers=len(found), total=total,
                          max_residual_mm=0.0, message=msg, code=code)

    # 构建原始中心和找到的中心
    orig_pts = np.array([(cells[g][o][0] + cells[g][o][2] / 2, cells[g][o][1] + cells[g][o][3] / 2) for g, o, _, _ in found])
    new_pts = np.array([(nx + cells[g][o][2] / 2, ny + cells[g][o][3] / 2) for g, o, nx, ny in found])

    # 仿射拟合：RANSAC 初始化 + 最小二乘迭代 3 轮剔除
    X = np.c_[orig_pts, np.ones(len(orig_pts))]
    H, inlier_mask = cv2.estimateAffine2D(
        orig_pts.astype(np.float32).reshape(-1, 1, 2),
        new_pts.astype(np.float32).reshape(-1, 1, 2),
        method=cv2.RANSAC,
        ransacReprojThreshold=0.5,
    )
    keep = inlier_mask.ravel() > 0 if inlier_mask is not None and inlier_mask.sum() >= 3 else np.ones(len(X), dtype=bool)
    M = None
    res = np.zeros(len(X))
    for _ in range(3):
        if keep.sum() < 3:
            break
        M, _, _, _ = np.linalg.lstsq(X[keep], new_pts[keep], rcond=None)
        predicted = X @ M
        res = np.linalg.norm(predicted - new_pts, axis=1)
        med_res = float(np.median(res[keep]))
        threshold = max(0.25, 2.5 * med_res)
        keep = res < threshold

    inliers = int(keep.sum())
    max_res = float(np.max(res[keep])) if inliers > 0 else 0.0

    # 判定是否通过
    ok = inliers >= 0.6 * total and max_res <= max_residual_mm
    if not ok:
        if inliers < 0.6 * total:
            msg, code = "可吸附的格子太少", "few"
        else:
            msg, code = f"偏差 {max_res:.2f} mm 超过上限", "residual"
        return SnapResult(cells=cells, ok=False, inliers=inliers, total=total,
                          max_residual_mm=max_res, message=msg, code=code)

    if M is None:
        return SnapResult(cells=cells, ok=False, inliers=inliers, total=total, max_residual_mm=max_res,
                          message="可吸附的格子太少", code="few")

    # 用拟合映射移动所有格
    all_centers = np.array([
        (cells[g][o][0] + cells[g][o][2] / 2, cells[g][o][1] + cells[g][o][3] / 2)
        for g in range(G) for o in range(O)
    ])
    all_X = np.c_[all_centers, np.ones(len(all_centers))]
    new_centers = all_X @ M

    snapped: list[list[Rect]] = []
    k = 0
    for g in range(G):
        row: list[Rect] = []
        for o in range(O):
            cx, cy = new_centers[k]
            w, h = cells[g][o][2], cells[g][o][3]
            row.append((round(cx - w / 2, 3), round(cy - h / 2, 3), w, h))
            k += 1
        snapped.append(row)

    # 后验校验：在吸附后的位置上验证边框形状分。
    # (1) 检查吸附后位置的形状分中位数是否 > 0（边缘比内部亮 = 格子形状）。
    # (2) 对比吸附前后的形状分：若吸附后反而更差，说明映射方向错误。
    def _eval_shape_scores(rects: list[list[Rect]]) -> np.ndarray:
        scores = []
        for row in rects:
            for rx, ry, rw, rh in row:
                px = int(round(rx * s))
                py = int(round(ry * s))
                if px < 0 or py < 0 or py + ch > ink.shape[0] or px + cw > ink.shape[1]:
                    scores.append(-999.0)
                    continue
                patch = ink[py: py + ch, px: px + cw]
                lm = float(patch[:, :ew].mean())
                rm = float(patch[:, cw - ew:].mean())
                cm = float(patch[:, center_start:center_end].mean())
                scores.append(min(lm, rm) - cm)
        return np.array(scores)

    scores_after = _eval_shape_scores(snapped)
    scores_before = _eval_shape_scores(cells)
    valid = (scores_after > -900) & (scores_before > -900)

    if valid.any():
        med_after = float(np.median(scores_after[valid]))
        med_before = float(np.median(scores_before[valid]))
        # 拒绝条件：吸附后中位形状分 < 0（边缘不比内部亮），
        # 或吸附后形状分低于吸附前（映射到了更差的位置）
        if med_after < 0 or (med_after < med_before - 2):
            return SnapResult(
                cells=cells, ok=False, inliers=inliers, total=total,
                max_residual_mm=max_res,
                message="吸附位置边框形状不匹配，请手动调整", code="shape",
            )

    # 整体错位检查：锁到相邻一行 / 一列时，只有最外一行（列）落空，总内点率仍可能有 80%。
    # (1) 每一行、每一列都要有至少一半的格子“确认”：是拟合内点，或者吸附后位置上的边框形状分
    #     不低于内点形状分中位数的一半（靠近题号等干扰时，正确的一列也可能搜索不到，但形状分正常；
    #     落到空白处或文字上的一行 / 一列形状分接近 0）；
    # (2) 网格中心的拟合平移量在 x、y 上都要小于半个间距（行 / 列间距中较小的那个）。
    inlier_set = {(g, o) for (g, o, _, _), k in zip(found, keep) if k}
    sc = scores_after.reshape(G, O)
    ref_score = float(np.median([sc[g, o] for g, o in inlier_set])) if inlier_set else 0.0
    confirmed = np.array([[(g, o) in inlier_set or (ref_score > 0 and sc[g, o] >= 0.5 * ref_score)
                           for o in range(O)] for g in range(G)], dtype=bool).reshape(G, O)
    centre = np.array(_grid_centre(cells))
    shift = np.r_[centre, 1.0] @ M - centre
    half_pitch = 0.5 * _min_pitch(cells)
    if ((confirmed.sum(axis=1) < 0.5 * O).any() or (confirmed.sum(axis=0) < 0.5 * G).any()
            or (np.abs(shift) >= half_pitch).any()):
        return SnapResult(cells=cells, ok=False, inliers=inliers, total=total, max_residual_mm=max_res,
                          message="吸附结果可能整体错位一行或一列，请把框画得更贴近印刷区域", code="misaligned")

    return SnapResult(
        cells=snapped, ok=True, inliers=inliers, total=total,
        max_residual_mm=max_res,
        message=f"已吸附 {inliers}/{total} 格，最大偏差 {max_res:.2f} mm", code="ok",
    )


def _grid_centre(cells: list[list[Rect]]) -> tuple[float, float]:
    pts = [_rect_center(c) for row in cells for c in row]
    return (sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts))


def _min_pitch(cells: list[list[Rect]]) -> float:
    """行 / 列间距中较小的那个（mm）；只有一行一列时返回格子长边。"""
    pitches = []
    if len(cells) >= 2:
        a, b = _rect_center(cells[0][0]), _rect_center(cells[1][0])
        pitches.append(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5)
    if cells and len(cells[0]) >= 2:
        a, b = _rect_center(cells[0][0]), _rect_center(cells[0][1])
        pitches.append(((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5)
    pitches = [p for p in pitches if p > 0]
    if not pitches:
        c = cells[0][0]
        return max(c[2], c[3])
    return min(pitches)


# 换起点重试的候选：平移 (dx, dy) 与向中心内收 d（mm）。只用于原始格子的副本，
# 并且 |dx| + d、|dy| + d 都必须小于半个间距，避免从相邻一行 / 一列起步。
_SEARCH_SHIFTS = (0.0, -0.5, 0.5, -1.0, 1.0, -1.5, 1.5)
_SEARCH_INSETS = (0.0, 0.5, 1.0)


def snap_grid_search(base: np.ndarray, cells: list[list[Rect]], dpi: float) -> SnapResult:
    """先按原位置吸附；失败（且不是结构上不能吸附）时换几个起点重试，取通过且内点最多的一次。

    用户画的框通常比印刷格子松一些、也不居中，按框生成的外圈格子会偏出搜索范围。
    每个候选都从原始格子的副本出发；是否通过仍由 snap_grid 的全部校验判定。"""
    first = snap_grid(base, cells, dpi)
    if first.ok or first.code == "dense":
        return first
    limit = 0.5 * _min_pitch(cells)
    xs = [c[0] for row in cells for c in row]
    ys = [c[1] for row in cells for c in row]
    x0, y0 = min(xs), min(ys)
    x1 = max(c[0] + c[2] for row in cells for c in row)
    y1 = max(c[1] + c[3] for row in cells for c in row)
    w, h = x1 - x0, y1 - y0
    best: SnapResult | None = None
    for d in _SEARCH_INSETS:
        if w <= 4 * d or h <= 4 * d:
            continue
        sx, sy = (w - 2 * d) / w if w > 0 else 1.0, (h - 2 * d) / h if h > 0 else 1.0
        for dx in _SEARCH_SHIFTS:
            for dy in _SEARCH_SHIFTS:
                if (dx, dy, d) == (0.0, 0.0, 0.0):
                    continue
                if abs(dx) + d >= limit or abs(dy) + d >= limit:
                    continue
                cand = [[(round(x0 + d + dx + (cx - x0) * sx, 3), round(y0 + d + dy + (cy - y0) * sy, 3),
                          round(cw * sx, 3), round(ch * sy, 3)) for cx, cy, cw, ch in row] for row in cells]
                res = snap_grid(base, cand, dpi)
                if res.ok and (best is None or res.inliers > best.inliers):
                    best = res
                    if res.inliers >= 0.95 * res.total:
                        return best
    return best if best is not None else first


# ── load_pages ────────────────────────────────────────────────


def load_pages(path: str | Path) -> list[tuple[np.ndarray, float]]:
    """加载 PDF 或图片文件，返回 [(灰度图, dpi)] 列表。"""
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        try:
            import pypdfium2
        except ImportError as e:
            raise ValueError(f"无法读取 {path.name}：缺少 pypdfium2") from e
        try:
            doc = pypdfium2.PdfDocument(str(path))
        except Exception as e:
            raise ValueError(f"无法读取 {path.name}：{e}") from e
        pages: list[tuple[np.ndarray, float]] = []
        try:
            for i in range(len(doc)):
                bitmap = doc[i].render(scale=300 / 72, grayscale=True)
                arr = bitmap.to_numpy()
                # pypdfium2 返回的可能是 (H, W, 1) 或 (H, W)
                if arr.ndim == 3:
                    arr = arr[:, :, 0]
                pages.append((arr, 300.0))
        finally:
            doc.close()
        return pages

    # 图片文件
    try:
        from PIL import Image
        img = Image.open(str(path))
        img.load()  # 强制加载以检测损坏
        gray = np.array(img.convert("L"))
    except Exception as e:
        raise ValueError(f"无法读取 {path.name}：{e}") from e
    dpi_info = img.info.get("dpi")
    dpi = round(float(dpi_info[0])) if dpi_info else 300.0
    return [(gray, dpi)]


# ── import_base ───────────────────────────────────────────────


def import_base(
    gray: np.ndarray,
    dpi: float,
    target_dpi: int = 300,
) -> ImportedBase:
    """导入底图：缩放到 target_dpi、检测纠偏角、旋转。"""
    # 缩放
    if abs(dpi - target_dpi) > 0.5:
        g = cv2.resize(gray, None, fx=target_dpi / dpi, fy=target_dpi / dpi, interpolation=cv2.INTER_AREA)
    else:
        g = gray.copy()

    paper_w = round(g.shape[1] / target_dpi * 25.4, 1)
    paper_h = round(g.shape[0] / target_dpi * 25.4, 1)
    paper = (paper_w, paper_h)

    # 尝试用定位块确定角度
    anchors = auto_anchors(g, target_dpi, paper)
    if anchors is not None:
        # 上边一对、下边一对连线角度的平均
        c = [_rect_center(a) for a in anchors]  # 左上、右上、右下、左下
        a_top = np.degrees(np.arctan2(c[1][1] - c[0][1], c[1][0] - c[0][0]))
        a_bot = np.degrees(np.arctan2(c[2][1] - c[3][1], c[2][0] - c[3][0]))
        angle = (a_top + a_bot) / 2
        method = "anchors"
    else:
        # 最大轮廓 minAreaRect
        bw = cv2.adaptiveThreshold(g, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 15)
        cnts, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        if cnts:
            biggest = max(cnts, key=cv2.contourArea)
            frac = cv2.contourArea(biggest) / g.size
            if frac >= 0.05:
                (_, _), (_, _), ang = cv2.minAreaRect(biggest)
                angle = ((ang + 45) % 90) - 45
                method = "frames"
            else:
                angle = 0.0
                method = "none"
        else:
            angle = 0.0
            method = "none"

    # 绕图像中心旋转
    h_px, w_px = g.shape[:2]
    M = cv2.getRotationMatrix2D((w_px / 2, h_px / 2), angle, 1.0)
    rotated = cv2.warpAffine(g, M, (w_px, h_px), flags=cv2.INTER_LINEAR, borderValue=255)

    return ImportedBase(image=rotated, paper=paper, skew_deg=float(angle), method=method)
