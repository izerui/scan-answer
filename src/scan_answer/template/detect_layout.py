"""从空白底图自动识别版面：考号、客观题块、主观题作答区。

思路：选项格里印的字母 / 数字就在格子正中，比框线清楚得多。先找出这些字形，把横竖对齐的连成网格；
同一道题里各格字形不同（A、B、C…），不同题同一位置的字形相同，比较相邻字形就能分出“题目方向”和
“选项方向”，不需要识别文字。题号按版面顺序数，题型一律单选，由人核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

from .model import ExamNoSpec, ObjectiveBlock, OmrGrid, Rect, Region, SubjectiveSpec

LETTERS = "ABCDEFG"


@dataclass
class LayoutResult:
    exam_no: ExamNoSpec | None = None
    objective: list[ObjectiveBlock] = field(default_factory=list)
    subjective: list[SubjectiveSpec] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class _Question:
    face: str
    centers: list[tuple[float, float]]  # 各选项格中心（mm），按选项顺序
    size: tuple[float, float]           # 格子宽高（mm）
    horizontal: bool                    # 选项横排

    @property
    def anchor(self) -> tuple[float, float]:
        return self.centers[0]


# ── 字形检测 ──────────────────────────────────────────────────


def _binarize(gray: np.ndarray) -> np.ndarray:
    return cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 25, 10)


def _components(bw: np.ndarray, s: float, close_mm: float = 0.25) -> list[tuple[float, float, float, float]]:
    """字形大小的连通域中心与宽高（mm）。"""
    k = max(1, int(close_mm * s))
    c = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    n, _, st, _ = cv2.connectedComponentsWithStats(c, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, a = (int(v) for v in st[i])
        wm, hm = w / s, h / s
        if (hm >= 2.0 and wm < 0.4 * hm) or (wm < 0.55 and hm < 1.5):  # [ ] 括号、框线残段
            continue
        if 0.8 <= hm <= 3.0 and 0.3 <= wm <= min(4.5, 2.2 * hm) and a >= 6:
            out.append(((x + w / 2) / s, (y + h / 2) / s, wm, hm))
    return out


def detect_glyphs(gray: np.ndarray, dpi: float) -> list[tuple[float, float, float, float]]:
    """选项格里的字母 / 数字候选：(中心 x, 中心 y, 宽, 高)，单位 mm。

    三遍检测合并：原图；去掉长横线、长竖线后（封闭方框的框线会和字母粘连）；模糊后放宽阈值
    （浅色、斑驳的印刷字连成一块）。大块优先，中心落在已选大块里的碎片丢掉。"""
    s = dpi / 25.4
    bw = _binarize(gray)
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (int(2.0 * s), 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(2.0 * s))))
    lines = cv2.dilate(cv2.bitwise_or(h, v), np.ones((3, 3), np.uint8))
    faint = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (7, 7), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                                  cv2.THRESH_BINARY_INV, 31, 6)
    cand = (_components(bw, s) + _components(cv2.bitwise_and(bw, cv2.bitwise_not(lines)), s)
            + _components(cv2.bitwise_and(faint, cv2.bitwise_not(lines)), s, 0.6))
    cand.sort(key=lambda c: -c[2] * c[3])
    out: list[tuple[float, float, float, float]] = []
    for c in cand:
        if not any(abs(c[0] - o[0]) < max(o[2] / 2, 0.5) and abs(c[1] - o[1]) < max(o[3] / 2, 0.5) for o in out):
            out.append(c)
    return out


# ── 连成网格 ──────────────────────────────────────────────────


def _clusters(g: np.ndarray) -> list[np.ndarray]:
    """把横向或纵向对齐、大小相近的字形连在一起，返回各连通块的下标。"""
    n = len(g)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        dx = np.abs(g[:, 0] - g[i, 0])
        dy = np.abs(g[:, 1] - g[i, 1])
        ratio = g[:, 3] / g[i, 3]
        similar = (ratio > 0.6) & (ratio < 1 / 0.6)
        link = similar & (((dy < 0.45) & (dx >= 3.0) & (dx <= 8.0)) | ((dx < 0.6) & (dy >= 2.5) & (dy <= 8.0)))
        for j in np.nonzero(link)[0]:
            a, b = find(i), find(int(j))
            if a != b:
                parent[a] = b
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [np.array(v) for v in groups.values() if len(v) >= 4]


def _split(values: np.ndarray, gap: float) -> np.ndarray:
    """一维聚类：排序后相邻差 > gap 处断开，返回每个值所属的类号（按位置从小到大）。"""
    order = np.argsort(values)
    labels = np.empty(len(values), int)
    k = 0
    for n, i in enumerate(order):
        if n and values[i] - values[order[n - 1]] > gap:
            k += 1
        labels[i] = k
    return labels


def _merge_close(values: np.ndarray, labels: np.ndarray) -> np.ndarray:
    """相邻两类的中心距离不到常规间距的一半时合成一类（同一列字形偏得多，被拆成了两列）。"""
    while labels.max() >= 2:
        means = np.array([values[labels == k].mean() for k in range(labels.max() + 1)])
        gaps = np.diff(means)
        k = int(gaps.argmin())
        if gaps[k] >= 0.5 * float(np.median(gaps)):
            break
        labels = np.where(labels > k, labels - 1, labels)
    return labels


@dataclass
class _Lattice:
    xs: list[float]               # 各列中心 x
    ys: list[float]               # 各行中心 y
    cell: dict[tuple[int, int], int]  # (行, 列) → 字形下标


def _lattice(g: np.ndarray, idx: np.ndarray) -> _Lattice | None:
    """把一块字形排成行列；反复去掉覆盖不到一半的行、列。不足 2×2 或整体覆盖率低于 60% 时返回 None。"""
    idx = np.asarray(idx)
    while True:
        if len(idx) < 4:
            return None
        cols = _merge_close(g[idx, 0], _split(g[idx, 0], 1.0))
        rows = _merge_close(g[idx, 1], _split(g[idx, 1], 0.8))
        nr, nc = rows.max() + 1, cols.max() + 1
        occ = np.zeros((nr, nc), bool)
        occ[rows, cols] = True
        row_occ, col_occ = occ.mean(axis=1), occ.mean(axis=0)
        worst_r, worst_c = row_occ.argmin(), col_occ.argmin()
        if min(row_occ[worst_r], col_occ[worst_c]) >= 0.5:
            break
        if row_occ[worst_r] <= col_occ[worst_c]:
            idx = idx[rows != worst_r]
        else:
            idx = idx[cols != worst_c]
    if nr < 2 or nc < 2 or occ.mean() < 0.6:
        return None
    xs = [float(np.median(g[idx[cols == c], 0])) for c in range(nc)]
    ys = [float(np.median(g[idx[rows == r], 1])) for r in range(nr)]
    cell: dict[tuple[int, int], int] = {}
    for k, i in enumerate(idx):
        cell.setdefault((int(rows[k]), int(cols[k])), int(i))
    return _Lattice(xs, ys, cell)


# ── 字形相似度 ────────────────────────────────────────────────


def _similarity(gray: np.ndarray, s: float, a: tuple[float, float], b: tuple[float, float]) -> float:
    """b 处字形（中间 2.0 mm 见方）在 a 处 2.6 mm 见方窗口里的最大归一化相关。"""
    r_out, r_in = int(1.3 * s), int(1.0 * s)
    ax, ay, bx, by = (round(v * s) for v in (*a, *b))
    win = gray[ay - r_out:ay + r_out, ax - r_out:ax + r_out]
    tpl = gray[by - r_in:by + r_in, bx - r_in:bx + r_in]
    if win.shape != (2 * r_out, 2 * r_out) or tpl.shape != (2 * r_in, 2 * r_in) or tpl.std() < 1:
        return 0.0
    return float(cv2.matchTemplate(win, tpl, cv2.TM_CCOEFF_NORMED).max())


# ── 网格 → 题目 ───────────────────────────────────────────────

SAME_GLYPH = 0.7   # 相邻字形相关系数达到它算“同一个字”
LINE_GLYPH = 0.3   # 一条选项线上字形相关的最低要求（淡印、斑驳的卡也能过）
MIN_AXIS_GAP = 0.15  # 两个方向的相似度至少差这么多才能分出题目方向


def _vertical_strokes(gray: np.ndarray, s: float) -> np.ndarray:
    """竖笔画图（高 ≥ 1.2 mm 的竖线）：框线、括号。"""
    return cv2.morphologyEx(_binarize(gray), cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(1.2 * s))))


def _boxed(vmap: np.ndarray, s: float, c: tuple[float, float]) -> bool:
    """字形左右两侧 0.8–3.2 mm 内都有竖笔画（框线或括号）。题号旁边没有。"""
    cx, cy = int(c[0] * s), int(c[1] * s)
    a, b, dy = int(0.8 * s), int(3.2 * s), int(0.5 * s)
    band = vmap[max(0, cy - dy):cy + dy]
    return bool(band[:, max(0, cx - b):max(0, cx - a)].any() and band[:, cx + a:cx + b].any())


def _questions(gray: np.ndarray, vmap: np.ndarray, s: float, g: np.ndarray, lat: _Lattice,
               face: str) -> list[_Question]:
    """从一个网格里分出题目。字形沿某方向相同 → 那是题目排列方向，另一方向是选项。"""
    pos = {k: (float(g[i, 0]), float(g[i, 1])) for k, i in lat.cell.items()}
    sim_x = {k: _similarity(gray, s, pos[k], pos[(k[0], k[1] + 1)]) for k in pos if (k[0], k[1] + 1) in pos}
    sim_y = {k: _similarity(gray, s, pos[k], pos[(k[0] + 1, k[1])]) for k in pos if (k[0] + 1, k[1]) in pos}
    if len(sim_x) < 2 or len(sim_y) < 2:
        return []
    mx, my = float(np.median(list(sim_x.values()))), float(np.median(list(sim_y.values())))
    if abs(mx - my) < MIN_AXIS_GAP or max(mx, my) < SAME_GLYPH:
        return []
    horizontal = my > mx  # 竖向字形相同：题目上下排，选项横排
    # 选项线：沿题目方向字形都相同的线；题号列（各行数字不同）在这里被去掉
    n_opt_lines = len(lat.xs) if horizontal else len(lat.ys)
    keep: list[int] = []
    score: dict[int, float] = {}
    for line in range(n_opt_lines):
        axis = 1 if horizontal else 0
        vals = [v for k, v in (sim_y if horizontal else sim_x).items() if k[axis] == line]
        boxed = [_boxed(vmap, s, p) for k, p in pos.items() if k[axis] == line]
        if vals and float(np.median(vals)) >= LINE_GLYPH and np.mean(boxed) >= 0.5:
            keep.append(line)
            score[line] = float(np.mean(boxed)) + float(np.median(vals))
    if len(keep) < 2:
        return []
    opt_pos = [(lat.xs if horizontal else lat.ys)[i] for i in keep]
    q_lines = range(len(lat.ys) if horizontal else len(lat.xs))
    q_pos = lat.ys if horizontal else lat.xs
    # 同一题的选项等间距；间距对不上的断开（题号的十位数、相邻两题之间的空隙）
    gaps = np.diff(opt_pos)
    base = float(np.median(gaps))
    groups: list[list[int]] = [[0]]
    for i, d in enumerate(gaps, start=1):
        if abs(d - base) > 0.15 * base:
            groups.append([])
        groups[-1].append(i)
    # 同一网格里各题选项数应当一样：比多数题多出来的，从两端去掉最不像选项的线（如紧挨着 A 的题号）
    sizes = [len(grp) for grp in groups if len(grp) >= 2]
    if len(sizes) >= 2:
        common = max(set(sizes), key=sizes.count)
        for grp in groups:
            while len(grp) > common:
                grp.pop(0 if score[keep[grp[0]]] <= score[keep[grp[-1]]] else -1)
    # 格子大小：按两个方向的最小间距估计（印刷框约占格距的七成）
    q_gaps = np.diff(q_pos)
    q_pitch = float(q_gaps.min()) if len(q_gaps) else base
    # 题目等间距排列（允许每几题多空一点）；汉字行间距忽大忽小，在这里排除
    if len(q_gaps) >= 2 and np.mean(np.abs(q_gaps - np.median(q_gaps)) <= 0.15 * np.median(q_gaps)) < 0.6:
        return []
    if min(base, q_pitch) < 2.5:
        return []
    px, py = (base, q_pitch) if horizontal else (q_pitch, base)
    size = (round(min(0.72 * px, 4.0), 2), round(min(0.68 * py, 2.6), 2))
    out: list[_Question] = []
    for grp in groups:
        if not 2 <= len(grp) <= 10:
            continue
        lines = [keep[i] for i in grp]
        for q in q_lines:
            present = sum(((q, ln) if horizontal else (ln, q)) in lat.cell for ln in lines)
            if present < 0.5 * len(lines):
                continue
            centers = [(lat.xs[ln], lat.ys[q]) if horizontal else (lat.xs[q], lat.ys[ln]) for ln in lines]
            out.append(_Question(face, centers, size, horizontal))
    return out if len(out) >= 3 else []


class _Evidence:
    """某位置是否真有一个选项格：附近有字形，且左右有框线或括号。"""

    def __init__(self, g: np.ndarray, vmap: np.ndarray, s: float):
        self.g, self.vmap, self.s = g, vmap, s

    def question(self, centers: list[tuple[float, float]], need: float = 0.75) -> bool:
        hits = 0
        for c in centers:
            d = np.hypot(self.g[:, 0] - c[0], self.g[:, 1] - c[1])
            hits += bool(d.min() < 0.7 and _boxed(self.vmap, self.s, c))
        return hits >= need * len(centers)


def _face_questions(gray: np.ndarray, dpi: float, face: str) -> tuple[list[_Question], _Evidence | None]:
    s = dpi / 25.4
    glyphs = detect_glyphs(gray, dpi)
    if len(glyphs) < 4:
        return [], None
    g = np.array(glyphs)
    vmap = _vertical_strokes(gray, s)
    smooth = cv2.GaussianBlur(gray, (5, 5), 0)  # 浅色、斑驳的印刷字比较前先抹平
    out: list[_Question] = []
    for idx in _clusters(g):
        lat = _lattice(g, idx)
        if lat is not None:
            out.extend(_questions(smooth, vmap, s, g, lat, face))
    return out, _Evidence(g, vmap, s)


def _shifted(q: _Question, axis: int, d: float) -> _Question:
    centers = [(x + d, y) if axis == 0 else (x, y + d) for x, y in q.centers]
    return _Question(q.face, centers, q.size, q.horizontal)


def _fill(block: list[_Question], axis: int, ev: _Evidence | None) -> list[_Question]:
    """补上题块里漏掉的题：中间正好空出一题的位置，以及末尾按已有间隔还能接上的题（如单独一题的 33 题）。"""
    if ev is None or len(block) < 2:
        return block
    pos = [q.anchor[axis] for q in block]
    gaps = np.diff(pos)
    pitch = float(gaps.min())
    out = [block[0]]
    for q, d in zip(block[1:], gaps):
        if abs(d - 2 * pitch) < 0.15 * pitch:
            mid = _shifted(out[-1], axis, d / 2)
            if ev.question(mid.centers, need=0.4):  # 两头都有题、正好空一个位置，结构上已经很确定
                out.append(mid)
        out.append(q)
    steps = sorted({round(float(d), 1) for d in np.diff([q.anchor[axis] for q in out])})
    for _ in range(50):
        nxt = next((c for c in (_shifted(out[-1], axis, d) for d in steps) if ev.question(c.centers)), None)
        if nxt is None:
            break
        out.append(nxt)
    return out


# ── 主观题作答区 ──────────────────────────────────────────────


def _answer_areas(gray: np.ndarray, dpi: float, taken: list[tuple[float, float]]) -> list[Rect]:
    """被长横线、长竖线围起来的大块空白（宽高都 ≥ 40 mm）。

    跳过：连到纸边的（没围住）、灰色底纹的（“阴影部分请勿作答”）、里面有选项格的（客观题、考号面板）。
    返回内缩 1.5 mm 的矩形。"""
    s = dpi / 25.4
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 10)
    k = int(15 * s)
    h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
    v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
    free = cv2.bitwise_not(cv2.dilate(cv2.bitwise_or(h, v), np.ones((5, 5), np.uint8)))
    n, _, st, _ = cv2.connectedComponentsWithStats(free, 4)
    height, width = gray.shape
    out: list[Rect] = []
    for i in range(1, n):
        x, y, w, hh, area = (int(v) for v in st[i])
        if x == 0 or y == 0 or x + w >= width or y + hh >= height:
            continue
        if w < 40 * s or hh < 40 * s or area < 0.85 * w * hh:
            continue
        if float(gray[y:y + hh, x:x + w].mean()) < 230:
            continue
        r = (x / s, y / s, w / s, hh / s)
        if any(r[0] <= cx <= r[0] + r[2] and r[1] <= cy <= r[1] + r[3] for cx, cy in taken):
            continue
        out.append((round(r[0] + 1.5, 1), round(r[1] + 1.5, 1), round(r[2] - 3.0, 1), round(r[3] - 3.0, 1)))
    if not out:
        return out
    cols = _split(np.array([r[0] for r in out]), 20.0)  # 按栏从左到右，栏内从上到下
    return [r for _, r in sorted(zip(cols, out), key=lambda t: (t[0], t[1][1]))]


# ── 编号与组装 ────────────────────────────────────────────────


def _column_major(qs: list[_Question]) -> bool:
    """同一列上的题多于同一行上的题 → 先竖着数（每栏从上到下，栏从左到右）。"""
    xs = np.array([q.anchor[0] for q in qs])
    ys = np.array([q.anchor[1] for q in qs])
    per_col = max(int(np.sum(np.abs(xs - x) < 1.0)) for x in xs)
    per_row = max(int(np.sum(np.abs(ys - y) < 0.6)) for y in ys)
    return per_col >= per_row


def _order(qs: list[_Question], ev: _Evidence | None = None) -> list[list[_Question]]:
    """一个面上的题按版面顺序排好，并按“同一栏 / 同一行、选项数和方向相同”切成题块。"""
    if not qs:
        return []
    col_major = _column_major(qs)
    # 先按栏（或行）分组，再在组内排序
    key_line = (lambda q: q.anchor[0]) if col_major else (lambda q: q.anchor[1])
    key_along = (lambda q: q.anchor[1]) if col_major else (lambda q: q.anchor[0])
    tol = 1.0 if col_major else 0.6
    # 栏之间可能上下错开成几排（例如上排 1–20、下排 21–40）：先按另一方向的大间隔分带
    along = np.array([key_along(q) for q in qs])
    bands = _split(along, 12.0) if col_major else np.zeros(len(qs), int)
    ordered: list[list[_Question]] = []
    for b in range(bands.max() + 1):
        band = [q for q, k in zip(qs, bands) if k == b]
        lines = _split(np.array([key_line(q) for q in band]), tol)
        for ln in range(lines.max() + 1):
            line = sorted((q for q, k in zip(band, lines) if k == ln), key=key_along)
            block: list[_Question] = []
            axis = 1 if col_major else 0
            for q in line:
                if block and (len(q.centers) != len(block[-1].centers) or q.horizontal != block[-1].horizontal):
                    ordered.append(_fill(block, axis, ev))
                    block = []
                block.append(q)
            ordered.append(_fill(block, axis, ev))
    return ordered


def _rect(c: tuple[float, float], size: tuple[float, float]) -> Rect:
    return (round(c[0] - size[0] / 2, 2), round(c[1] - size[1] / 2, 2), size[0], size[1])


def _group_size(block: list[_Question]) -> int:
    """题块里每几题多空一点（如每 5 题一组）；看不出来时为 0。"""
    if len(block) < 3:
        return 0
    axis = 1 if block[0].horizontal else 0
    gaps = np.diff([q.anchor[axis] for q in block])
    big = np.nonzero(gaps > 1.25 * float(np.min(gaps)))[0]
    return int(big[0] + 1) if len(big) else 0


def detect_layout(bases: dict[str, np.ndarray], dpi: int) -> LayoutResult:
    """识别各面（先正面后背面）的考号、客观题块和主观题作答区；题号从 1 起按版面顺序连续编号。"""
    result = LayoutResult()
    found = {face: _face_questions(bases[face], dpi, face) for face in ("front", "back") if face in bases}
    per_face = {face: qs for face, (qs, _) in found.items()}
    evidence = {face: ev for face, (_, ev) in found.items()}
    # 考号：10 个选项（0–9）的网格，取题数（位数）最多的一块所在的那组
    exam = [q for qs in per_face.values() for q in qs if len(q.centers) == 10]
    if exam:
        blocks = [b for face in per_face
                  for b in _order([q for q in per_face[face] if len(q.centers) == 10], evidence[face])]
        best = max(blocks, key=len)
        if len(best) >= 4:
            cells = [[_rect(c, q.size) for c in q.centers] for q in best]
            result.exam_no = ExamNoSpec(grid=OmrGrid(face=best[0].face, labels=[str(i) for i in range(10)],
                                                     cells=cells))
    no = 1
    for face, qs in per_face.items():
        for block in _order([q for q in qs if 2 <= len(q.centers) <= 7], evidence[face]):
            n_opt = len(block[0].centers)
            cells = [[_rect(c, q.size) for c in q.centers] for q in block]
            end = no + len(block) - 1
            result.objective.append(ObjectiveBlock(
                name=f"{no}–{end}" if end > no else str(no), qtype="single", start=no,
                grid=OmrGrid(face=face, labels=list(LETTERS[:n_opt]), cells=cells), group=_group_size(block)))
            no = end + 1
    taken: dict[str, list[tuple[float, float]]] = {face: [] for face in bases}
    grids = [b.grid for b in result.objective] + ([result.exam_no.grid] if result.exam_no else [])
    for grid in grids:
        taken[grid.face].extend((x + w / 2, y + h / 2) for row in grid.cells for x, y, w, h in row)
    for face in ("front", "back"):
        if face in bases:
            for rect in _answer_areas(bases[face], dpi, taken[face]):
                result.subjective.append(SubjectiveSpec(no=str(no), qtype="essay", content="zh",
                                                        regions=[Region(face=face, rect=rect)]))
                no += 1
    if result.exam_no is None:
        result.notes.append("没找到考号区")
    if not result.objective:
        result.notes.append("没找到选择题")
    if not result.subjective:
        result.notes.append("没找到主观题作答区")
    return result


def describe_layout(r: LayoutResult) -> str:
    """识别结果的一句话说明，给状态栏用。"""
    parts = []
    if r.exam_no is not None:
        parts.append(f"考号 {len(r.exam_no.grid.cells)} 位")
    if r.objective:
        first = r.objective[0].start
        last = r.objective[-1].start + len(r.objective[-1].grid.cells) - 1
        parts.append(f"选择题 {first}–{last}（默认单选，多选题请改题型）")
    if r.subjective:
        parts.append(f"主观题作答区 {len(r.subjective)} 个（{r.subjective[0].no}–{r.subjective[-1].no}）")
    if not parts:
        return "没识别到考号、选择题或作答区，请用“画框”手动添加"
    return "自动识别到：" + "，".join(parts) + "；请核对题号和题型" + "".join(f"；{n}" for n in r.notes)
