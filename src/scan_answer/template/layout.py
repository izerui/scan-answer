"""按规则批量生成填涂格坐标（模板编辑器"框整块、填参数"也用这个函数）。"""
from __future__ import annotations

from .model import Rect


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
