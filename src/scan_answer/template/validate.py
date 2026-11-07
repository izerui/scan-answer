"""模板校验：结构合法性检查与底图自对齐检查。"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np

from .model import Rect, Template

if TYPE_CHECKING:
    pass

# 弯引号常量，避免在 f-string 中与字符串定界符冲突
_LQ = "\u201c"
_RQ = "\u201d"


def _rect_valid(r: Rect, paper_w: float, paper_h: float) -> tuple[bool, str]:
    """检查矩形的宽高为正，且完全落在纸面内。返回 (ok, 原因关键字)。"""
    x, y, w, h = r
    if w <= 0 or h <= 0:
        return False, "宽或高"  # 宽或高
    if x < 0 or y < 0 or x + w > paper_w or y + h > paper_h:
        return False, "超出纸面"  # 超出纸面
    return True, ""


def _check_rects(rects: list[tuple[str, Rect]], paper_w: float, paper_h: float) -> list[str]:
    """批量检查矩形列表，返回问题消息。rects 每项为 (描述名, rect)。"""
    msgs: list[str] = []
    for desc, r in rects:
        ok, reason = _rect_valid(r, paper_w, paper_h)
        if not ok:
            msgs.append(f"{desc}：{reason}")
    return msgs


def _q(name: str) -> str:
    """用弯引号包裹名称。"""
    return f"{_LQ}{name}{_RQ}"


def validate_template(t: Template) -> list[str]:
    """返回中文问题列表，空列表表示通过。"""
    msgs: list[str] = []
    pw, ph = t.paper
    face_names = set(t.faces.keys())

    # 收集所有需要检查的矩形
    rects: list[tuple[str, Rect]] = []

    # 考号
    if t.exam_no:
        for i, row in enumerate(t.exam_no.grid.cells):
            for j, c in enumerate(row):
                rects.append((f"考号第{i}位选项{j}", c))

    # 缺考标记
    if t.absent:
        rects.append(("缺考标记", t.absent.rect))

    # 条码
    for b in t.barcodes:
        rects.append((f"条码{_q(b.key)}", b.rect))

    # 字段
    for f in t.fields:
        rects.append((f"字段{_q(f.key)}", f.rect))

    # 客观题
    for blk in t.objective:
        for i, row in enumerate(blk.grid.cells):
            for j, c in enumerate(row):
                qno = blk.start + i
                rects.append((f"客观题第{qno}题选项{blk.grid.labels[j]}", c))

    # 主观题
    for s in t.subjective:
        for ri, reg in enumerate(s.regions):
            rects.append((f"主观题{s.no}区域{ri}", reg.rect))

    # 忽略区
    for i, reg in enumerate(t.ignore):
        rects.append((f"忽略区{i}", reg.rect))

    # 同步头
    for si, sync in enumerate(t.sync):
        for mi, m in enumerate(sync.marks):
            rects.append((f"同步头{si}标记{mi}", m))

    # 自定义填涂字段
    for omr in t.omr_fields:
        for i, row in enumerate(omr.grid.cells):
            for j, c in enumerate(row):
                rects.append((f"填涂字段{_q(omr.key)}第{i}组选项{j}", c))

    # 选做组
    for ch in t.choose_one:
        for ci, c in enumerate(ch.cells):
            rects.append((f"选做组{_q(ch.key)}格{ci}", c))

    # 检查项 1：矩形合法
    msgs.extend(_check_rects(rects, pw, ph))

    # 检查项 2：区域引用的面存在
    face_refs: list[tuple[str, str]] = []
    if t.exam_no:
        face_refs.append(("考号", t.exam_no.grid.face))
    if t.absent:
        face_refs.append(("缺考标记", t.absent.face))
    for b in t.barcodes:
        face_refs.append((f"条码{_q(b.key)}", b.face))
    for f in t.fields:
        face_refs.append((f"字段{_q(f.key)}", f.face))
    for blk in t.objective:
        face_refs.append((f"客观题块{_q(blk.name)}", blk.grid.face))
    for s in t.subjective:
        for reg in s.regions:
            face_refs.append((f"主观题{s.no}", reg.face))
    for reg in t.ignore:
        face_refs.append(("忽略区", reg.face))
    for sync in t.sync:
        face_refs.append(("同步头", sync.face))
    for omr in t.omr_fields:
        face_refs.append((f"填涂字段{_q(omr.key)}", omr.grid.face))
    for ch in t.choose_one:
        face_refs.append((f"选做组{_q(ch.key)}", ch.face))

    for desc, face in face_refs:
        if face not in face_names:
            msgs.append(f"{desc}：引用了不存在的面{_q(face)}")

    # 检查项 3：定位方式的要求
    for fname, face in t.faces.items():
        cn = "正面" if fname == "front" else "背面"
        if face.align.method == "anchors":
            if len(face.align.anchors) < 4:
                msgs.append(
                    f"{cn}：anchors 方式至少 4 个定位块"
                    f"（当前 {len(face.align.anchors)} 个）"
                )
        elif face.align.method == "frames":
            if len(face.align.frames) < 1:
                msgs.append(f"{cn}：frames 方式至少 1 个外框")

    # 检查项 4：题号不重复
    obj_qnos: dict[str, str] = {}  # qno_str -> 来源描述
    for blk in t.objective:
        for i in range(len(blk.grid.cells)):
            qno = str(blk.start + i)
            if qno in obj_qnos:
                msgs.append(
                    f"客观题题号重复：{qno}"
                    f"（{_q(blk.name)}与{_q(obj_qnos[qno])}）"
                )
            else:
                obj_qnos[qno] = blk.name
    subj_qnos: dict[str, str] = {}
    for s in t.subjective:
        if s.no in subj_qnos:
            msgs.append(f"主观题题号重复：{s.no}")
        else:
            subj_qnos[s.no] = s.no
        if s.no in obj_qnos:
            msgs.append(f"主观题{s.no}与客观题题号重复")

    # 检查项 5：各类别内键不重复
    for category, items, key_attr in [
        ("条码", t.barcodes, "key"),
        ("字段", t.fields, "key"),
        ("填涂字段", t.omr_fields, "key"),
        ("选做组", t.choose_one, "key"),
    ]:
        seen: set[str] = set()
        for item in items:
            k = getattr(item, key_attr)
            if k in seen:
                msgs.append(f"{category}键重复：{_q(k)}")
            seen.add(k)

    # 检查项 6：OMR 网格每组格数 == len(labels)，至少 1 组
    for omr in t.omr_fields:
        g = omr.grid
        if len(g.cells) < 1:
            msgs.append(f"填涂字段{_q(omr.key)}：至少需要 1 组")
        for i, row in enumerate(g.cells):
            if len(row) != len(g.labels):
                msgs.append(
                    f"填涂字段{_q(omr.key)}第{i}组："
                    f"格数 {len(row)} 不等于标签数 {len(g.labels)}"
                )

    # 检查项 7：选做组
    all_qnos = set(obj_qnos.keys()) | set(subj_qnos.keys())
    for ch in t.choose_one:
        if len(ch.questions) != len(ch.cells):
            msgs.append(f"选做组{_q(ch.key)}：questions 与 cells 长度不一致")
        if len(ch.questions) < 2:
            msgs.append(f"选做组{_q(ch.key)}：至少需要 2 个题号")
        for qno in ch.questions:
            if qno not in all_qnos:
                msgs.append(
                    f"选做组{_q(ch.key)}：题号 {qno} "
                    f"不在客观题或主观题中"
                )

    # 检查项 8：同步头每组至少 2 个
    for i, sync in enumerate(t.sync):
        if len(sync.marks) < 2:
            msgs.append(
                f"同步头{i}：至少需要 2 个标记"
                f"（当前 {len(sync.marks)} 个）"
            )

    return msgs


def check_alignment(t: Template, bases: dict[str, np.ndarray]) -> list[str]:
    """在每面自己的底图上运行对齐检查。"""
    from ..engine.align import align_primary

    msgs: list[str] = []
    face_cn = {"front": "正面", "back": "背面"}
    for name, face in t.faces.items():
        cn = face_cn.get(name, name)
        if name not in bases:
            msgs.append(f"{cn}：缺少底图")
            continue
        result = align_primary(bases[name], face.align, t.dpi)
        if not result.ok:
            msgs.append(f"{cn}：用当前定位方式在底图上对齐失败（{result.reason}）")
        elif result.residual_mm > 0.5:
            msgs.append(f"{cn}：残差 {result.residual_mm:.2f} mm 过大")
    return msgs
