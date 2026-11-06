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
from .align import AlignResult, align_features, align_primary, crop_region, effective_dpi, warp_to_template
from .barcode import read_barcode
from .omr import Thresholds, cell_fill, decide, grid_fills

WORK_DPI = 200
SCORE_DPI = 30
MIN_SCORE = 0.35  # 对齐后与空白底图的相关系数下限
MAX_SKEW = 8.0


class PhysicalError(Exception):
    """物理异常：需要现场重扫或作废。code 见 PHYSICAL_CODES。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


PHYSICAL_CODES = {"ALIGN_FAILED", "FACE_MISMATCH", "BLANK_PAGE", "MISSING_FACE", "SKEW_TOO_LARGE"}


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
    barcodes: dict[str, tuple[str, str] | None] = field(default_factory=dict)


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


def place_faces(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float]) -> tuple[dict[str, FacePlacement], bool]:
    """为每个模板面找到对应的输入图和旋转角度。先用主定位方式，全部失败再用特征匹配兜底。"""
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
                    raise PhysicalError("SKEW_TOO_LARGE", f"{f} 面倾斜 {p.align.skew_deg:.1f}°")
            return placements, list(perm) != sorted(perm)
    if not any_aligned:
        raise PhysicalError("ALIGN_FAILED", "所有面都无法定位")
    raise PhysicalError("FACE_MISMATCH", "图像与模板的正反面无法匹配")


def _expand(rect, mm):
    x, y, w, h = rect
    return (x - mm, y - mm, w + 2 * mm, h + 2 * mm)


def analyze_sheet(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float] | None = None) -> SheetAnalysis:
    t = ctx.tpl
    dpis = dpis or [estimate_dpi(g, t.paper) for g in images]
    for g in images:
        if is_blank(g):
            raise PhysicalError("BLANK_PAGE", "检测到空白页")
    placements, swapped = place_faces(ctx, images, dpis)
    work = {f: warp_to_template(oriented(images[p.source], p.rotated), p.align.H, t.paper, ctx.work_dpi)
            for f, p in placements.items()}
    a = SheetAnalysis(placements=placements, swapped=swapped)
    if t.exam_no:
        g = t.exam_no.grid
        a.exam_fills = grid_fills(work[g.face], ctx.work_bases[g.face], g.cells, ctx.work_dpi)
    if t.absent:
        a.absent_fill = cell_fill(work[t.absent.face], ctx.work_bases[t.absent.face], t.absent.rect, ctx.work_dpi)
    for blk in t.objective:
        a.objective_fills.append(grid_fills(work[blk.grid.face], ctx.work_bases[blk.grid.face], blk.grid.cells, ctx.work_dpi))
    for bc in t.barcodes:
        p = placements[bc.face]
        src = oriented(images[p.source], p.rotated)
        hit = read_barcode(crop_region(src, p.align.H, bc.rect, p.dpi))
        if hit is None:
            hit = read_barcode(crop_region(src, p.align.H, _expand(bc.rect, 10), p.dpi))
        a.barcodes[bc.key] = hit
    return a


@dataclass
class SheetDecision:
    exam_no: dict | None
    absent: dict | None
    objective: list[dict]
    barcodes: dict[str, dict]
    consistent: bool | None
    warnings: list[dict]


def decide_sheet(ctx: TemplateContext, a: SheetAnalysis, th: Thresholds) -> SheetDecision:
    t = ctx.tpl
    warnings: list[dict] = []
    exam = None
    if t.exam_no and a.exam_fills is not None:
        labels = t.exam_no.grid.labels
        digits, value = [], ""
        for pos, fills in enumerate(a.exam_fills, start=1):
            ans, st = decide(fills, labels, "single", th)
            ok = st == "ok"
            value += ans if ok else "?"
            digits.append({"pos": pos, "value": ans if ok else None, "status": st})
        statuses = {d["status"] for d in digits}
        status = "ok" if statuses == {"ok"} else ("blank" if statuses == {"blank"} else "partial")
        exam = {"value": value, "status": status, "digits": [d for d in digits if d["status"] != "ok"]}
        if status == "partial":
            bad = "、".join(str(d["pos"]) for d in digits if d["status"] != "ok")
            warnings.append({"code": "EXAM_NO_PARTIAL", "field": "student.exam_no", "msg": f"考号第 {bad} 位未识别"})
        elif status == "blank":
            warnings.append({"code": "EXAM_NO_BLANK", "field": "student.exam_no", "msg": "考号未填涂"})
    absent = None
    if a.absent_fill is not None:
        absent = {"value": bool(a.absent_fill >= th.mark), "fill": round(a.absent_fill, 3)}
        if absent["value"]:
            warnings.append({"code": "ABSENT_MARKED", "field": "student.absent", "msg": "缺考标记已填涂"})
    objective = []
    for blk, fills in zip(t.objective, a.objective_fills):
        for k, row in enumerate(fills):
            no = str(blk.start + k)
            ans, st = decide(row, blk.grid.labels, blk.qtype, th)
            objective.append({"no": no, "type": blk.qtype, "answer": ans, "status": st,
                              "fill": {lab: round(float(v), 3) for lab, v in zip(blk.grid.labels, row)}})
            if st != "ok":
                warnings.append({"code": "OBJ_" + st.upper(), "field": f"objective.{no}"})
    barcodes = {}
    for key, hit in a.barcodes.items():
        barcodes[key] = ({"value": hit[0], "symbology": hit[1], "status": "ok"} if hit
                         else {"value": None, "symbology": None, "status": "failed"})
    consistent = None
    sb = barcodes.get("student_barcode")
    if exam and exam["status"] == "ok" and sb and sb["status"] == "ok":
        consistent = sb["value"] == exam["value"]
        if not consistent:
            warnings.append({"code": "ID_MISMATCH", "msg": "填涂考号与条码不一致"})
    if sb and sb["status"] != "ok":
        warnings.append({"code": "BARCODE_MISSING", "field": "student.barcodes.student_barcode", "msg": "考生条码未识别"})
    for f, p in a.placements.items():
        if p.align.method == "features":
            warnings.append({"code": "ALIGN_FALLBACK", "field": f"scan.faces.{f}", "msg": f"{f} 面使用特征匹配兜底定位"})
    return SheetDecision(exam, absent, objective, barcodes, consistent, warnings)
