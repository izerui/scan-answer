"""单张答题卡识别：方向与正反面判定 → 全局对齐 → OMR / 条码。

分两步，便于批次自适应阈值：
  analyze_sheet()  只计算涂黑度、条码等"原始量"
  decide_sheet()   用（批次）阈值做判定，生成考号、答案、状态和 warnings
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

import cv2
import numpy as np

from ..template.model import Template
from .align import (AlignResult, align_features, align_primary, effective_dpi, manual_alignment, reference_points,
                    scale_matrix, warp_to_template)
from .barcode_regions import read_barcode_regions
from .omr import Thresholds, decide, decide_idx, sample_cell, sample_grid

WORK_DPI = 200
SCORE_DPI = 30
MIN_SCORE = 0.35  # 对齐后与空白底图的相关系数下限
MAX_SKEW = 8.0
DERIVED_MIN_SCORE = 0.30  # 借用另一面推算定位（校正平移后）与空白底图相关性的下限
DERIVED_SEARCH_MM = 40.0  # 推算后校正平移的搜索范围（正反面传感器位置偏差）
# 标识区（混卡检测）：对齐后的卡面与底图在标识区内做局部相关比较。
# 降到 100 dpi 并轻度模糊，压住扫描噪声、JPEG 压缩和笔画粗细差异；在 ±1.5 mm 内搜索平移取最大相关，
# 容忍印刷套准和定位的轻微误差。阈值按内置模板实测确定（tests/test_mixed_card.py）：
#   同模板（6 个内置模板的底图自身、噪声 σ=25/50、模糊 σ=1.5/3、旋转 1.5°/3° + 平移、JPEG q30、
#     对比度减弱、200 dpi、组合扰动）全部标识区 0.940–0.999；
#   英语一 / 英语二 / 政治互相（版面几乎相同，整页 NCC 区分不开）：卷别字样“（一）/（二）”0.32–0.36，
#     政治副标题 0.39–0.41，右页题头 0.05–0.61。阈值取两者之间的 0.75。
IDENTITY_DPI = 100
IDENTITY_BLUR = 1.0  # 高斯模糊 σ（IDENTITY_DPI 下的像素）
IDENTITY_SEARCH_MM = 1.5
IDENTITY_MIN_SCORE = 0.75
IDENTITY_MIN_STD = 8.0  # 底图标识区内灰度标准差低于此值视为没有印刷内容，无法比较（跳过）


class PhysicalError(Exception):
    """物理异常：需要现场重扫或作废。code 见 PHYSICAL_CODES。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


PHYSICAL_CODES = {"ALIGN_FAILED", "FACE_MISMATCH", "BLANK_PAGE", "MISSING_FACE", "SKEW_TOO_LARGE", "WRONG_CARD"}


class TemplateContext:
    """模板 + 预先缩放好的底图，供多张卡复用。"""

    def __init__(self, tpl: Template, bases: dict[str, np.ndarray], work_dpi: int = WORK_DPI):
        self.tpl = tpl
        self.bases = bases
        self.work_dpi = work_dpi
        self.work_bases = {k: cv2.resize(v, None, fx=work_dpi / tpl.dpi, fy=work_dpi / tpl.dpi,
                                         interpolation=cv2.INTER_AREA) for k, v in bases.items()}
        self.score_bases = {k: cv2.GaussianBlur(cv2.resize(v, None, fx=SCORE_DPI / tpl.dpi, fy=SCORE_DPI / tpl.dpi,
                                                           interpolation=cv2.INTER_AREA), (3, 3), 0)
                            for k, v in bases.items()}
        # 标识区底图小块：(面, 小块)；底图该处没有印刷内容时小块为 None（不比较）
        self.identity_bases: list[tuple[str, np.ndarray | None]] = []
        for r in tpl.identity:
            patch = identity_patch(self.work_bases[r.face], r.rect, 0.0, work_dpi) if r.face in self.work_bases else None
            self.identity_bases.append((r.face, patch if patch is not None and float(patch.std()) >= IDENTITY_MIN_STD
                                        else None))


@dataclass
class FacePlacement:
    source: int  # 来自第几张输入图
    rotated: int  # 0 或 180
    align: AlignResult
    dpi: float


@dataclass
class SheetAnalysis:
    placements: dict[str, FacePlacement]
    swapped: bool
    exam_fills: np.ndarray | None = None
    absent_fill: float | None = None
    objective_fills: list[np.ndarray] = field(default_factory=list)
    barcodes: dict[str, tuple | None] = field(default_factory=dict)
    omr_field_fills: dict[str, np.ndarray] = field(default_factory=dict)
    choose_fills: dict[str, np.ndarray] = field(default_factory=dict)
    sync: dict[str, dict] = field(default_factory=dict)
    sampling: dict[str, dict] = field(default_factory=dict)
    barcode_evidence: dict[str, dict] = field(default_factory=dict)
    evidence_version: int = 1
    identity: str | None = None  # 标识区：partial（部分对不上）/ confirmed（操作员确认后跳过混卡检查）


def estimate_dpi(gray: np.ndarray, paper: tuple[float, float]) -> float:
    return max(gray.shape) / max(paper) * 25.4


def is_blank(gray: np.ndarray) -> bool:
    return float((gray < 128).mean()) < 0.003


def oriented(gray: np.ndarray, rotated: int) -> np.ndarray:
    return gray if rotated == 0 else cv2.rotate(gray, cv2.ROTATE_180)


def _score(ctx: TemplateContext, face: str, gray: np.ndarray, H: np.ndarray) -> float:
    small = cv2.GaussianBlur(warp_to_template(gray, H, ctx.tpl.paper, SCORE_DPI), (3, 3), 0)
    base = ctx.score_bases[face]
    h, w = min(small.shape[0], base.shape[0]), min(small.shape[1], base.shape[1])
    return float(cv2.matchTemplate(small[:h, :w], base[:h, :w], cv2.TM_CCOEFF_NORMED)[0, 0])


def identity_patch(work: np.ndarray, rect: tuple[float, float, float, float], margin_mm: float,
                   work_dpi: float) -> np.ndarray:
    """从模板坐标系下的整页图（work_dpi）裁出 rect 外扩 margin_mm 的区域，降到 IDENTITY_DPI 并轻度模糊。

    超出纸面的部分补白，保证卡面小块不小于底图小块。"""
    s = work_dpi / 25.4
    x, y, w, h = rect
    x0, y0 = (x - margin_mm) * s, (y - margin_mm) * s
    size = (max(1, int(round((w + 2 * margin_mm) * s))), max(1, int(round((h + 2 * margin_mm) * s))))
    M = np.array([[1, 0, -x0], [0, 1, -y0]], dtype=np.float64)
    crop = cv2.warpAffine(work, M, size, flags=cv2.INTER_LINEAR, borderValue=255)
    f = IDENTITY_DPI / work_dpi
    small = cv2.resize(crop, (max(1, round(size[0] * f)), max(1, round(size[1] * f))), interpolation=cv2.INTER_AREA)
    return cv2.GaussianBlur(small, (0, 0), IDENTITY_BLUR).astype(np.float32)


def identity_scores(ctx: TemplateContext, work: dict[str, np.ndarray], faces: set[str]) -> list[float | None]:
    """每个标识区：卡面与底图的局部相关系数（±IDENTITY_SEARCH_MM 平移内取最大）。

    work 为对齐到模板坐标系的整页图（ctx.work_dpi）；只比较 faces 中的面，其余（推算定位的面）
    以及底图该处没有印刷内容的标识区记为 None。"""
    scores: list[float | None] = []
    for region, (face, base) in zip(ctx.tpl.identity, ctx.identity_bases):
        if base is None or face not in faces or face not in work:
            scores.append(None)
            continue
        big = identity_patch(work[face], region.rect, IDENTITY_SEARCH_MM, ctx.work_dpi)
        if big.shape[0] < base.shape[0] or big.shape[1] < base.shape[1]:
            scores.append(None)
            continue
        if float(big.std()) < 1e-3:
            scores.append(0.0)  # 卡面该处一片空白：与有印刷内容的底图必然不符
            continue
        res = cv2.matchTemplate(big, base, cv2.TM_CCOEFF_NORMED)
        scores.append(float(np.nan_to_num(res, nan=0.0).max()))
    return scores


def check_identity(ctx: TemplateContext, work: dict[str, np.ndarray], faces: set[str]) -> str | None:
    """可比较的标识区全部与底图不符时抛 WRONG_CARD（混入了其他科目或版本的答题卡）。

    只有部分对不上时多半是学生在标识区里写画，不判混卡，返回 "partial" 供结果里加警告；都对上返回 None。
    实测混卡时各标识区都远低于阈值（最高 0.61），所以“全部不符”不会放过混卡。"""
    scores = [sc for sc in identity_scores(ctx, work, faces) if sc is not None]
    bad = [sc for sc in scores if sc < IDENTITY_MIN_SCORE]
    if bad and len(bad) == len(scores):
        raise PhysicalError("WRONG_CARD", "卡面标识与模板不符，可能混入了其他科目或版本的答题卡")
    return "partial" if bad else None


def _normalize(res: AlignResult, rot: int, shape: tuple[int, int]) -> tuple[AlignResult, int]:
    """定位块近似中心对称时，H 本身可能已包含 180° 旋转。

    此时改为"图像先旋转 180° 再对齐"的等价表示：rot += 180，H = H @ R180，
    使 rotated 字段如实反映图像方向，倾斜角归一到 (-90°, 90°]。
    """
    if abs(res.skew_deg) <= 90:
        return res, rot
    h, w = shape
    r180 = np.array([[-1, 0, w - 1], [0, -1, h - 1], [0, 0, 1]], dtype=np.float64)
    res.H = res.H @ r180
    res.skew_deg -= 180 if res.skew_deg > 0 else -180
    return res, (rot + 180) % 360


FACE_NAMES = {"front": "正面", "back": "背面"}


def _place_with_manual(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float],
                       manual: dict) -> tuple[dict[str, FacePlacement], bool]:
    """人工定位的面直接按点击的参照点求变换；其余面在剩下的图里自动定位。

    manual: {面: {"image": 输入图序号, "points": [[x, y] | None, ...]}}，points 与 reference_points 一一对应。
    人工定位的面不受 MAX_SKEW 限制（操作员已确认），但仍要与空白底图对得上，防止点错位置或顺序。
    """
    faces = list(ctx.tpl.faces)
    if len(images) != len(faces):
        raise PhysicalError("MISSING_FACE", f"需要 {len(faces)} 面图像，实际 {len(images)} 面")
    placements: dict[str, FacePlacement] = {}
    for face, m in manual.items():
        i = int(m["image"])
        if face not in ctx.tpl.faces or not 0 <= i < len(images) or i in {p.source for p in placements.values()}:
            raise ValueError(f"人工定位参数无效：{face} → 第 {i + 1} 张图")
        name = FACE_NAMES.get(face, face)
        res = manual_alignment(ctx.tpl.faces[face].align, m["points"])
        if not res.ok:
            raise PhysicalError("ALIGN_FAILED", f"{name}人工定位失败：{res.reason}")
        res, rot = _normalize(res, 0, images[i].shape)
        if _score(ctx, face, oriented(images[i], rot), res.H) < MIN_SCORE:
            raise PhysicalError("ALIGN_FAILED", f"{name}人工定位后与模板对不上，请检查点击的位置和顺序")
        placements[face] = FacePlacement(i, rot, res, effective_dpi(res.H))
    rest = [i for i in range(len(images)) if i not in {p.source for p in placements.values()}]
    for face in (f for f in faces if f not in placements):
        name = FACE_NAMES.get(face, face)
        best = None
        for i in rest:
            for rot in (0, 180):
                g = oriented(images[i], rot)
                # 与全自动流程一致：主定位失败或内容对不上（分数不足）时再用特征匹配
                for use_fallback in (False, True):
                    res = (align_features(g, ctx.bases[face], ctx.tpl.dpi, dpis[i]) if use_fallback
                           else align_primary(g, ctx.tpl.faces[face].align, dpis[i]))
                    if not res.ok:
                        continue
                    sc = _score(ctx, face, g, res.H)
                    if best is None or sc > best[0]:
                        res, eff_rot = _normalize(res, rot, g.shape)
                        best = (sc, FacePlacement(i, eff_rot, res, effective_dpi(res.H)))
                    if sc >= MIN_SCORE:
                        break
        if best is None or best[0] < MIN_SCORE:
            raise PhysicalError("ALIGN_FAILED", f"{name}自动定位失败，请把{name}也手动定位")
        if abs(best[1].align.skew_deg) > MAX_SKEW:
            raise PhysicalError("SKEW_TOO_LARGE", f"{name}倾斜 {best[1].align.skew_deg:.1f}°，请把{name}也手动定位")
        placements[face] = best[1]
        rest.remove(best[1].source)
    order = [placements[f].source for f in faces]
    return placements, order != sorted(order)


def place_faces(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float],
                manual: dict | None = None) -> tuple[dict[str, FacePlacement], bool]:
    """为每个模板面找到对应的输入图和旋转角度。先用主定位方式，全部失败再用特征匹配兜底。

    manual 非空时改由操作员点击的参照点定位这些面（见 _place_with_manual）。"""
    if manual:
        return _place_with_manual(ctx, images, dpis, manual)
    faces = list(ctx.tpl.faces)
    if len(images) != len(faces):
        raise PhysicalError("MISSING_FACE", f"需要 {len(faces)} 面图像，实际 {len(images)} 面")
    best: dict[tuple[int, str], tuple[float, FacePlacement]] = {}
    any_aligned = False
    for use_fallback in (False, True):
        for i, img in enumerate(images):
            for rot in (0, 180):
                g = oriented(img, rot)
                for face in faces:
                    if best.get((i, face), (0.0,))[0] >= 0.6:
                        continue
                    spec = ctx.tpl.faces[face].align
                    res = (align_features(g, ctx.bases[face], ctx.tpl.dpi, dpis[i]) if use_fallback
                           else align_primary(g, spec, dpis[i]))
                    if not res.ok:
                        continue
                    any_aligned = True
                    sc = _score(ctx, face, g, res.H)
                    if sc > best.get((i, face), (-1.0,))[0]:
                        res, eff_rot = _normalize(res, rot, g.shape)
                        best[(i, face)] = (sc, FacePlacement(i, eff_rot, res, effective_dpi(res.H)))
        choice = None
        for perm in permutations(range(len(images)), len(faces)):
            pairs = [best.get((i, f)) for i, f in zip(perm, faces)]
            if any(p is None or p[0] < MIN_SCORE for p in pairs):
                continue
            total = sum(p[0] for p in pairs)
            if choice is None or total > choice[0]:
                choice = (total, perm, pairs)
        if choice:
            _, perm, pairs = choice
            placements = {f: p[1] for f, p in zip(faces, pairs)}
            for f, p in placements.items():
                if abs(p.align.skew_deg) > MAX_SKEW:
                    raise PhysicalError("SKEW_TOO_LARGE", f"{FACE_NAMES.get(f, f)}倾斜 {p.align.skew_deg:.1f}°，"
                                                          "可手动定位或重扫")
            return placements, list(perm) != sorted(perm)
    derived = _derive_missing_face(ctx, images, faces, best)
    if derived is not None:
        return derived
    if not any_aligned:
        raise PhysicalError("ALIGN_FAILED", "所有面都无法定位")
    raise PhysicalError("FACE_MISMATCH", "图像与模板的正反面无法匹配")


def _mirror(shape: tuple[int, int], paper: tuple[float, float], axis: str) -> tuple[np.ndarray, np.ndarray]:
    """返回 (像素镜像矩阵, mm 镜像矩阵)。axis="x" 为左右翻页，"y" 为上下翻页。"""
    h, w = shape
    pw, ph = paper
    if axis == "x":
        fp = np.array([[-1, 0, w - 1], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
        fm = np.array([[-1, 0, pw], [0, 1, 0], [0, 0, 1]], dtype=np.float64)
    else:
        fp = np.array([[1, 0, 0], [0, -1, h - 1], [0, 0, 1]], dtype=np.float64)
        fm = np.array([[1, 0, 0], [0, -1, ph], [0, 0, 1]], dtype=np.float64)
    return fp, fm


def _derive_missing_face(ctx: TemplateContext, images: list[np.ndarray], faces: list[str],
                         best: dict[tuple[int, str], tuple[float, FacePlacement]]
                         ) -> tuple[dict[str, FacePlacement], bool] | None:
    """两面模板只有一面能定位时，按双面扫描的镜像关系由这一面推算另一面（只用于切图）。

    同一张纸的正反面在两张扫描图里互为镜像：H_B = F_mm · H_A · F_px。
    对另一张图尝试 0°/180° 与左右/上下翻页 4 种组合，取与空白底图相关性最高的一种。
    """
    if len(faces) != 2 or len(images) != 2:
        return None
    paper = ctx.tpl.paper
    choice = None
    for (i, face), (sc, known) in best.items():
        if sc < MIN_SCORE or abs(known.align.skew_deg) > MAX_SKEW:
            continue
        other = faces[1] if face == faces[0] else faces[0]
        j = 1 - i
        for rot in (0, 180):
            g = oriented(images[j], rot)
            for axis in ("x", "y"):
                fp, fm = _mirror(g.shape[:2], paper, axis)
                try:
                    H = _refine_translation(ctx, other, g, fm @ known.align.H @ fp)
                    dsc = _score(ctx, other, g, H)
                except cv2.error:
                    continue
                if dsc < DERIVED_MIN_SCORE:
                    continue
                total = sc + dsc
                if choice is None or total > choice[0]:
                    res = AlignResult(True, "derived", H=H, skew_deg=-known.align.skew_deg, residual_mm=0.0)
                    choice = (total, face, known, other, FacePlacement(j, rot, res, effective_dpi(H)))
    if choice is None:
        return None
    _, face, known, other, derived = choice
    placements = {face: known, other: derived}
    perm = [placements[f].source for f in faces]
    return placements, perm != sorted(perm)


def _refine_translation(ctx: TemplateContext, face: str, gray: np.ndarray, H: np.ndarray) -> np.ndarray:
    """在 ±DERIVED_SEARCH_MM 内搜索平移，校正镜像推算的位置偏差（低分辨率整页相关匹配）。"""
    m = DERIVED_SEARCH_MM
    pw, ph = ctx.tpl.paper
    shift = np.array([[1, 0, m], [0, 1, m], [0, 0, 1]], dtype=np.float64)
    big = cv2.GaussianBlur(warp_to_template(gray, shift @ H, (pw + 2 * m, ph + 2 * m), SCORE_DPI), (3, 3), 0)
    base = ctx.score_bases[face]
    if big.shape[0] < base.shape[0] or big.shape[1] < base.shape[1]:
        return H
    res = cv2.matchTemplate(big, base, cv2.TM_CCOEFF_NORMED)
    _, _, _, loc = cv2.minMaxLoc(res)
    s = SCORE_DPI / 25.4
    dx, dy = loc[0] / s - m, loc[1] / s - m
    return np.array([[1, 0, -dx], [0, 1, -dy], [0, 0, 1]], dtype=np.float64) @ H


def derived_faces(a: "SheetAnalysis") -> set[str]:
    """由另一面推算定位的面：只用于切图，不识别填涂。"""
    return {f for f, p in a.placements.items() if p.align.method == "derived"}


def measure_sync(work: np.ndarray, marks: list[tuple[float, float, float, float]],
                  dpi: float, radius_mm: float = 1.5) -> list[tuple[float, float] | None]:
    """测量同步头：返回每个标记相对模板中心的偏移 (dx, dy) mm，找不到返回 None。"""
    from .align import px_per_mm
    s = px_per_mm(dpi)
    r_px = int(round(radius_mm * s))
    results: list[tuple[float, float] | None] = []
    for mx, my, mw, mh in marks:
        # 模板中心像素
        cx = int(round((mx + mw / 2) * s))
        cy = int(round((my + mh / 2) * s))
        # 搜索窗口
        y0 = max(0, cy - r_px)
        y1 = min(work.shape[0], cy + r_px)
        x0 = max(0, cx - r_px)
        x1 = min(work.shape[1], cx + r_px)
        patch = work[y0:y1, x0:x1].astype(np.float32)
        if patch.size == 0:
            results.append(None)
            continue
        lo, hi = float(patch.min()), float(patch.max())
        if hi - lo < 60:
            results.append(None)
            continue
        threshold = (lo + hi) / 2
        dark = patch <= threshold
        # 面积检查：暗像素面积 < 同步头面积 40% 时视为无效
        mark_area_px = mw * s * mh * s
        if dark.sum() < 0.40 * mark_area_px:
            results.append(None)
            continue
        # 暗像素质心
        ys, xs = np.where(dark)
        centroid_x = float(xs.mean()) + x0
        centroid_y = float(ys.mean()) + y0
        dx_mm = (centroid_x - cx) / s
        dy_mm = (centroid_y - cy) / s
        results.append((dx_mm, dy_mm))
    return results


def _expand(rect, mm):
    x, y, w, h = rect
    return (x - mm, y - mm, w + 2 * mm, h + 2 * mm)


def analyze_sheet(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float] | None = None,
                  manual: dict | None = None, confirm_identity: bool = False) -> SheetAnalysis:
    t = ctx.tpl
    dpis = dpis or [estimate_dpi(g, t.paper) for g in images]
    # 墨量少只是“可能空白”：印刷很淡的卡面（如 408 背面不作答时）也低于阈值，但能按定位块对上模板。
    # 所以只有定位不上、或只能由另一面推算时，才把墨量少的页判为空白页。
    try:
        placements, swapped = place_faces(ctx, images, dpis, manual)
    except PhysicalError as e:
        if e.code in ("ALIGN_FAILED", "FACE_MISMATCH", "MISSING_FACE") and any(is_blank(g) for g in images):
            raise PhysicalError("BLANK_PAGE", "检测到空白页") from e
        raise
    if any(p.align.method == "derived" and is_blank(images[p.source]) for p in placements.values()):
        raise PhysicalError("BLANK_PAGE", "检测到空白页")
    work = {f: warp_to_template(oriented(images[p.source], p.rotated), p.align.H, t.paper, ctx.work_dpi)
            for f, p in placements.items()}
    coverage = {
        f: cv2.warpPerspective(
            np.full(oriented(images[p.source], p.rotated).shape[:2], 255, np.uint8),
            scale_matrix(ctx.work_dpi) @ p.align.H,
            (work[f].shape[1], work[f].shape[0]), flags=cv2.INTER_LINEAR, borderValue=0,
        ) == 255 for f, p in placements.items()
    }
    a = SheetAnalysis(placements=placements, swapped=swapped, evidence_version=2)
    dead = derived_faces(a)
    # 混卡检测：只检查实际定位成功的面（推算定位的面位置不够准，不参与比较）。
    # confirm_identity：操作员核对原图后确认不是混卡（例如学生在标识区里大面积写画），跳过检查并留警告
    a.identity = "confirmed" if confirm_identity else check_identity(ctx, work, set(placements) - dead)

    def fills_of(key: str, face: str, cells) -> np.ndarray:
        if face in dead:
            a.sampling[key] = {"face": face, "shift_mm": [0., 0.],
                               "reasons": [["derived_alignment"] * len(row) for row in cells]}
            return np.zeros((len(cells), 0), dtype=np.float32)  # 推算面：不识别，空数组不参与阈值拟合
        fills, metadata = sample_grid(work[face], ctx.work_bases[face], cells, ctx.work_dpi,
                                      coverage=coverage[face])
        a.sampling[key] = {"face": face, **metadata}
        return fills

    if t.exam_no:
        g = t.exam_no.grid
        a.exam_fills = fills_of("exam_no", g.face, g.cells)
    if t.absent:
        face = t.absent.face
        value, reason = (float("nan"), "derived_alignment") if face in dead else sample_cell(
            work[face], ctx.work_bases[face], t.absent.rect, ctx.work_dpi, coverage=coverage[face])
        a.absent_fill = value
        a.sampling["absent"] = {"face": face, "shift_mm": [0., 0.], "reasons": [[reason]]}
    for index, blk in enumerate(t.objective):
        a.objective_fills.append(fills_of(f"objective/{index}", blk.grid.face, blk.grid.cells))
    _read_barcodes(ctx, a, images)
    # 自定义填涂字段
    for of in t.omr_fields:
        a.omr_field_fills[of.key] = fills_of(f"omr_field/{of.key}", of.grid.face, of.grid.cells)
    # 选做组
    for co in t.choose_one:
        a.choose_fills[co.key] = fills_of(f"choose/{co.key}", co.face, [co.cells]).ravel()
    # 同步头
    for ss in t.sync:
        if ss.face in dead:
            continue
        offsets = measure_sync(work[ss.face], ss.marks, ctx.work_dpi)
        found = sum(1 for o in offsets if o is not None)
        if found > 0:
            dists = [max(abs(o[0]), abs(o[1])) for o in offsets if o is not None]
            max_off = round(max(dists), 2)
        else:
            max_off = None
        a.sync[ss.face] = {"found": found, "total": len(ss.marks), "max_offset_mm": max_off}
    return a


def _read_barcodes(ctx: TemplateContext, a: SheetAnalysis, images: list[np.ndarray]) -> None:
    sources = {f: oriented(images[p.source], p.rotated) for f, p in a.placements.items()}
    a.barcodes, a.barcode_evidence = read_barcode_regions(
        ctx.tpl.barcodes, sources, {f: p.align.H for f, p in a.placements.items()},
        {f: p.dpi for f, p in a.placements.items()})


@dataclass
class SheetDecision:
    exam_no: dict | None
    absent: dict | None
    objective: list[dict]
    barcodes: dict[str, dict]
    consistent: bool | None
    warnings: list[dict]
    omr_fields: dict[str, dict] = field(default_factory=dict)
    choose_one: list[dict] = field(default_factory=list)


def decide_sheet(ctx: TemplateContext, a: SheetAnalysis, th: Thresholds) -> SheetDecision:
    t = ctx.tpl
    warnings: list[dict] = []
    for key, metadata in a.sampling.items():
        if any(reason is not None for row in metadata.get("reasons", []) for reason in row):
            warnings.append({"code": "SAMPLE_INVALID", "field": key,
                             "msg": "填涂采样区域不完整或无法定位，不能按未填涂处理"})
    dead = derived_faces(a)

    def grid_values(grid, values, key):
        expected = (len(grid.cells), len(grid.labels))
        try:
            values = np.asarray(values, dtype=np.float64)
        except (TypeError, ValueError):
            values = np.empty(0)
        if values.shape != expected:
            warnings.append({"code": "SAMPLE_INVALID", "field": key,
                             "msg": "保存的采样数量与模板不一致，需要重新识别"})
            return np.full(expected, np.nan)
        return values

    exam = None
    if t.exam_no and t.exam_no.grid.face in dead:
        exam = {"value": "", "status": "failed", "digits": []}
    elif t.exam_no:
        labels = t.exam_no.grid.labels
        digits, value = [], ""
        for pos, fills in enumerate(grid_values(t.exam_no.grid, a.exam_fills, "exam_no"), start=1):
            ans, st = decide(fills, labels, "single", th)
            ok = st == "ok"
            value += ans if ok else "?"
            digits.append({"pos": pos, "value": ans if ok else None, "status": st})
        statuses = {d["status"] for d in digits}
        status = ("failed" if "failed" in statuses else
                  "ok" if statuses == {"ok"} else "blank" if statuses == {"blank"} else "partial")
        exam = {"value": value, "status": status, "digits": [d for d in digits if d["status"] != "ok"]}
        if status in ("partial", "failed"):
            bad = "、".join(str(d["pos"]) for d in digits if d["status"] != "ok")
            warnings.append({"code": "EXAM_NO_PARTIAL", "field": "student.exam_no", "msg": f"考号第 {bad} 位未识别"})
        elif status == "blank":
            warnings.append({"code": "EXAM_NO_BLANK", "field": "student.exam_no", "msg": "考号未填涂"})
    absent = None
    if t.absent and t.absent.face in dead:
        absent = {"value": None, "fill": None, "status": "failed"}
    elif t.absent and (a.absent_fill is None or not np.isfinite(a.absent_fill)):
        absent = {"value": None, "fill": None, "status": "failed"}
    elif a.absent_fill is not None:
        absent = {"value": bool(a.absent_fill >= th.mark), "fill": round(a.absent_fill, 3)}
        if absent["value"]:
            warnings.append({"code": "ABSENT_MARKED", "field": "student.absent", "msg": "缺考标记已填涂"})
    objective = []
    for index, blk in enumerate(t.objective):
        if blk.grid.face in dead:
            objective.extend({"no": str(blk.start + k), "type": blk.qtype, "answer": "", "status": "failed",
                              "fill": {}} for k in range(len(blk.grid.cells)))
            continue
        raw = a.objective_fills[index] if len(a.objective_fills) == len(t.objective) else None
        fills = grid_values(blk.grid, raw, f"objective/{index}")
        for k, row in enumerate(fills):
            no = str(blk.start + k)
            ans, st = decide(row, blk.grid.labels, blk.qtype, th)
            objective.append({"no": no, "type": blk.qtype, "answer": ans, "status": st,
                              "fill": {lab: round(float(v), 3) if np.isfinite(v) else None
                                       for lab, v in zip(blk.grid.labels, row)}})
            if st != "ok":
                warnings.append({"code": "OBJ_" + st.upper(), "field": f"objective.{no}"})
    barcodes = {}
    for key, hit in a.barcodes.items():
        located = hit[2] if hit and len(hit) > 2 else "region"
        barcodes[key] = ({"value": hit[0], "symbology": hit[1], "status": "ok", "located": located} if hit
                         else {"value": None, "symbology": None, "status": "failed", "located": None})
        if key in a.barcode_evidence:
            barcodes[key].update(a.barcode_evidence[key])
        if barcodes[key]["status"] == "ambiguous":
            warnings.append({"code": "BARCODE_AMBIGUOUS", "field": f"student.barcodes.{key}",
                             "msg": "条码存在多个候选或位置归属冲突，请核对候选图片"})
        if hit and located in ("expanded", "page"):
            warnings.append({"code": "BARCODE_RELOCATED", "field": f"student.barcodes.{key}",
                             "msg": "条码在区域外找到（贴歪或贴出框）"})
    consistent = None
    sb = barcodes.get("student_barcode")
    if exam and exam["status"] == "ok" and sb and sb["status"] == "ok":
        consistent = sb["value"] == exam["value"]
        if not consistent:
            warnings.append({"code": "ID_MISMATCH", "msg": "填涂考号与条码不一致"})
    if sb and sb["status"] != "ok":
        warnings.append({"code": "BARCODE_MISSING", "field": "student.barcodes.student_barcode", "msg": "考生条码未识别"})
    # 自定义填涂字段
    omr_fields: dict[str, dict] = {}
    for of in t.omr_fields:
        fills = a.omr_field_fills.get(of.key)
        if of.grid.face in dead:
            omr_fields[of.key] = {"label": of.label, "value": "", "status": "failed", "digits": []}
            continue
        fills = grid_values(of.grid, fills, f"omr_field/{of.key}")
        labels = of.grid.labels
        digits, value = [], ""
        for pos, row_fills in enumerate(fills, start=1):
            ans, st = decide(row_fills, labels, "single", th)
            ok = st == "ok"
            value += ans if ok else "?"
            digits.append({"pos": pos, "value": ans if ok else None, "status": st})
        statuses = {d["status"] for d in digits}
        status = ("failed" if "failed" in statuses else
                  "ok" if statuses == {"ok"} else "blank" if statuses == {"blank"} else "partial")
        omr_fields[of.key] = {"label": of.label, "value": value, "status": status,
                               "digits": [d for d in digits if d["status"] != "ok"]}
        if status in ("partial", "failed"):
            warnings.append({"code": "OMR_FIELD_PARTIAL", "field": f"student.omr_fields.{of.key}",
                             "msg": f"{of.label}有位未识别"})
        elif status == "blank":
            warnings.append({"code": "OMR_FIELD_BLANK", "field": f"student.omr_fields.{of.key}",
                             "msg": f"{of.label}未填涂"})
    # 选做组
    choose_one_out: list[dict] = []
    for co in t.choose_one:
        fills = a.choose_fills.get(co.key)
        if co.face in dead:
            choose_one_out.append({"key": co.key, "label": co.label, "questions": co.questions,
                                   "chosen": [], "status": "failed", "fill": {}})
            continue
        if fills is None or np.asarray(fills).shape != (len(co.questions),):
            fills = np.full(len(co.questions), np.nan)
            warnings.append({"code": "SAMPLE_INVALID", "field": f"choose/{co.key}",
                             "msg": "保存的选做采样数量与模板不一致，需要重新识别"})
        idxs, st = decide_idx(fills, "single", th)
        chosen = [co.questions[i] for i in idxs]
        fill_dict = {q: round(float(fills[i]), 3) if np.isfinite(fills[i]) else None
                     for i, q in enumerate(co.questions)}
        choose_one_out.append({"key": co.key, "label": co.label, "questions": co.questions,
                                "chosen": chosen, "status": st, "fill": fill_dict})
        if st == "blank":
            warnings.append({"code": "CHOICE_BLANK", "field": f"choose_one.{co.key}"})
        elif st == "multiple":
            warnings.append({"code": "CHOICE_MULTIPLE", "field": f"choose_one.{co.key}"})
        elif st == "uncertain":
            warnings.append({"code": "CHOICE_UNCERTAIN", "field": f"choose_one.{co.key}"})
    for f, p in a.placements.items():
        if p.align.method == "features":
            warnings.append({"code": "ALIGN_FALLBACK", "field": f"scan.faces.{f}", "msg": f"{FACE_NAMES.get(f, f)}使用特征匹配兜底定位"})
        elif p.align.method == "manual":
            used = len(reference_points(t.faces[f].align)) - len(p.align.missing) if f in t.faces else 0
            extra = "；只点了 3 个参照点，无法自检点击误差，请重点核对" if used == 3 else ""
            warnings.append({"code": "ALIGN_MANUAL", "field": f"scan.faces.{f}",
                             "msg": f"{FACE_NAMES.get(f, f)}由操作员手动定位，请核对识别结果{extra}"})
        elif p.align.missing:
            name = FACE_NAMES.get(f, f)
            nums = "、".join(str(k + 1) for k in p.align.missing)
            warnings.append({"code": "ANCHOR_MISSING", "field": f"scan.faces.{f}",
                             "msg": f"{name}定位块 {nums} 未找到（污损或印出纸边），已用其余定位块定位，请核对"})
        elif p.align.method == "derived":
            name = {"front": "正面", "back": "背面"}.get(f, f)
            warnings.append({"code": "ALIGN_DERIVED", "field": f"scan.faces.{f}",
                             "msg": f"{name}定位失败，按另一面位置推算切图；该面的填涂项未识别"})
    if a.identity == "partial":
        warnings.append({"code": "IDENTITY_PARTIAL", "field": "scan.identity",
                         "msg": "部分标识区与模板不符（可能有书写遮挡），请核对是否混入了其他科目的答题卡"})
    elif a.identity == "confirmed":
        warnings.append({"code": "IDENTITY_CONFIRMED", "field": "scan.identity",
                         "msg": "卡面标识与模板不符，操作员确认不是混卡后按本模板识别，请核对"})
    if a.swapped:
        # 顺序配对时中间缺一页，后面每份都会“正反面颠倒”并串到下一份答卷上：只标记，由人核对
        warnings.append({"code": "FACE_SWAPPED", "field": "scan.swapped",
                         "msg": "正反面顺序与导入顺序相反（第 1 张图是背面）：个别出现请核对是否串卷，"
                                "整批都这样多半是纸放反了"})
    return SheetDecision(exam, absent, objective, barcodes, consistent, warnings,
                         omr_fields=omr_fields, choose_one=choose_one_out)
