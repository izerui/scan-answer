"""可编辑模板文档：TemplateDoc 包装 Template + 底图，提供区域增删改查、撤销重做、保存。"""
from __future__ import annotations

import copy
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cv2
import numpy as np

from .auto import (ImportedBase, SnapResult, auto_anchors, auto_frames,
                   auto_sync, grid_in_rect, snap_grid_search)
from .io import (load_template, save_template, template_from_dict,
                 template_to_dict)
from .model import (AlignSpec, BarcodeSpec, ChooseOneSpec, ExamNoSpec, Face,
                    FieldSpec, ObjectiveBlock, OmrFieldSpec, OmrGrid, Rect,
                    Region, SubjectiveSpec, SyncSpec, Template)
from .validate import check_alignment, validate_template

KINDS = ("anchor", "frame", "sync", "exam_no", "omr_field", "barcode", "absent", "field",
         "objective", "subjective", "choose_one", "ignore")
KIND_LABELS = {
    "anchor": "定位块", "frame": "定位外框", "sync": "同步头", "exam_no": "考号",
    "omr_field": "填涂字段", "barcode": "条码", "absent": "缺考标记", "field": "信息栏",
    "objective": "客观题", "subjective": "主观题", "choose_one": "选做组", "ignore": "忽略区",
}
KIND_COLOR = {
    "anchor": "loc", "frame": "loc", "sync": "loc", "exam_no": "id",
    "omr_field": "id", "barcode": "bar", "absent": "info", "field": "info",
    "objective": "obj", "subjective": "sub", "choose_one": "sub", "ignore": "info",
}


@dataclass(frozen=True)
class RegionRef:
    kind: str
    index: int = 0
    sub: int = 0


def _grid_bbox(cells: list[list[Rect]]) -> Rect:
    """计算网格的外包矩形。"""
    xs, ys, xes, yes = [], [], [], []
    for row in cells:
        for x, y, w, h in row:
            xs.append(x)
            ys.append(y)
            xes.append(x + w)
            yes.append(y + h)
    x0, y0 = min(xs), min(ys)
    return (x0, y0, max(xes) - x0, max(yes) - y0)


def _affine_scale_cells(cells: list[list[Rect]], old_bbox: Rect, new_bbox: Rect) -> list[list[Rect]]:
    """将格子按旧外包框到新外包框做仿射缩放平移。"""
    ox, oy, ow, oh = old_bbox
    nx, ny, nw, nh = new_bbox
    sx = nw / ow if ow > 0 else 1.0
    sy = nh / oh if oh > 0 else 1.0
    result: list[list[Rect]] = []
    for row in cells:
        new_row: list[Rect] = []
        for cx, cy, cw, ch in row:
            rx = nx + (cx - ox) * sx
            ry = ny + (cy - oy) * sy
            rw = cw * sx
            rh = ch * sy
            new_row.append((round(rx, 3), round(ry, 3), round(rw, 3), round(rh, 3)))
        result.append(new_row)
    return result


def _safe_filename(name: str) -> str:
    """模板名转安全文件名。"""
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "", name).strip().rstrip(" .")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³]",
                    s.split(".")[0].rstrip().upper()):
        s = "_" + s
    return s or "template"


def _infer_horizontal(cells: list[list[Rect]]) -> bool:
    """从现有格子推断 options_horizontal：同一组选项的排列方向。

    若 cells[0][0] → cells[0][1] 的 x 差 > y 差则为横排。"""
    if len(cells) < 1 or len(cells[0]) < 2:
        return True  # 默认横排
    c0 = cells[0][0]
    c1 = cells[0][1]
    dx = abs((c1[0] + c1[2] / 2) - (c0[0] + c0[2] / 2))
    dy = abs((c1[1] + c1[3] / 2) - (c0[1] + c0[3] / 2))
    return dx >= dy


def _anchor_frame_map(t: Template, kind: str) -> list[tuple[str, int]]:
    """按 t.faces 顺序，返回 (face_name, local_index) 列表，全局索引 = 列表位置。"""
    attr = "anchors" if kind == "anchor" else "frames"
    method = "anchors" if kind == "anchor" else "frames"
    result: list[tuple[str, int]] = []
    for fname, face in t.faces.items():
        if face.align.method == method:
            for i in range(len(getattr(face.align, attr))):
                result.append((fname, i))
    return result


def _resolve_anchor_frame(t: Template, ref: "RegionRef") -> tuple[str, int]:
    """将全局 anchor/frame index 解析为 (face_name, local_index)。"""
    mapping = _anchor_frame_map(t, ref.kind)
    if 0 <= ref.index < len(mapping):
        return mapping[ref.index]
    raise IndexError(f"anchor/frame 全局索引 {ref.index} 超出范围（共 {len(mapping)} 个）")


class TemplateDoc:
    """可编辑模板文档。"""

    def __init__(self, t: Template, bases: dict[str, np.ndarray], directory: Path | None):
        self.template: Template = t
        self.bases: dict[str, np.ndarray] = bases
        self.directory: Path | None = directory
        self.dirty: bool = False
        self.revision: int = 0
        self._saved_version: int = t.version

    def _changed(self) -> None:
        self.dirty = True
        self.revision += 1

    @classmethod
    def load(cls, directory: str | Path) -> "TemplateDoc":
        d = Path(directory)
        t, bases = load_template(d)
        return cls(t, bases, d)

    @classmethod
    def new(cls, name: str, faces: dict[str, ImportedBase]) -> "TemplateDoc":
        """从导入的底图创建新模板。"""
        cls.validate_imported_faces(faces)
        face_dict: dict[str, Face] = {}
        bases: dict[str, np.ndarray] = {}
        first = next(iter(faces.values()))
        paper = first.paper
        height, width = first.image.shape[:2]
        dpi = 300

        for fname, ib in faces.items():
            image = ib.image
            if image.shape[:2] != (height, width):
                image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
            bases[fname] = image

            # 自动检测定位方式
            anchors = auto_anchors(image, dpi, paper)
            if anchors is not None:
                align = AlignSpec(method="anchors", anchors=anchors)
            else:
                frames = auto_frames(image, dpi, paper)
                if frames:
                    align = AlignSpec(method="frames", frames=frames)
                else:
                    align = AlignSpec(method="anchors")  # 空列表，保存时由校验拦下

            face_dict[fname] = Face(image=f"{fname}.png", align=align)

        t = Template(
            schema_version="1.0", name=name, version=0,
            paper=paper, dpi=dpi, faces=face_dict,
            exam_no=None, absent=None,
            barcodes=[], fields=[], objective=[], subjective=[],
            ignore=[], sync=[], omr_fields=[], choose_one=[],
        )

        # auto_sync 对每个面
        for fname, image in bases.items():
            syncs = auto_sync(image, dpi)
            for s in syncs:
                s.face = fname
            t.sync.extend(syncs)

        doc = cls(t, bases, None)
        doc.dirty = True
        return doc

    @staticmethod
    def validate_imported_faces(faces: dict[str, ImportedBase]) -> None:
        """校验 300 DPI 导入底图；失败抛 ValueError，不修改输入。

        各面宽高分别允许 1 mm 误差，new() 再统一到第一面的像素尺寸。
        """
        if not faces or any(name not in ("front", "back") for name in faces):
            raise ValueError("至少需要一个有效面（front/back）")
        first = next(iter(faces.values()))
        for name, ib in faces.items():
            if (len(ib.paper) != 2 or
                    any(not math.isfinite(v) or v <= 0 for v in ib.paper)):
                raise ValueError(f"{name}：纸张尺寸必须为正数")
            short, long = sorted(ib.paper)
            if short > 298 or long > 421:
                raise ValueError(f"{name}：纸张超过 A3（短边 297 mm、长边 420 mm）")
            if (not isinstance(ib.image, np.ndarray) or ib.image.ndim != 2
                    or not ib.image.size):
                raise ValueError(f"{name}：缺少有效灰度底图")
            if any(abs(a - b) > 1 + 1e-9 for a, b in zip(ib.paper, first.paper)):
                raise ValueError(f"{name}：正背面尺寸或方向不一致（允许 1 mm 误差）")
            # 像素取整可额外相差 1 px；方向或 DPI 错误不能被缩放掩盖。
            pixel_mm = (ib.image.shape[1] * 25.4 / 300,
                        ib.image.shape[0] * 25.4 / 300)
            if any(abs(a - b) > 1 + 25.4 / 300
                   for a, b in zip(pixel_mm, ib.paper)):
                raise ValueError(f"{name}：底图尺寸或方向与 300 DPI 纸张尺寸不一致")

    def set_alignment_method(self, face: str, method: Literal["anchors", "frames"]) -> None:
        """切换定位方式，保留两种方式已有的矩形。"""
        if face not in self.template.faces:
            raise ValueError(f"不存在的面：{face}")
        if method not in ("anchors", "frames"):
            raise ValueError(f"不支持的定位方式：{method}")
        align = self.template.faces[face].align
        if align.method != method:
            align.method = method
            self._changed()

    # ── 区域列表 ──────────────────────────────────────────────

    def regions(self, face: str) -> list[RegionRef]:
        """返回指定面的所有区域引用，按 KINDS 顺序。"""
        t = self.template
        refs: list[RegionRef] = []

        # anchor（全局索引）
        for gi, (fname, _li) in enumerate(_anchor_frame_map(t, "anchor")):
            if fname == face:
                refs.append(RegionRef("anchor", gi))

        # frame（全局索引）
        for gi, (fname, _li) in enumerate(_anchor_frame_map(t, "frame")):
            if fname == face:
                refs.append(RegionRef("frame", gi))

        # sync
        for si, sync in enumerate(t.sync):
            if sync.face == face:
                for mi in range(len(sync.marks)):
                    refs.append(RegionRef("sync", si, mi))

        # exam_no
        if t.exam_no and t.exam_no.grid.face == face:
            refs.append(RegionRef("exam_no", 0))

        # omr_field
        for i, of in enumerate(t.omr_fields):
            if of.grid.face == face:
                refs.append(RegionRef("omr_field", i))

        # barcode
        for i, bc in enumerate(t.barcodes):
            if bc.face == face:
                refs.append(RegionRef("barcode", i))

        # absent
        if t.absent and t.absent.face == face:
            refs.append(RegionRef("absent", 0))

        # field
        for i, f in enumerate(t.fields):
            if f.face == face:
                refs.append(RegionRef("field", i))

        # objective
        for i, blk in enumerate(t.objective):
            if blk.grid.face == face:
                refs.append(RegionRef("objective", i))

        # subjective（每个区域一个 ref）
        for i, s in enumerate(t.subjective):
            for ri, reg in enumerate(s.regions):
                if reg.face == face:
                    refs.append(RegionRef("subjective", i, ri))

        # choose_one
        for i, ch in enumerate(t.choose_one):
            if ch.face == face:
                refs.append(RegionRef("choose_one", i))

        # ignore
        for i, ig in enumerate(t.ignore):
            if ig.face == face:
                refs.append(RegionRef("ignore", i))

        return refs

    def rect(self, ref: RegionRef) -> Rect:
        """获取区域矩形。网格类返回外包框。"""
        t = self.template
        if ref.kind == "anchor":
            fname, li = _resolve_anchor_frame(t, ref)
            return t.faces[fname].align.anchors[li]
        if ref.kind == "frame":
            fname, li = _resolve_anchor_frame(t, ref)
            return t.faces[fname].align.frames[li]
        if ref.kind == "sync":
            return t.sync[ref.index].marks[ref.sub]
        if ref.kind == "exam_no":
            return _grid_bbox(t.exam_no.grid.cells)
        if ref.kind == "omr_field":
            return _grid_bbox(t.omr_fields[ref.index].grid.cells)
        if ref.kind == "barcode":
            return t.barcodes[ref.index].rect
        if ref.kind == "absent":
            return t.absent.rect
        if ref.kind == "field":
            return t.fields[ref.index].rect
        if ref.kind == "objective":
            return _grid_bbox(t.objective[ref.index].grid.cells)
        if ref.kind == "subjective":
            return t.subjective[ref.index].regions[ref.sub].rect
        if ref.kind == "choose_one":
            cells = t.choose_one[ref.index].cells
            x0 = min(c[0] for c in cells)
            y0 = min(c[1] for c in cells)
            x1 = max(c[0] + c[2] for c in cells)
            y1 = max(c[1] + c[3] for c in cells)
            return (x0, y0, x1 - x0, y1 - y0)
        if ref.kind == "ignore":
            return t.ignore[ref.index].rect
        raise ValueError(f"未知区域类型: {ref.kind}")

    def face_of(self, ref: RegionRef) -> str:
        """获取区域所在面。"""
        t = self.template
        if ref.kind == "anchor":
            fname, _li = _resolve_anchor_frame(t, ref)
            return fname
        if ref.kind == "frame":
            fname, _li = _resolve_anchor_frame(t, ref)
            return fname
        if ref.kind == "sync":
            return t.sync[ref.index].face
        if ref.kind == "exam_no":
            return t.exam_no.grid.face
        if ref.kind == "omr_field":
            return t.omr_fields[ref.index].grid.face
        if ref.kind == "barcode":
            return t.barcodes[ref.index].face
        if ref.kind == "absent":
            return t.absent.face
        if ref.kind == "field":
            return t.fields[ref.index].face
        if ref.kind == "objective":
            return t.objective[ref.index].grid.face
        if ref.kind == "subjective":
            return t.subjective[ref.index].regions[ref.sub].face
        if ref.kind == "choose_one":
            return t.choose_one[ref.index].face
        if ref.kind == "ignore":
            return t.ignore[ref.index].face
        raise ValueError(f"未知区域类型: {ref.kind}")

    def title(self, ref: RegionRef) -> str:
        """区域标签，用于树和画布。"""
        t = self.template
        if ref.kind == "anchor":
            return f"定位块 {ref.index + 1}"
        if ref.kind == "frame":
            return f"外框 {ref.index + 1}"
        if ref.kind == "sync":
            return f"同步头 {ref.index + 1} · {ref.sub + 1}"
        if ref.kind == "exam_no":
            n = len(t.exam_no.grid.cells) if t.exam_no else 0
            return f"考号 {n} 位"
        if ref.kind == "omr_field":
            of = t.omr_fields[ref.index]
            return of.label
        if ref.kind == "barcode":
            return f"条码 {t.barcodes[ref.index].key}"
        if ref.kind == "absent":
            return "缺考标记"
        if ref.kind == "field":
            return f"信息栏 {t.fields[ref.index].label}"
        if ref.kind == "objective":
            blk = t.objective[ref.index]
            end = blk.start + len(blk.grid.cells) - 1
            return f"{blk.start}–{end}"
        if ref.kind == "subjective":
            s = t.subjective[ref.index]
            n_reg = len(s.regions)
            name = f"{s.no} ({s.sub})" if s.sub else str(s.no)
            if n_reg > 1:
                return f"{name} · {ref.sub + 1}"
            return name
        if ref.kind == "choose_one":
            return t.choose_one[ref.index].label
        if ref.kind == "ignore":
            return f"忽略区 {ref.index + 1}"
        return ref.kind

    # ── 修改操作 ──────────────────────────────────────────────

    def set_rect(self, ref: RegionRef, rect: Rect) -> None:
        """设置区域矩形。网格类：所有格按外包框的变化做仿射缩放平移。"""
        if self.rect(ref) == rect:
            return
        t = self.template
        if ref.kind == "anchor":
            fname, li = _resolve_anchor_frame(t, ref)
            t.faces[fname].align.anchors[li] = rect
        elif ref.kind == "frame":
            fname, li = _resolve_anchor_frame(t, ref)
            t.faces[fname].align.frames[li] = rect
        elif ref.kind == "sync":
            t.sync[ref.index].marks[ref.sub] = rect
        elif ref.kind == "exam_no":
            old_bbox = _grid_bbox(t.exam_no.grid.cells)
            t.exam_no.grid.cells = _affine_scale_cells(t.exam_no.grid.cells, old_bbox, rect)
        elif ref.kind == "omr_field":
            of = t.omr_fields[ref.index]
            old_bbox = _grid_bbox(of.grid.cells)
            of.grid.cells = _affine_scale_cells(of.grid.cells, old_bbox, rect)
        elif ref.kind == "barcode":
            t.barcodes[ref.index].rect = rect
        elif ref.kind == "absent":
            t.absent.rect = rect
        elif ref.kind == "field":
            t.fields[ref.index].rect = rect
        elif ref.kind == "objective":
            blk = t.objective[ref.index]
            old_bbox = _grid_bbox(blk.grid.cells)
            blk.grid.cells = _affine_scale_cells(blk.grid.cells, old_bbox, rect)
        elif ref.kind == "subjective":
            t.subjective[ref.index].regions[ref.sub].rect = rect
        elif ref.kind == "choose_one":
            ch = t.choose_one[ref.index]
            old_cells = ch.cells
            x0 = min(c[0] for c in old_cells)
            y0 = min(c[1] for c in old_cells)
            x1 = max(c[0] + c[2] for c in old_cells)
            y1 = max(c[1] + c[3] for c in old_cells)
            old_bbox = (x0, y0, x1 - x0, y1 - y0)
            # 仿射缩放
            nx, ny, nw, nh = rect
            sx = nw / (x1 - x0) if (x1 - x0) > 0 else 1.0
            sy = nh / (y1 - y0) if (y1 - y0) > 0 else 1.0
            ch.cells = [
                (round(nx + (c[0] - x0) * sx, 3), round(ny + (c[1] - y0) * sy, 3),
                 round(c[2] * sx, 3), round(c[3] * sy, 3))
                for c in old_cells
            ]
        elif ref.kind == "ignore":
            t.ignore[ref.index].rect = rect
        else:
            raise ValueError(f"未知区域类型: {ref.kind}")
        self._changed()

    def add(self, kind: str, face: str, rect: Rect) -> RegionRef:
        """新增区域，返回 RegionRef。"""
        t = self.template
        if face not in t.faces:
            raise ValueError(f"不存在的面：{face}")
        if kind == "anchor":
            t.faces[face].align.method = "anchors"
            t.faces[face].align.anchors.append(rect)
            # 全局索引 = 追加后的全局映射中最后一个属于该面的
            gi = _anchor_frame_map(t, "anchor").index(
                (face, len(t.faces[face].align.anchors) - 1))
            ref = RegionRef("anchor", gi)
        elif kind == "frame":
            t.faces[face].align.method = "frames"
            t.faces[face].align.frames.append(rect)
            gi = _anchor_frame_map(t, "frame").index(
                (face, len(t.faces[face].align.frames) - 1))
            ref = RegionRef("frame", gi)
        elif kind == "sync":
            # 新增一个只有一个标记的同步头组
            t.sync.append(SyncSpec(face=face, direction="vertical", marks=[rect]))
            ref = RegionRef("sync", len(t.sync) - 1, 0)
        elif kind == "exam_no":
            n_digits = 10
            cells = grid_in_rect(rect, n_digits, 10, True, cell_ratio=0.62)
            labels = [str(i) for i in range(10)]
            t.exam_no = ExamNoSpec(grid=OmrGrid(face=face, labels=labels, cells=cells))
            ref = RegionRef("exam_no", 0)
        elif kind == "omr_field":
            # 自动生成 key
            existing_keys = {of.key for of in t.omr_fields}
            n = 1
            while f"field{n}" in existing_keys:
                n += 1
            key = f"field{n}"
            cells = grid_in_rect(rect, 1, 10, True, cell_ratio=0.62)
            labels = [str(i) for i in range(10)]
            t.omr_fields.append(OmrFieldSpec(key=key, label=f"填涂字段 {n}",
                                              grid=OmrGrid(face=face, labels=labels, cells=cells)))
            ref = RegionRef("omr_field", len(t.omr_fields) - 1)
        elif kind == "barcode":
            existing_keys = {b.key for b in t.barcodes}
            n = 1
            while f"barcode{n}" in existing_keys:
                n += 1
            t.barcodes.append(BarcodeSpec(key=f"barcode{n}", face=face, rect=rect))
            ref = RegionRef("barcode", len(t.barcodes) - 1)
        elif kind == "absent":
            t.absent = Region(face=face, rect=rect)
            ref = RegionRef("absent", 0)
        elif kind == "field":
            existing_keys = {f.key for f in t.fields}
            n = 1
            while f"field{n}" in existing_keys:
                n += 1
            t.fields.append(FieldSpec(key=f"field{n}", label=f"信息栏 {n}", face=face, rect=rect))
            ref = RegionRef("field", len(t.fields) - 1)
        elif kind == "objective":
            # 起始题号 = 现有最大题号 + 1
            max_no = 0
            for blk in t.objective:
                end = blk.start + len(blk.grid.cells) - 1
                max_no = max(max_no, end)
            for s in t.subjective:
                try:
                    max_no = max(max_no, int(s.no))
                except ValueError:
                    pass
            start = max_no + 1
            n_q = 5
            labels = ["A", "B", "C", "D"]
            cells = grid_in_rect(rect, n_q, len(labels), True, block_every=5, gap_ratio=0.5, cell_ratio=0.62)
            grid = OmrGrid(face=face, labels=labels, cells=cells)
            name = f"{start}–{start + n_q - 1}"
            t.objective.append(ObjectiveBlock(name=name, qtype="single", start=start, grid=grid, group=5))
            ref = RegionRef("objective", len(t.objective) - 1)
        elif kind == "subjective":
            max_no = 0
            for blk in t.objective:
                end = blk.start + len(blk.grid.cells) - 1
                max_no = max(max_no, end)
            for s in t.subjective:
                try:
                    max_no = max(max_no, int(s.no))
                except ValueError:
                    pass
            no = str(max_no + 1)
            t.subjective.append(SubjectiveSpec(no=no, qtype="essay", content="zh",
                                                regions=[Region(face=face, rect=rect)]))
            ref = RegionRef("subjective", len(t.subjective) - 1, 0)
        elif kind == "choose_one":
            # 题号取最后两道主观题
            subj_nos = [s.key for s in t.subjective]
            questions = subj_nos[-2:] if len(subj_nos) >= 2 else subj_nos[:]
            if not questions:
                questions = ["1", "2"]
            n = 1
            existing_keys = {c.key for c in t.choose_one}
            while f"choose{n}" in existing_keys:
                n += 1
            # 每个题号一个格子，等宽排列在 rect 内
            w_each = rect[2] / len(questions) if questions else rect[2]
            cells_list = [
                (round(rect[0] + i * w_each, 3), round(rect[1], 3),
                 round(w_each, 3), round(rect[3], 3))
                for i in range(len(questions))
            ]
            t.choose_one.append(ChooseOneSpec(key=f"choose{n}", label=f"选做组 {n}",
                                               face=face, questions=questions, cells=cells_list))
            ref = RegionRef("choose_one", len(t.choose_one) - 1)
        elif kind == "ignore":
            t.ignore.append(Region(face=face, rect=rect))
            ref = RegionRef("ignore", len(t.ignore) - 1)
        else:
            raise ValueError(f"未知区域类型: {kind}")
        self._changed()
        return ref

    def remove(self, refs: list[RegionRef]) -> None:
        """删除区域。"""
        t = self.template
        refs = list(dict.fromkeys(refs))
        removed_keys: set[str] = set()
        removed_choices = {r.index for r in refs if r.kind == "choose_one"}
        for ref in refs:
            if ref.kind == "objective" and 0 <= ref.index < len(t.objective):
                block = t.objective[ref.index]
                removed_keys.update(str(block.start + i) for i in range(len(block.grid.cells)))
        for index, subject in enumerate(t.subjective):
            removed_regions = {r.sub for r in refs if r.kind == "subjective" and r.index == index}
            if set(range(len(subject.regions))) <= removed_regions:
                removed_keys.add(subject.key)
        for index, choice in enumerate(t.choose_one):
            if index not in removed_choices and removed_keys.intersection(choice.questions):
                raise ValueError(f"题目仍被选做组 {choice.key} 引用，请先调整选做组")
        before = self.snapshot()
        # 按种类分组，然后按 index 逆序删除以避免索引偏移
        by_kind: dict[str, list[RegionRef]] = {}
        for ref in refs:
            by_kind.setdefault(ref.kind, []).append(ref)

        for kind, kind_refs in by_kind.items():
            if kind == "anchor":
                # 全局索引 → (face, local)，按 local 逆序分面删除
                removals: dict[str, list[int]] = {}
                for r in kind_refs:
                    fname, li = _resolve_anchor_frame(t, r)
                    removals.setdefault(fname, []).append(li)
                for fname, lis in removals.items():
                    for li in sorted(set(lis), reverse=True):
                        if li < len(t.faces[fname].align.anchors):
                            t.faces[fname].align.anchors.pop(li)
            elif kind == "frame":
                removals_f: dict[str, list[int]] = {}
                for r in kind_refs:
                    fname, li = _resolve_anchor_frame(t, r)
                    removals_f.setdefault(fname, []).append(li)
                for fname, lis in removals_f.items():
                    for li in sorted(set(lis), reverse=True):
                        if li < len(t.faces[fname].align.frames):
                            t.faces[fname].align.frames.pop(li)
            elif kind == "sync":
                # 同步头：删除标记，空组则删组
                for r in sorted(kind_refs, key=lambda r: (r.index, r.sub), reverse=True):
                    if r.index < len(t.sync):
                        sync = t.sync[r.index]
                        if r.sub < len(sync.marks):
                            sync.marks.pop(r.sub)
                        if not sync.marks:
                            t.sync.pop(r.index)
            elif kind == "exam_no":
                t.exam_no = None
            elif kind == "omr_field":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.omr_fields):
                        t.omr_fields.pop(i)
            elif kind == "barcode":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.barcodes):
                        t.barcodes.pop(i)
            elif kind == "absent":
                t.absent = None
            elif kind == "field":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.fields):
                        t.fields.pop(i)
            elif kind == "objective":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.objective):
                        t.objective.pop(i)
            elif kind == "subjective":
                # 删除区域；无区域的主观题删掉整个
                for r in sorted(kind_refs, key=lambda r: (r.index, r.sub), reverse=True):
                    if r.index < len(t.subjective):
                        s = t.subjective[r.index]
                        if r.sub < len(s.regions):
                            s.regions.pop(r.sub)
                        if not s.regions:
                            t.subjective.pop(r.index)
            elif kind == "choose_one":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.choose_one):
                        t.choose_one.pop(i)
            elif kind == "ignore":
                indices = sorted({r.index for r in kind_refs}, reverse=True)
                for i in indices:
                    if i < len(t.ignore):
                        t.ignore.pop(i)

        if self.snapshot() != before:
            self._changed()

    def params(self, ref: RegionRef) -> dict:
        """获取区域参数（供属性面板使用）。"""
        t = self.template
        if ref.kind == "objective":
            blk = t.objective[ref.index]
            return {
                "name": blk.name,
                "start": blk.start,
                "n_questions": len(blk.grid.cells),
                "n_options": len(blk.grid.labels),
                "labels": blk.grid.labels[:],
                "qtype": blk.qtype,
                "options_horizontal": _infer_horizontal(blk.grid.cells),
                "group": blk.group,
            }
        if ref.kind == "exam_no":
            return {
                "n_digits": len(t.exam_no.grid.cells),
                "options_horizontal": _infer_horizontal(t.exam_no.grid.cells),
            }
        if ref.kind == "omr_field":
            of = t.omr_fields[ref.index]
            return {
                "key": of.key,
                "label": of.label,
                "n_digits": len(of.grid.cells),
                "labels": of.grid.labels[:],
                "options_horizontal": _infer_horizontal(of.grid.cells),
            }
        if ref.kind == "subjective":
            s = t.subjective[ref.index]
            return {"no": s.no, "sub": s.sub, "qtype": s.qtype, "content": s.content}
        if ref.kind == "choose_one":
            ch = t.choose_one[ref.index]
            return {"key": ch.key, "label": ch.label, "questions": ch.questions[:]}
        if ref.kind == "field":
            f = t.fields[ref.index]
            return {"key": f.key, "label": f.label}
        if ref.kind == "barcode":
            return {"key": t.barcodes[ref.index].key}
        return {}

    def set_params(self, ref: RegionRef, p: dict) -> None:
        """事务式设置参数及关联引用；失败时内容、dirty 和 revision 不变。"""
        target = self._parameter_target(self.template, ref)
        if target is None:
            return
        candidate = copy.deepcopy(self.template)
        self._set_params(candidate, ref, copy.deepcopy(p))
        if candidate == self.template:
            return
        updated = self._parameter_target(candidate, ref)
        # 保留题块/字段的对象身份，兼容属性面板及已有调用者持有的引用。
        target.__dict__.update(updated.__dict__)
        for old, new in zip(self.template.choose_one, candidate.choose_one):
            if old.questions != new.questions:
                old.questions = new.questions
        self._changed()

    @staticmethod
    def _parameter_target(t: Template, ref: RegionRef):
        collections = {
            "objective": t.objective, "omr_field": t.omr_fields,
            "subjective": t.subjective, "choose_one": t.choose_one,
            "field": t.fields, "barcode": t.barcodes,
        }
        if ref.kind == "exam_no":
            return t.exam_no
        items = collections.get(ref.kind)
        if items is None:
            return None
        if not 0 <= ref.index < len(items):
            raise IndexError("区域索引超出范围")
        return items[ref.index]

    @staticmethod
    def _positive_int(value, label: str, minimum: int = 1) -> int:
        if type(value) is not int or value < minimum:
            raise ValueError(f"{label}必须为不小于 {minimum} 的整数")
        return value

    @staticmethod
    def _key(value, label: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label}不能为空")
        return value.strip()

    @staticmethod
    def _update_question_refs(t: Template, mapping: dict[str, str], removed: set[str]) -> None:
        for choice in t.choose_one:
            if removed.intersection(choice.questions):
                raise ValueError(f"题目仍被选做组 {choice.key} 引用，请先调整选做组")
            choice.questions = [mapping.get(key, key) for key in choice.questions]

    def _set_params(self, t: Template, ref: RegionRef, p: dict) -> None:
        """在独立候选模板上更新；只在布局参数变化时重新生成网格。"""
        if ref.kind in ("field", "barcode", "omr_field", "choose_one") and "key" in p:
            key = self._key(p["key"], "键")
            attr = {"field": "fields", "barcode": "barcodes",
                    "omr_field": "omr_fields", "choose_one": "choose_one"}[ref.kind]
            if any(item.key == key for i, item in enumerate(getattr(t, attr)) if i != ref.index):
                raise ValueError(f"键重复：{key}")
            p["key"] = key
        if ref.kind == "objective":
            blk = t.objective[ref.index]
            # 读取新值
            new_start = self._positive_int(p.get("start", blk.start), "起始题号")
            new_qtype = p.get("qtype", blk.qtype)
            new_group = self._positive_int(p.get("group", blk.group), "分组题数", 0)
            new_n_q = self._positive_int(p.get("n_questions", len(blk.grid.cells)), "题数")
            new_labels = p.get("labels", blk.grid.labels)
            # 保持 n_options 与 labels 一致：若只提供了 n_options 则截取/扩展 labels
            if "n_options" in p and "labels" not in p:
                n_opt = self._positive_int(p["n_options"], "选项数")
                if n_opt != len(new_labels):
                    new_labels = [chr(ord("A") + i) for i in range(n_opt)]
            self._validate_labels(new_labels)
            new_horiz = p.get("options_horizontal", _infer_horizontal(blk.grid.cells))
            old_keys = [str(blk.start + i) for i in range(len(blk.grid.cells))]
            new_keys = [str(new_start + i) for i in range(new_n_q)]
            other_keys = {
                str(other.start + i)
                for index, other in enumerate(t.objective) if index != ref.index
                for i in range(len(other.grid.cells))
            } | {s.no for s in t.subjective} | {s.key for s in t.subjective}
            if other_keys.intersection(new_keys):
                raise ValueError("客观题题号与已有题目重复")
            self._update_question_refs(
                t, dict(zip(old_keys, new_keys)), set(old_keys[new_n_q:]))

            # 判断是否需要重新生成网格
            need_regen = (
                new_n_q != len(blk.grid.cells) or
                len(new_labels) != len(blk.grid.labels) or
                new_horiz != _infer_horizontal(blk.grid.cells) or
                new_group != blk.group
            )

            # 更新非布局参数
            blk.start = new_start
            blk.qtype = new_qtype
            blk.group = new_group
            blk.grid.labels = new_labels

            if need_regen:
                bbox = _grid_bbox(blk.grid.cells)
                blk.grid.cells = grid_in_rect(
                    bbox, new_n_q, len(new_labels), new_horiz,
                    block_every=blk.group, gap_ratio=0.5, cell_ratio=0.62,
                )

            # 仅在 p 未显式提供 name 时自动更新名称
            if "name" in p:
                blk.name = p["name"]
            else:
                blk.name = f"{blk.start}–{blk.start + len(blk.grid.cells) - 1}"
        elif ref.kind == "exam_no":
            old_n = len(t.exam_no.grid.cells)
            new_n = self._positive_int(p.get("n_digits", old_n), "位数")
            old_horiz = _infer_horizontal(t.exam_no.grid.cells)
            new_horiz = p.get("options_horizontal", old_horiz)
            if new_n != old_n or new_horiz != old_horiz:
                bbox = _grid_bbox(t.exam_no.grid.cells)
                t.exam_no.grid.cells = grid_in_rect(bbox, new_n, 10, new_horiz, cell_ratio=0.62)
        elif ref.kind == "omr_field":
            of = t.omr_fields[ref.index]
            of.key = p.get("key", of.key)
            of.label = p.get("label", of.label)
            new_labels = p.get("labels", of.grid.labels)
            self._validate_labels(new_labels)
            old_n = len(of.grid.cells)
            new_n = self._positive_int(p.get("n_digits", old_n), "位数")
            old_horiz = _infer_horizontal(of.grid.cells)
            new_horiz = p.get("options_horizontal", old_horiz)
            need_regen = (new_n != old_n or len(new_labels) != len(of.grid.labels)
                          or new_horiz != old_horiz)
            of.grid.labels = new_labels
            if need_regen:
                bbox = _grid_bbox(of.grid.cells)
                of.grid.cells = grid_in_rect(bbox, new_n, len(new_labels), new_horiz, cell_ratio=0.62)
        elif ref.kind == "subjective":
            s = t.subjective[ref.index]
            old_key = s.key
            s.no = self._key(str(p.get("no", s.no)) if p.get("no", s.no) is not None else "", "题号")
            s.sub = str(p.get("sub", s.sub) or "")
            if s.sub:
                s.sub = self._key(s.sub, "小题号")
            if any(other.key == s.key for i, other in enumerate(t.subjective) if i != ref.index):
                raise ValueError(f"主观题题号重复：{s.key}")
            objective_keys = {str(block.start + i) for block in t.objective
                              for i in range(len(block.grid.cells))}
            if s.no in objective_keys or s.key in objective_keys:
                raise ValueError(f"主观题 {s.key} 与客观题题号重复")
            self._update_question_refs(t, {old_key: s.key}, set())
            s.qtype = p.get("qtype", s.qtype)
            s.content = p.get("content", s.content)
        elif ref.kind == "choose_one":
            ch = t.choose_one[ref.index]
            ch.key = p.get("key", ch.key)
            ch.label = p.get("label", ch.label)
            if "questions" in p:
                questions = p["questions"]
                if not isinstance(questions, (list, tuple)) or len(questions) < 2:
                    raise ValueError("选做组至少需要 2 个题号")
                questions = [self._key(q, "选做题号") for q in questions]
                if len(set(questions)) != len(questions):
                    raise ValueError("选做组题号不能重复")
                available = {s.key for s in t.subjective} | {
                    str(block.start + i) for block in t.objective
                    for i in range(len(block.grid.cells))
                }
                if set(questions) - available:
                    raise ValueError("选做组引用了不存在的题号")
                if len(questions) != len(ch.cells):
                    x, y, w, h = _grid_bbox([ch.cells])
                    width = w / len(questions)
                    ch.cells = [(round(x + i * width, 3), round(y, 3),
                                 round(width, 3), round(h, 3))
                                for i in range(len(questions))]
                ch.questions = questions
        elif ref.kind == "field":
            f = t.fields[ref.index]
            f.key = p.get("key", f.key)
            f.label = p.get("label", f.label)
        elif ref.kind == "barcode":
            t.barcodes[ref.index].key = p.get("key", t.barcodes[ref.index].key)

    @staticmethod
    def _validate_labels(labels) -> None:
        if (not isinstance(labels, (list, tuple)) or not labels
                or any(not isinstance(label, str) or not label.strip() for label in labels)
                or len(set(labels)) != len(labels)):
            raise ValueError("选项标签不能为空或重复")

    def snap(self, ref: RegionRef, apply: bool = True) -> SnapResult:
        """在底图上吸附网格（失败时在半个间距内换起点重试，见 auto.snap_grid_search）。

        只有结果 ok 且 apply=True 时才写回格子并标记 dirty；失败时文档不变。"""
        t = self.template
        face = self.face_of(ref)
        base = self.bases.get(face)
        if base is None:
            return SnapResult(cells=[], ok=False, inliers=0, total=0,
                              max_residual_mm=0.0, message="缺少底图", code="unsupported")
        grid = self._grid(ref)
        if grid is None:
            return SnapResult(cells=[], ok=False, inliers=0, total=0,
                              max_residual_mm=0.0, message="该区域不支持吸附", code="unsupported")
        result = snap_grid_search(base, copy.deepcopy(grid.cells), t.dpi)
        if result.ok and apply:
            self.apply_snap(ref, result)
        return result

    def apply_snap(self, ref: RegionRef, result: SnapResult) -> None:
        """把通过的吸附结果写回网格。"""
        grid = self._grid(ref)
        if grid is None or not result.ok:
            return
        cells = [[tuple(float(v) for v in c) for c in row] for row in result.cells]
        if grid.cells != cells:
            grid.cells = cells
            self._changed()

    def _grid(self, ref: RegionRef) -> OmrGrid | None:
        t = self.template
        if ref.kind == "objective":
            return t.objective[ref.index].grid
        if ref.kind == "exam_no" and t.exam_no is not None:
            return t.exam_no.grid
        if ref.kind == "omr_field":
            return t.omr_fields[ref.index].grid
        return None

    def add_subjective_region(self, index: int, face: str, rect: Rect) -> RegionRef:
        """给第 index 道主观题追加一个区域（按列表顺序拼接），返回新区域的引用。"""
        if not 0 <= index < len(self.template.subjective):
            raise IndexError("主观题索引超出范围")
        if face not in self.template.faces:
            raise ValueError(f"不存在的面：{face}")
        if len(rect) != 4 or any(not math.isfinite(v) for v in rect):
            raise ValueError("区域矩形必须包含 4 个有限数值")
        x, y, w, h = rect
        pw, ph = self.template.paper
        if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > pw or y + h > ph:
            raise ValueError("区域宽高必须为正，且不能超出纸面")
        regions = self.template.subjective[index].regions
        regions.append(Region(face=face, rect=rect))
        self._changed()
        return RegionRef("subjective", index, len(regions) - 1)

    def move_subjective_region(self, index: int, old: int, new: int) -> RegionRef:
        """移动同一题的区域（可跨面）；old/new 均为零起始的列表索引。"""
        if not 0 <= index < len(self.template.subjective):
            raise IndexError("主观题索引超出范围")
        regions = self.template.subjective[index].regions
        if not 0 <= old < len(regions) or not 0 <= new < len(regions):
            raise IndexError("主观题区域索引超出范围")
        if old != new:
            regions.insert(new, regions.pop(old))
            self._changed()
        return RegionRef("subjective", index, new)

    def auto_align(self, face: str, what: Literal["anchors", "frames", "sync"]) -> str:
        """自动检测定位块/外框/同步头，返回中文结果说明。"""
        base = self.bases.get(face)
        if base is None:
            return "缺少底图"

        t = self.template
        if what == "anchors":
            result = auto_anchors(base, t.dpi, t.paper)
            if result is None:
                return "未检测到定位块"
            changed = (t.faces[face].align.method != "anchors"
                       or t.faces[face].align.anchors != result)
            t.faces[face].align.method = "anchors"
            t.faces[face].align.anchors = result
            if changed:
                self._changed()
            return f"检测到 {len(result)} 个定位块"
        elif what == "frames":
            result = auto_frames(base, t.dpi, t.paper)
            if not result:
                return "未检测到外框"
            changed = (t.faces[face].align.method != "frames"
                       or t.faces[face].align.frames != result)
            t.faces[face].align.method = "frames"
            t.faces[face].align.frames = result
            if changed:
                self._changed()
            return f"检测到 {len(result)} 个外框"
        elif what == "sync":
            result = auto_sync(base, t.dpi)
            # 给同步头设置正确的 face
            for s in result:
                s.face = face
            # 删掉该面的旧同步头
            if [s for s in t.sync if s.face == face] != result:
                t.sync = [s for s in t.sync if s.face != face]
                t.sync.extend(result)
                self._changed()
            if result:
                total_marks = sum(len(s.marks) for s in result)
                return f"检测到 {len(result)} 组同步头（共 {total_marks} 个标记）"
            return "未检测到同步头"
        return f"不支持的检测类型: {what}"

    def problems(self) -> list[str]:
        """校验问题列表。结构校验不通过时跳过对齐检查（避免崩溃）。"""
        candidate = self.template
        if candidate.version == 0 and self._saved_version == 0:
            # 草稿从 0 开始；结构校验针对首次保存会发布的版本，不能改动文档。
            candidate = copy.copy(candidate)
            candidate.version = 1
        msgs = validate_template(candidate)
        if not msgs:
            msgs.extend(check_alignment(candidate, self.bases))
        return msgs

    def save(self, directory: Path | None = None) -> Path:
        """保存模板。有问题时抛 ValueError。"""
        from ..app.library import user_templates_dir
        from ..app.settings import bundled_templates_dir

        probs = self.problems()
        if probs:
            raise ValueError("\n".join(probs))

        # 确定目标目录
        target = directory
        if target is None:
            target = self.directory

        # 如果目标目录为空、位于 bundled 下、或不可写，改用用户模板目录
        need_redirect = False
        if target is None:
            need_redirect = True
        else:
            target = Path(target)
            bundled = bundled_templates_dir()
            try:
                if target.resolve().is_relative_to(bundled.resolve()):
                    need_redirect = True
            except (ValueError, OSError):
                pass
            if not need_redirect:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                    # 测试可写
                    test_file = target / ".write_test"
                    test_file.touch()
                    test_file.unlink()
                except OSError:
                    need_redirect = True

        if need_redirect:
            base_name = _safe_filename(self.template.name)
            utd = user_templates_dir()
            target = utd / base_name
            n = 2
            while target.exists():
                target = utd / f"{base_name}_{n}"
                n += 1

        old_version = self.template.version
        self.template.version = max(self._saved_version, old_version) + 1
        try:
            save_template(self.template, target, self.bases)
        except Exception:
            self.template.version = old_version
            raise
        self._saved_version = self.template.version
        self.dirty = False
        self.directory = target
        return target

    # ── 快照与恢复 ────────────────────────────────────────────

    def snapshot(self) -> dict:
        """获取模板快照（不含底图）。"""
        return template_to_dict(self.template)

    def restore(self, snap: dict) -> None:
        """从快照恢复模板。恢复后标记为脏（模板可能与磁盘不一致）。"""
        restored = template_from_dict(copy.deepcopy(snap))
        restored.version = self._saved_version
        # 底图不是撤销内容；保留最近保存时发布的资源引用。
        for name, face in restored.faces.items():
            if name in self.template.faces:
                face.image = self.template.faces[name].image
        self.template = restored
        self._changed()


class UndoStack:
    """撤销重做栈。"""

    def __init__(self, doc: TemplateDoc, limit: int = 200):
        self._doc = doc
        self._limit = limit
        self._undo: list[tuple[str, dict]] = []
        self._redo: list[tuple[str, dict]] = []

    def push(self, label: str, snapshot: dict | None = None) -> None:
        """记录修改前快照并清空重做栈；可在修改成功后传入先前快照。"""
        snap = self._doc.snapshot() if snapshot is None else copy.deepcopy(snapshot)
        self._undo.append((label, snap))
        if len(self._undo) > self._limit:
            self._undo.pop(0)
        self._redo.clear()

    def undo(self) -> bool:
        if not self._undo:
            return False
        label, snap = self._undo.pop()
        # 保存当前状态到重做栈
        self._redo.append((label, self._doc.snapshot()))
        self._doc.restore(snap)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        label, snap = self._redo.pop()
        # 保存当前状态到撤销栈
        self._undo.append((label, self._doc.snapshot()))
        self._doc.restore(snap)
        return True

    def can_undo(self) -> bool:
        return len(self._undo) > 0

    def can_redo(self) -> bool:
        return len(self._redo) > 0
