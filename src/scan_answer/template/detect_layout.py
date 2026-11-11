"""从空白底图自动识别版面：考号、客观题块、主观题作答区。

思路：选项格里印的字母 / 数字就在格子正中，比框线清楚得多。先找出这些字形，把横竖对齐的连成网格；
同一道题里各格字形不同（A、B、C…），不同题同一位置的字形相同，比较相邻字形就能分出“题目方向”和
“选项方向”，不需要识别文字。题号按版面顺序数，题型一律单选，由人核对。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import pairwise

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
    """b 处字形（中间 2.0 mm 见方）在 a 处 2.6 mm 见方窗口里的最大归一化相关（容许 ±0.3 mm 错位）。"""
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
STRONG_GLYPH = 0.9  # 只有 2 道题的小网格要求的相似度
GROUP_GLYPH = 0.6   # 一组选项各线相似度中位数的下限
TIE_DROP = 0.3      # 选项数打平时，第一条线比其余线低这么多就当题号去掉
EDGE_DROP = 0.9     # 一组选项头尾的线得分比本组中位数低这么多就去掉（题号）


def _vertical_strokes(gray: np.ndarray, s: float) -> np.ndarray:
    """竖笔画图（高 ≥ 1.2 mm 的竖线）：框线、括号。

    扫描件的细框线又淡又断，还可能略微倾斜：阈值放宽一些，先补上竖向的小断口、横向加宽 1 像素，再取竖线。"""
    bw = cv2.adaptiveThreshold(cv2.GaussianBlur(gray, (3, 3), 0), 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 25, 6)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (1, 5)))
    bw = cv2.dilate(bw, cv2.getStructuringElement(cv2.MORPH_RECT, (2, 1)))
    return cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(1.2 * s))))


def _boxed(vmap: np.ndarray, s: float, c: tuple[float, float]) -> bool:
    """字形左右两侧 0.8–3.2 mm 内都有竖笔画（框线或括号）。题号旁边没有。"""
    cx, cy = int(c[0] * s), int(c[1] * s)
    a, b, dy = int(0.8 * s), int(3.2 * s), int(0.5 * s)
    band = vmap[max(0, cy - dy):cy + dy]
    return bool(band[:, max(0, cx - b):max(0, cx - a)].any() and band[:, cx + a:cx + b].any())


def _trim(grp: list[tuple[float, int | None]], score: list[float]) -> list[tuple[float, int | None]]:
    """去掉头尾得分明显低于本组其余线的线（紧挨着选项的题号），以及头尾补出来的线。

    正好 10 条线的是考号的 0–9，没有题号混在里面，不去头尾（扫描件上个别数字行的相似度会偏低）。"""
    grp = list(grp)
    while len(grp) > 2 and len(grp) != 10:
        ref = float(np.median([score[i] for _, i in grp if i is not None]))
        ends = [e for e in (0, -1) if grp[e][1] is not None and score[grp[e][1]] < ref - EDGE_DROP]
        if not ends:
            break
        grp.pop(min(ends, key=lambda e: score[grp[e][1]]))
    while grp and grp[0][1] is None:
        grp.pop(0)
    while grp and grp[-1][1] is None:
        grp.pop()
    return grp


def _option_groups(opt_pos: list[float], score: list[float]) -> list[list[tuple[float, int | None]]]:
    """把选项线按间距分成一题一组：[(位置, 线下标；补出来的线为 None)]。

    同一题的选项等间距；间距对不上的断开（题号、相邻两题之间的空隙）。正好空出一个间距的先补一条线
    （扫描后某行字母没检测到）；补完的组超过 10 个选项、或与同网格其他组选项数不一致时，在补线处拆开。"""
    gaps = np.diff(opt_pos)
    base = float(np.median(gaps))
    groups: list[list[tuple[float, int | None]]] = [[(opt_pos[0], 0)]]
    for i, d in enumerate(gaps, start=1):
        if abs(d - 2 * base) <= 0.1 * base:
            groups[-1].append(((opt_pos[i - 1] + opt_pos[i]) / 2, None))
        elif abs(d - base) > 0.2 * base:
            groups.append([])
        groups[-1].append((opt_pos[i], i))
    groups = [_trim(grp, score) for grp in groups]
    sizes = [len(grp) for grp in groups if len(grp) >= 2]
    common = max(set(sizes), key=sizes.count) if len(sizes) >= 2 else None
    out: list[list[tuple[float, int | None]]] = []
    for grp in groups:
        filled = any(i is None for _, i in grp)
        if filled and (len(grp) > 10 or (common is not None and len(grp) != common)):
            part: list[tuple[float, int | None]] = []
            for item in grp + [(0.0, None)]:
                if item[1] is None:
                    out.append(_trim(part, score) if part else part)
                    part = []
                else:
                    part.append(item)
        else:
            out.append(grp)
    out = [grp for grp in out if grp]
    # 一道题最多 7 个选项（考号 10 个）：8、9 个或 10 个以上的，从两端去掉得分低的线
    for grp in out:
        while len(grp) in (8, 9) or len(grp) > 10:
            first = score[grp[0][1]] if grp[0][1] is not None else -1.0
            last = score[grp[-1][1]] if grp[-1][1] is not None else -1.0
            grp.pop(0 if first <= last else -1)
    # 同一网格里各题选项数应当一样：比多数题多出来的，从两端去掉得分低的线
    sizes = [len(grp) for grp in out if len(grp) >= 2]
    if len(sizes) >= 2:
        common = max(set(sizes), key=sizes.count)
        for grp in out:
            while len(grp) > common:
                first = score[grp[0][1]] if grp[0][1] is not None else -1.0
                last = score[grp[-1][1]] if grp[-1][1] is not None else -1.0
                grp.pop(0 if first <= last else -1)
    return out


def _questions(gray: np.ndarray, vmap: np.ndarray, s: float, g: np.ndarray, lat: _Lattice,
               face: str, horizontal: bool | None = None) -> list[_Question]:
    """从一个网格里分出题目。字形沿某方向相同 → 那是题目排列方向，另一方向是选项。

    horizontal 给定时（与同一面上已认出的同样网格比照），不再用相似度判断方向。"""
    pos = {k: (lat.xs[k[1]], lat.ys[k[0]]) for k in lat.cell}  # 网格交点：单个字形中心在扫描件上会偏
    sim_x = {k: _similarity(gray, s, pos[k], pos[(k[0], k[1] + 1)]) for k in pos if (k[0], k[1] + 1) in pos}
    sim_y = {k: _similarity(gray, s, pos[k], pos[(k[0] + 1, k[1])]) for k in pos if (k[0] + 1, k[1]) in pos}
    if len(sim_x) < 2 or len(sim_y) < 2:
        return []
    mx, my = float(np.median(list(sim_x.values()))), float(np.median(list(sim_y.values())))
    if horizontal is None:
        if abs(mx - my) < MIN_AXIS_GAP or max(mx, my) < SAME_GLYPH:
            return []
        horizontal = my > mx  # 竖向字形相同：题目上下排，选项横排
    axis = 1 if horizontal else 0
    # 选项线候选：沿题目方向字形大致相同的线。得分 = 2 × 相似度 + 两侧有框的比例。相似度最可靠：
    # 题号线各题号不同（只是十位相同），明显偏低；有框比例在淡印、扫描件上不稳，只作辅助
    keep: list[int] = []
    score: list[float] = []
    boxed_frac: list[float] = []
    sims: list[float] = []
    for line in range(len(lat.xs) if horizontal else len(lat.ys)):
        vals = [v for k, v in (sim_y if horizontal else sim_x).items() if k[axis] == line]
        boxed = [_boxed(vmap, s, p) for k, p in pos.items() if k[axis] == line]
        if vals and float(np.median(vals)) >= LINE_GLYPH:
            keep.append(line)
            score.append(2 * float(np.median(vals)) + float(np.mean(boxed)))
            boxed_frac.append(float(np.mean(boxed)))
            sims.append(float(np.median(vals)))
    if len(keep) < 2:
        return []
    opt_pos = [(lat.xs if horizontal else lat.ys)[i] for i in keep]
    q_lines = range(len(lat.ys) if horizontal else len(lat.xs))
    q_pos = lat.ys if horizontal else lat.xs
    groups = _option_groups(opt_pos, score)
    # 选项格两侧有框线或括号；一个框都看不到的多半是成行的文字（扫描后淡框也能看到一部分）
    kept = [i for grp in groups for _, i in grp if i is not None]
    if not kept or np.mean([boxed_frac[i] for i in kept]) < 0.2:
        return []
    base = float(np.median(np.diff(opt_pos)))
    # 格子大小：按两个方向的最小间距估计（印刷框约占格距的七成）。宁小勿大：识别按格内涂黑比例判断，
    # 格子比框大会把没涂到的边也算进去、读数偏低；常见框约 3.3–3.8 × 2.0–2.4 mm，上限取得比它们都小
    q_gaps = np.diff(q_pos)
    q_pitch = float(q_gaps.min()) if len(q_gaps) else base
    # 题目等间距排列（允许每几题多空一点）；汉字行间距忽大忽小，在这里排除
    if len(q_gaps) >= 2 and np.mean(np.abs(q_gaps - np.median(q_gaps)) <= 0.15 * np.median(q_gaps)) < 0.6:
        return []
    if min(base, q_pitch) < 2.5:
        return []
    px, py = (base, q_pitch) if horizontal else (q_pitch, base)
    size = (round(min(0.72 * px, 3.4), 2), round(min(0.68 * py, 2.1), 2))
    out: list[_Question] = []
    for grp in groups:
        if not 2 <= len(grp) <= 10:
            continue
        # 每个选项位置上各题的字都一样（A、A、A…），一组里多数线的相似度都高；成行的文字只有个别标点一致
        if np.median([sims[i] for _, i in grp if i is not None]) < GROUP_GLYPH:
            continue
        real = [keep[i] for _, i in grp if i is not None]
        for q in q_lines:
            present = sum(((q, ln) if horizontal else (ln, q)) in lat.cell for ln in real)
            if present < 0.5 * len(real):
                continue
            centers = [(p, q_pos[q]) if horizontal else (q_pos[q], p) for p, _ in grp]
            out.append(_Question(face, [(float(x), float(y)) for x, y in centers], size, horizontal))
    # 至少 3 道题；只有 2 道题的小网格（被切开的一段）要字形很像才算
    return out if len(out) >= 3 or (len(out) == 2 and max(mx, my) >= STRONG_GLYPH) else []


def _shape(lat: _Lattice) -> tuple[int, int, float, float]:
    """网格的行数、列数和行距、列距（用来认出同一面上一模一样的网格）。"""
    px = float(np.median(np.diff(lat.xs))) if len(lat.xs) > 1 else 0.0
    py = float(np.median(np.diff(lat.ys))) if len(lat.ys) > 1 else 0.0
    return len(lat.ys), len(lat.xs), px, py


def _same_shape(a: tuple[int, int, float, float], b: tuple[int, int, float, float]) -> bool:
    return (a[0] == b[0] and a[1] == b[1] and abs(a[2] - b[2]) <= 0.1 * max(a[2], b[2])
            and abs(a[3] - b[3]) <= 0.1 * max(a[3], b[3]))


class _Evidence:
    """在某一面底图上核对位置：附近有没有字形、两侧有没有框，以及同一选项位置上各题字形像不像。"""

    def __init__(self, g: np.ndarray, vmap: np.ndarray, smooth: np.ndarray, s: float):
        self.g, self.vmap, self.smooth, self.s = g, vmap, smooth, s

    def glyph(self, c: tuple[float, float]) -> tuple[float, float] | None:
        """c 附近 0.7 mm 内最近的字形中心。"""
        d = np.hypot(self.g[:, 0] - c[0], self.g[:, 1] - c[1])
        i = int(d.argmin())
        return (float(self.g[i, 0]), float(self.g[i, 1])) if d[i] < 0.7 else None

    def question(self, centers: list[tuple[float, float]], need: float = 0.75) -> bool:
        hits = sum(self.glyph(c) is not None and _boxed(self.vmap, self.s, c) for c in centers)
        return hits >= need * len(centers)

    def option_score(self, qs: list[_Question], j: int) -> float:
        """各题第 j 个选项这条线的得分，算法同 _questions：2 × 相邻题字形相似度中位数 + 有框比例。"""
        pts = [q.centers[j] if self.glyph(q.centers[j]) else None for q in qs]  # 用网格位置，同 _questions
        sims = [_similarity(self.smooth, self.s, a, b) for a, b in pairwise(pts) if a and b]
        boxed = [_boxed(self.vmap, self.s, q.centers[j]) for q in qs]
        return 2 * (float(np.median(sims)) if sims else 0.0) + float(np.mean(boxed))

    def option_hits(self, qs: list[_Question], centers: list[tuple[float, float]]) -> float:
        return sum(self.glyph(c) is not None for c in centers) / max(1, len(qs))


def _option_pitch(q: _Question) -> float:
    axis = 0 if q.horizontal else 1
    return float(np.median(np.diff([c[axis] for c in q.centers])))


def _harmonize(lattices: list[list[_Question]], ev: _Evidence) -> list[list[_Question]]:
    """同一面上选项方向、间距相同的网格，选项数按多数（按题数加权）统一：多一个的去掉头尾得分低的那条线
    （多半是题号），少一个的在头或尾按间距补一条（扫描件上整行淡印字母没检测到）。相差更多的不动
    （如英语 41–45 题本来就是 7 个选项）。"""
    out = [list(qs) for qs in lattices]
    for i, qs in enumerate(out):
        n = len(qs[0].centers)
        if n == 10:  # 考号另行处理，不参与也不受统一
            continue
        pitch = _option_pitch(qs[0])
        votes: dict[int, int] = {}
        for other in out:
            o = other[0]
            if len(o.centers) != 10 and o.horizontal == qs[0].horizontal \
                    and abs(_option_pitch(o) - pitch) <= 0.15 * pitch:
                votes[len(o.centers)] = votes.get(len(o.centers), 0) + len(other)
        if len(votes) < 2:
            continue
        if n - 1 in votes and votes[n - 1] >= votes[n]:
            # 多数的少一个选项：去掉头尾得分低的那条线（多半是题号）。打平时只看第一条线：题号总在选项前面，
            # 它是全组最低、又比其余线低一截才去掉
            scores = [ev.option_score(qs, j) for j in range(n)]
            if votes[n - 1] > votes[n]:
                drop = 0 if scores[0] <= scores[-1] else n - 1
            elif scores[0] == min(scores) and scores[0] < float(np.median(scores[1:])) - TIE_DROP:
                drop = 0
            else:
                continue
            out[i] = [_Question(q.face, q.centers[:drop] + q.centers[drop + 1:], q.size, q.horizontal) for q in qs]
            continue
        if votes.get(n + 1, 0) <= votes[n]:
            continue
        # 多数的多一个选项：在头或尾按间距补一条，补在有字形的一侧
        dx, dy = (pitch, 0.0) if qs[0].horizontal else (0.0, pitch)
        before = [(q.centers[0][0] - dx, q.centers[0][1] - dy) for q in qs]
        after = [(q.centers[-1][0] + dx, q.centers[-1][1] + dy) for q in qs]
        hb, ha = ev.option_hits(qs, before), ev.option_hits(qs, after)
        if max(hb, ha) < 0.6:
            continue
        out[i] = [_Question(q.face, ([b] + q.centers) if hb > ha else (q.centers + [a]), q.size, q.horizontal)
                  for q, b, a in zip(qs, before, after)]
    return [qs for qs in out if not _stray(qs, out)]


def _stray(qs: list[_Question], lattices: list[list[_Question]]) -> bool:
    """误认的小网格（标题大字、成行文字的碎片）：不到 5 道题，而且同一面上没有选项方向、间距相同的网格，
    或者选项数和它们的多数相差 2 个以上。考号（10 个选项）另行处理。"""
    n = len(qs[0].centers)
    if len(qs) >= 5 or n == 10:
        return False
    pitch = _option_pitch(qs[0])
    votes: dict[int, int] = {}
    for other in lattices:
        o = other[0]
        if other is not qs and len(o.centers) != 10 and o.horizontal == qs[0].horizontal \
                and abs(_option_pitch(o) - pitch) <= 0.15 * pitch:
            votes[len(o.centers)] = votes.get(len(o.centers), 0) + len(other)
    if not votes:
        return True
    return abs(max(votes, key=lambda v: votes[v]) - n) >= 2


def _face_questions(gray: np.ndarray, dpi: float, face: str) -> tuple[list[_Question], _Evidence | None]:
    s = dpi / 25.4
    glyphs = detect_glyphs(gray, dpi)
    if len(glyphs) < 4:
        return [], None
    g = np.array(glyphs)
    vmap = _vertical_strokes(gray, s)
    smooth = cv2.GaussianBlur(gray, (5, 5), 0)  # 浅色、斑驳的印刷字比较前先抹平
    ev = _Evidence(g, vmap, smooth, s)
    lattices = []
    found: list[tuple[_Lattice, list[_Question]]] = []  # 认出来的网格及其题目
    missed: list[_Lattice] = []
    for idx in _clusters(g):
        lat = _lattice(g, idx)
        if lat is None:
            continue
        qs = _questions(smooth, vmap, s, g, lat, face)
        if not qs:
            missed.append(lat)
            continue
        found.append((lat, qs))
        # 一个网格里可能有选项数不同的几组（各组分开统一）
        for n in sorted({len(q.centers) for q in qs}):
            lattices.append([q for q in qs if len(q.centers) == n])
    # 字形太淡、相似度不够没认出来的网格：和已认出的某个网格一模一样（行列数、行距列距都相同）时，
    # 把那个网格的题目平移过来（如 408、政治卡由多块相同的小网格组成），平移后的位置上要真有字形
    for lat in missed:
        twin = next(((t, qs) for t, qs in found if _same_shape(_shape(t), _shape(lat))), None)
        if twin is None:
            continue
        t, qs = twin
        dx = float(np.median(np.subtract(lat.xs, t.xs)))
        dy = float(np.median(np.subtract(lat.ys, t.ys)))
        moved = [_Question(face, [(x + dx, y + dy) for x, y in q.centers], q.size, q.horizontal) for q in qs]
        if np.mean([ev.glyph(c) is not None for q in moved for c in q.centers]) >= 0.6:
            for n in sorted({len(q.centers) for q in moved}):
                lattices.append([q for q in moved if len(q.centers) == n])
    return [q for qs in _harmonize(lattices, ev) for q in qs], ev


def _shifted(q: _Question, axis: int, d: float) -> _Question:
    centers = [(x + d, y) if axis == 0 else (x, y + d) for x, y in q.centers]
    return _Question(q.face, centers, q.size, q.horizontal)


def _fill(block: list[_Question], axis: int, ev: _Evidence | None,
          taken: np.ndarray | None = None) -> list[_Question]:
    """补上题块里漏掉的题：中间正好空出一题的位置，以及末尾按已有间隔还能接上的题（如单独一题的 33 题）。

    taken：同一面上其他题的格子中心，补出来的题不能压在它们上面（相邻题块）。"""
    if ev is None or len(block) < 2:
        return block

    def free(q: _Question) -> bool:
        return taken is None or not len(taken) or all(
            np.min(np.hypot(taken[:, 0] - x, taken[:, 1] - y)) > 1.0 for x, y in q.centers)

    pos = [q.anchor[axis] for q in block]
    gaps = np.diff(pos)
    pitch = float(gaps.min())
    out = [block[0]]
    for q, d in zip(block[1:], gaps):
        if abs(d - 2 * pitch) < 0.15 * pitch:
            mid = _shifted(out[-1], axis, d / 2)
            if free(mid) and ev.question(mid.centers, need=0.4):  # 两头都有题、正好空一个位置，结构上已经很确定
                out.append(mid)
        out.append(q)
    steps = sorted({round(float(d), 1) for d in np.diff([q.anchor[axis] for q in out])})
    for _ in range(50):
        nxt = next((c for c in (_shifted(out[-1], axis, d) for d in steps) if free(c) and ev.question(c.centers)),
                   None)
        if nxt is None:
            break
        out.append(nxt)
    return out


# ── 主观题作答区 ──────────────────────────────────────────────


def _answer_areas(gray: np.ndarray, dpi: float, taken: list[tuple[float, float]]) -> list[Rect]:
    """被长横线、长竖线围起来的大块空白（宽高都 ≥ 40 mm）。

    跳过：连到纸边的（没围住）、灰色底纹的（“阴影部分请勿作答”）、里面有选项格的（客观题、考号面板）、
    去掉长线后还有不少墨迹的（“注意事项”等文字框；作文方格线是长线，会被去掉）。返回内缩 1.5 mm 的矩形。"""
    s = dpi / 25.4
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 31, 10)

    def lines(length_mm: float) -> np.ndarray:
        k = int(length_mm * s)
        h = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (k, 1)))
        v = cv2.morphologyEx(bw, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, k)))
        return cv2.bitwise_or(h, v)

    free = cv2.bitwise_not(cv2.dilate(lines(15), np.ones((5, 5), np.uint8)))
    text = cv2.bitwise_and(bw, cv2.bitwise_not(cv2.dilate(lines(5), np.ones((3, 3), np.uint8))))
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
        if float((text[y:y + hh, x:x + w] > 0).mean()) > 0.02:  # 作答区几乎空白（实测 ≤ 0.6%，文字框 ≥ 5%）
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
    taken = np.array([c for q in qs for c in q.centers])
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
                    ordered.append(_fill(block, axis, ev, taken))
                    block = []
                block.append(q)
            ordered.append(_fill(block, axis, ev, taken))
    return ordered


def _rect(c: tuple[float, float], size: tuple[float, float]) -> Rect:
    return (round(float(c[0]) - size[0] / 2, 2), round(float(c[1]) - size[1] / 2, 2),
            float(size[0]), float(size[1]))


def _group_size(block: list[_Question]) -> int:
    """题块里每几题多空一点（如每 5 题一组）；看不出来时为 0。"""
    if len(block) < 3:
        return 0
    xs, ys = [q.anchor[0] for q in block], [q.anchor[1] for q in block]
    gaps = np.diff(xs if np.ptp(xs) > np.ptp(ys) else ys)  # 沿题目排列的方向
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
    blocks_10: list[list[_Question]] = []
    if exam:
        blocks_10 = [b for face in per_face
                     for b in _order([q for q in per_face[face] if len(q.centers) == 10], evidence[face])]
        best = max(blocks_10, key=len)
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
    result.notes.extend(_review_notes(result, len(blocks_10) if exam else 0))
    if result.exam_no is None:
        result.notes.append("没找到考号区")
    if not result.objective:
        result.notes.append("没找到选择题")
    if not result.subjective:
        result.notes.append("没找到主观题作答区")
    return result


def _review_notes(r: LayoutResult, grids_10: int) -> list[str]:
    """需要人重点核对的地方：选项数与同一面多数题块不同的、只有 2 个选项的、没用上的 0–9 填涂区。"""
    notes = []
    for face in ("front", "back"):
        blocks = [b for b in r.objective if b.grid.face == face]
        counts: dict[int, int] = {}
        for b in blocks:
            counts[len(b.grid.labels)] = counts.get(len(b.grid.labels), 0) + len(b.grid.cells)
        if not counts:
            continue
        common = max(counts, key=lambda n: counts[n])
        odd = [b.name for b in blocks if len(b.grid.labels) != common]
        if odd:
            notes.append(f"第 {'、'.join(odd)} 题的选项数和其他题不同")
    two = [b.name for b in r.objective if len(b.grid.labels) == 2]
    if two:
        notes.append(f"第 {'、'.join(two)} 题只有 2 个选项（判断题请改题型和选项）")
    if grids_10 > 1:
        notes.append(f"另有 {grids_10 - 1} 处 0–9 填涂区没有用上（如考场号，需要时用“填涂字段”画）")
    return notes


def describe_layout(r: LayoutResult) -> str:
    """识别结果的一句话说明，给状态栏用。状态栏太长时从中间省略，所以需要核对的提示放最前面。"""
    parts = []
    if r.exam_no is not None:
        parts.append(f"考号 {len(r.exam_no.grid.cells)} 位")
    if r.objective:
        first = r.objective[0].start
        last = r.objective[-1].start + len(r.objective[-1].grid.cells) - 1
        parts.append(f"选择题 {first}–{last}（默认单选）")
    if r.subjective:
        parts.append(f"主观题作答区 {r.subjective[0].no}–{r.subjective[-1].no}" if len(r.subjective) > 1
                     else f"主观题作答区 {r.subjective[0].no}")
    if not parts:
        return "没识别到考号、选择题或作答区，请用“画框”手动添加"
    found = "自动识别到" + "、".join(parts)
    return f"请核对：{'；'.join(r.notes)}；{found}" if r.notes else found
