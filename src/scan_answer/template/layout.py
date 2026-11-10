"""按规则批量生成填涂格坐标（模板编辑器"框整块、填参数"也用这个函数）；忽略区掩码。"""
from __future__ import annotations

import numpy as np

from .model import Rect, Template


def ignore_mask(tpl: Template, face: str, shape: tuple[int, ...], dpi: float) -> np.ndarray | None:
    """模板坐标系下、dpi 分辨率、shape 大小的图上忽略区为 True 的掩码；该面没有忽略区时返回 None。

    忽略区是卡面上会干扰自动处理的内容（大块灰底阴影、贴的条码、常被涂写的地方）：
    模板编辑器自动检测定位块 / 外框 / 同步头时当它不存在，识别时整页比对和特征匹配兜底也不看它。"""
    rects = [r.rect for r in tpl.ignore if r.face == face]
    if not rects:
        return None
    h, w = shape[:2]
    s = dpi / 25.4
    mask = np.zeros((h, w), dtype=bool)
    for x, y, rw, rh in rects:
        x0, y0 = max(0, int(np.floor(x * s))), max(0, int(np.floor(y * s)))
        x1, y1 = min(w, int(np.ceil((x + rw) * s))), min(h, int(np.ceil((y + rh) * s)))
        if x1 > x0 and y1 > y0:
            mask[y0:y1, x0:x1] = True
    return mask if mask.any() else None


def mask_ignored(image: np.ndarray, tpl: Template, face: str, dpi: float, fill: int = 255) -> np.ndarray:
    """把忽略区涂成白纸（返回副本；没有忽略区时原样返回）。"""
    mask = ignore_mask(tpl, face, image.shape, dpi)
    if mask is None:
        return image
    out = image.copy()
    out[mask] = fill
    return out


def grid_cells(
    first_center: tuple[float, float],
    n_groups: int,
    n_options: int,
    option_pitch: float,
    group_pitch: float,
    cell_size: tuple[float, float],
    options_horizontal: bool = True,
    block_every: int = 0,
    block_gap: float = 0.0,
) -> list[list[Rect]]:
    """生成 n_groups 组、每组 n_options 个格子。

    options_horizontal=True：同一组的选项横向排列，组（题）纵向排列；False 则相反。
    block_every>0 时，每 block_every 组额外增加 block_gap 的间距（例如每 5 题空一行）。
    返回 cells[group][option] = (x, y, w, h)，以格子中心对称展开。
    """
    cx0, cy0 = first_center
    w, h = cell_size
    cells: list[list[Rect]] = []
    for g in range(n_groups):
        extra = (g // block_every) * block_gap if block_every else 0.0
        row: list[Rect] = []
        for o in range(n_options):
            if options_horizontal:
                cx = cx0 + o * option_pitch
                cy = cy0 + g * group_pitch + extra
            else:
                cx = cx0 + g * group_pitch + extra
                cy = cy0 + o * option_pitch
            row.append((round(cx - w / 2, 3), round(cy - h / 2, 3), w, h))
        cells.append(row)
    return cells
