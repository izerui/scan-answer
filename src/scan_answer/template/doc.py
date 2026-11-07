"""可编辑模板文档：TemplateDoc 包装 Template + 底图，提供区域增删改查、撤销重做、保存。"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    s = re.sub(r'[\\/:*?"<>|]', "", name).strip()
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
    if ref.index < len(mapping):
        return mapping[ref.index]
    raise IndexError(f"anchor/frame 全局索引 {ref.index} 超出范围（共 {len(mapping)} 个）")


class TemplateDoc:
    """可编辑模板文档。"""

    def __init__(self, t: Template, bases: dict[str, np.ndarray], directory: Path | None):
        self.template: Template = t
        self.bases: dict[str, np.ndarray] = bases
        self.directory: Path | None = directory
        self.dirty: bool = False

    @classmethod
    def load(cls, directory: str | Path) -> "TemplateDoc":
        d = Path(directory)
        t, bases = load_template(d)
        return cls(t, bases, d)

    @classmethod
    def new(cls, name: str, faces: dict[str, ImportedBase]) -> "TemplateDoc":
        """从导入的底图创建新模板。"""
        face_dict: dict[str, Face] = {}
        bases: dict[str, np.ndarray] = {}
        paper = None
        dpi = 300

        for fname, ib in faces.items():
            bases[fname] = ib.image
            if paper is None:
                paper = ib.paper

            # 自动检测定位方式
            anchors = auto_anchors(ib.image, dpi, ib.paper)
            if anchors is not None:
                align = AlignSpec(method="anchors", anchors=anchors)
            else:
                frames = auto_frames(ib.image, dpi, ib.paper)
                if frames:
                    align = AlignSpec(method="frames", frames=frames)
                else:
                    align = AlignSpec(method="anchors")  # 空列表，保存时由校验拦下

            face_dict[fname] = Face(image=f"{fname}.png", align=align)

        if paper is None:
            paper = (210.0, 297.0)

        t = Template(
            schema_version="1.0", name=name, version=0,
            paper=paper, dpi=dpi, faces=face_dict,
            exam_no=None, absent=None,
            barcodes=[], fields=[], objective=[], subjective=[],
            ignore=[], sync=[], omr_fields=[], choose_one=[],
        )

        # auto_sync 对每个面
        for fname, ib in faces.items():
            syncs = auto_sync(ib.image, dpi)
            for s in syncs:
                s.face = fname
            t.sync.extend(syncs)

        doc = cls(t, bases, None)
        doc.dirty = True
        return doc

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
            if n_reg > 1:
                return f"{s.no} · {ref.sub + 1}"
            return str(s.no)
        if ref.kind == "choose_one":
            return t.choose_one[ref.index].label
        if ref.kind == "ignore":
            return f"忽略区 {ref.index + 1}"
        return ref.kind

    # ── 修改操作 ──────────────────────────────────────────────

    def set_rect(self, ref: RegionRef, rect: Rect) -> None:
        """设置区域矩形。网格类：所有格按外包框的变化做仿射缩放平移。"""
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
        self.dirty = True

    def add(self, kind: str, face: str, rect: Rect) -> RegionRef:
        """新增区域，返回 RegionRef。"""
        t = self.template
        if kind == "anchor":
            t.faces[face].align.anchors.append(rect)
            # 全局索引 = 追加后的全局映射中最后一个属于该面的
            gi = len(_anchor_frame_map(t, "anchor")) - 1
            ref = RegionRef("anchor", gi)
        elif kind == "frame":
            t.faces[face].align.frames.append(rect)
            gi = len(_anchor_frame_map(t, "frame")) - 1
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
            subj_nos = [s.no for s in t.subjective]
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
        self.dirty = True
        return ref

    def remove(self, refs: list[RegionRef]) -> None:
        """删除区域。"""
        t = self.template
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

        self.dirty = True

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
            return {"no": s.no, "qtype": s.qtype, "content": s.content}
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
        """设置区域参数。只在布局相关参数（数量/选项数/方向/分组）改变时重新生成网格。"""
        t = self.template
        if ref.kind == "objective":
            blk = t.objective[ref.index]
            # 读取新值
            new_start = p.get("start", blk.start)
            new_qtype = p.get("qtype", blk.qtype)
            new_group = p.get("group", blk.group)
            new_n_q = p.get("n_questions", len(blk.grid.cells))
            new_labels = p.get("labels", blk.grid.labels)
            # 保持 n_options 与 labels 一致：若只提供了 n_options 则截取/扩展 labels
            if "n_options" in p and "labels" not in p:
                n_opt = p["n_options"]
                if n_opt != len(new_labels):
                    defaults = list("ABCDEFGH")[:n_opt]
                    new_labels = defaults
            new_horiz = p.get("options_horizontal", _infer_horizontal(blk.grid.cells))

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
            new_n = p.get("n_digits", old_n)
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
            old_n = len(of.grid.cells)
            new_n = p.get("n_digits", old_n)
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
            s.no = p.get("no", s.no)
            s.qtype = p.get("qtype", s.qtype)
            s.content = p.get("content", s.content)
        elif ref.kind == "choose_one":
            ch = t.choose_one[ref.index]
            ch.key = p.get("key", ch.key)
            ch.label = p.get("label", ch.label)
            ch.questions = p.get("questions", ch.questions)
        elif ref.kind == "field":
            f = t.fields[ref.index]
            f.key = p.get("key", f.key)
            f.label = p.get("label", f.label)
        elif ref.kind == "barcode":
            t.barcodes[ref.index].key = p.get("key", t.barcodes[ref.index].key)

        self.dirty = True

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
        grid.cells = [[tuple(float(v) for v in c) for c in row] for row in result.cells]
        self.dirty = True

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
        regions = self.template.subjective[index].regions
        regions.append(Region(face=face, rect=rect))
        self.dirty = True
        return RegionRef("subjective", index, len(regions) - 1)

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
            t.faces[face].align.method = "anchors"
            t.faces[face].align.anchors = result
            self.dirty = True
            return f"检测到 {len(result)} 个定位块"
        elif what == "frames":
            result = auto_frames(base, t.dpi, t.paper)
            if not result:
                return "未检测到外框"
            t.faces[face].align.method = "frames"
            t.faces[face].align.frames = result
            self.dirty = True
            return f"检测到 {len(result)} 个外框"
        elif what == "sync":
            result = auto_sync(base, t.dpi)
            # 给同步头设置正确的 face
            for s in result:
                s.face = face
            # 删掉该面的旧同步头
            t.sync = [s for s in t.sync if s.face != face]
            t.sync.extend(result)
            self.dirty = True
            if result:
                total_marks = sum(len(s.marks) for s in result)
                return f"检测到 {len(result)} 组同步头（共 {total_marks} 个标记）"
            return "未检测到同步头"
        return f"不支持的检测类型: {what}"

    def problems(self) -> list[str]:
        """校验问题列表。结构校验不通过时跳过对齐检查（避免崩溃）。"""
        msgs = validate_template(self.template)
        if not msgs:
            msgs.extend(check_alignment(self.template, self.bases))
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

        self.template.version += 1
        try:
            save_template(self.template, target, self.bases)
        except Exception:
            self.template.version -= 1  # 回滚
            raise
        self.dirty = False
        self.directory = target
        return target

    # ── 快照与恢复 ────────────────────────────────────────────

    def snapshot(self) -> dict:
        """获取模板快照（不含底图）。"""
        return template_to_dict(self.template)

    def restore(self, snap: dict) -> None:
        """从快照恢复模板。恢复后标记为脏（模板可能与磁盘不一致）。"""
        self.template = template_from_dict(snap)
        self.dirty = True


class UndoStack:
    """撤销重做栈。"""

    def __init__(self, doc: TemplateDoc, limit: int = 200):
        self._doc = doc
        self._limit = limit
        self._undo: list[tuple[str, dict]] = []
        self._redo: list[tuple[str, dict]] = []

    def push(self, label: str) -> None:
        """在修改之前调用：记录当前快照，并清空重做栈。"""
        snap = self._doc.snapshot()
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
