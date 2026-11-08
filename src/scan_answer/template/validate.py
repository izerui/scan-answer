"""模板校验：结构合法性检查与底图自对齐检查。"""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from functools import lru_cache
from math import isfinite
from numbers import Integral, Real
from types import UnionType
from typing import Literal, get_args, get_origin, get_type_hints

import numpy as np

from .model import Rect, Template

# 弯引号常量，避免在 f-string 中与字符串定界符冲突
_LQ = "\u201c"
_RQ = "\u201d"


@lru_cache
def _field_types(cls: type) -> dict:
    return get_type_hints(cls)


def _check_structure(value, expected, path: str, msgs: list[str]) -> None:
    """先验证模型形状，使后面的业务检查无需索引或运算无效数据。"""
    origin, args = get_origin(expected), get_args(expected)
    if origin is UnionType:
        # 模型中的联合类型都是可空的数据类。
        if value is None and type(None) in args:
            return
        _check_structure(value, next(a for a in args if a is not type(None)), path, msgs)
        return
    if is_dataclass(expected):
        if not isinstance(value, expected):
            msgs.append(f"{path}：结构错误，需要 {expected.__name__}")
            return
        hints = _field_types(expected)
        for field in fields(expected):
            if not hasattr(value, field.name):
                msgs.append(f"{path}.{field.name}：缺少字段")
            else:
                _check_structure(getattr(value, field.name), hints[field.name],
                                 f"{path}.{field.name}", msgs)
        return
    if origin in (list, tuple):
        if not isinstance(value, (list, tuple)) or (origin is tuple and len(value) != len(args)):
            msgs.append(f"{path}：列表或坐标结构错误")
            return
        for i, item in enumerate(value):
            _check_structure(item, args[0] if origin is list else args[i], f"{path}[{i}]", msgs)
        return
    if origin is dict:
        if not isinstance(value, dict):
            msgs.append(f"{path}：需要键值映射")
            return
        for key, item in value.items():
            _check_structure(key, args[0], f"{path} 的键", msgs)
            _check_structure(item, args[1], f"{path}[{key!r}]", msgs)
        return
    if origin is Literal:
        if not isinstance(value, str) or value not in args:
            reason = "引用了不存在的面" if args == ("front", "back") else "不支持的类型或方式"
            msgs.append(f"{path}：{reason} {value!r}，应为 {args}")
        return
    if expected in (int, float):
        numeric_type = Integral if expected is int else Real
        ok = isinstance(value, numeric_type) and not isinstance(value, bool)
        if ok:
            try:
                ok = isfinite(value)
            except (OverflowError, TypeError, ValueError):
                ok = False
        if not ok:
            msgs.append(f"{path}：需要有限{'整数' if expected is int else '数值'}")
        return
    if not isinstance(value, expected):
        msgs.append(f"{path}：需要 {expected.__name__}")


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
    _check_structure(t, Template, "模板", msgs)
    if msgs:
        return msgs

    pw, ph = t.paper
    face_names = set(t.faces.keys())
    if t.schema_version != "1.0":
        msgs.append(f"不支持的模板版本：{t.schema_version}")
    if not t.name.strip():
        msgs.append("模板名称不能为空")
    if t.version <= 0:
        msgs.append("模板版本号必须为正整数")
    if pw <= 0 or ph <= 0:
        msgs.append("纸张宽高必须为正数")
    if min(pw, ph) > 298 or max(pw, ph) > 421:
        msgs.append("纸张不能超过 A3（短边 297 mm、长边 420 mm，允许 1 mm 误差）")
    if t.dpi <= 0:
        msgs.append("DPI 必须为正整数")
    if not face_names:
        msgs.append("模板至少需要一个有效面")
    for name, face in t.faces.items():
        if name not in ("front", "back"):
            msgs.append(f"无效的面：{_q(name)}")
        if not face.image.strip():
            msgs.append(f"面{_q(name)}：底图路径不能为空")

    # 收集所有需要检查的矩形
    rects: list[tuple[str, Rect]] = []
    for name, face in t.faces.items():
        for i, rect in enumerate(face.align.anchors):
            rects.append((f"面{_q(name)}定位块{i}", rect))
        for i, rect in enumerate(face.align.frames):
            rects.append((f"面{_q(name)}外框{i}", rect))

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
                rects.append((f"客观题第{qno}题选项{j}", c))

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
        if blk.start <= 0:
            msgs.append(f"客观题块{_q(blk.name)}：起始题号必须为正整数")
        if blk.group < 0:
            msgs.append(f"客观题块{_q(blk.name)}：每组题数不能为负数")
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
        if not s.no.strip() or s.no != s.no.strip():
            msgs.append("主观题题号不能为空或包含首尾空白")
        if s.sub and (not s.sub.strip() or s.sub != s.sub.strip()):
            msgs.append(f"主观题{s.no}：小题号不能仅为空白或包含首尾空白")
        if not s.regions:
            msgs.append(f"主观题{s.key}：至少需要 1 个作答区域")
        # 唯一键为“题号”或“题号.小题号”；同一题号下可以有多个小题
        if s.key in subj_qnos:
            msgs.append(f"主观题题号重复：{s.key}")
        else:
            subj_qnos[s.key] = s.no
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
            if not k.strip():
                msgs.append(f"{category}键不能为空")
            if k in seen:
                msgs.append(f"{category}键重复：{_q(k)}")
            seen.add(k)

    # 所有 OMR 网格使用相同约束，包括考号与客观题。
    grids = [(f"填涂字段{_q(o.key)}", o.grid) for o in t.omr_fields]
    grids.extend((f"客观题块{_q(o.name)}", o.grid) for o in t.objective)
    if t.exam_no:
        grids.append(("考号", t.exam_no.grid))
    for desc, g in grids:
        if not g.labels or any(not label.strip() for label in g.labels):
            msgs.append(f"{desc}：标签不能为空")
        if len(set(g.labels)) != len(g.labels):
            msgs.append(f"{desc}：标签重复")
        if len(g.cells) < 1:
            msgs.append(f"{desc}：至少需要 1 组")
        for i, row in enumerate(g.cells):
            if len(row) != len(g.labels):
                msgs.append(
                    f"{desc}第{i}组："
                    f"格数 {len(row)} 不等于标签数 {len(g.labels)}"
                )

    # 检查项 7：选做组
    all_qnos = set(obj_qnos.keys()) | set(subj_qnos.keys())
    for ch in t.choose_one:
        if len(ch.questions) != len(ch.cells):
            msgs.append(f"选做组{_q(ch.key)}：questions 与 cells 长度不一致")
        if len(ch.questions) < 2:
            msgs.append(f"选做组{_q(ch.key)}：至少需要 2 个题号")
        if len(set(ch.questions)) != len(ch.questions):
            msgs.append(f"选做组{_q(ch.key)}：题号重复")
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
