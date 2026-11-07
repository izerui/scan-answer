"""TemplateDoc 可编辑模板文档测试。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"
PDF_PATH = ROOT / "samples" / "raw" / "8k-199.pdf"


# ── test_regions_cover_199 ──────────────────────────────────────


def test_regions_cover_199():
    """199 模板正反面区域覆盖检查。"""
    from scan_answer.template.doc import RegionRef, TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)

    front = doc.regions("front")
    back = doc.regions("back")

    # 正面：4 anchor、12 sync (sub 0–11)、exam_no、2 barcode、absent、2 field、4 objective、1 subjective 区域、1 ignore
    anchors = [r for r in front if r.kind == "anchor"]
    assert len(anchors) == 4, f"正面 anchor 数: {len(anchors)}"

    syncs = [r for r in front if r.kind == "sync"]
    assert len(syncs) == 12, f"正面 sync 数: {len(syncs)}"
    sync_subs = sorted(r.sub for r in syncs)
    assert sync_subs == list(range(12)), f"sync sub: {sync_subs}"

    exam_nos = [r for r in front if r.kind == "exam_no"]
    assert len(exam_nos) == 1

    barcodes = [r for r in front if r.kind == "barcode"]
    assert len(barcodes) == 2

    absents = [r for r in front if r.kind == "absent"]
    assert len(absents) == 1

    fields_front = [r for r in front if r.kind == "field"]
    assert len(fields_front) == 2

    objectives = [r for r in front if r.kind == "objective"]
    assert len(objectives) == 4

    subj_front = [r for r in front if r.kind == "subjective"]
    assert len(subj_front) == 1, f"正面 subjective 区域数: {len(subj_front)}"

    ignores = [r for r in front if r.kind == "ignore"]
    assert len(ignores) == 1

    # 背面：2 frame、1 field、3 subjective 区域
    frames = [r for r in back if r.kind == "frame"]
    assert len(frames) == 2, f"背面 frame 数: {len(frames)}"

    fields_back = [r for r in back if r.kind == "field"]
    assert len(fields_back) == 1

    subj_back = [r for r in back if r.kind == "subjective"]
    assert len(subj_back) == 3, f"背面 subjective 区域数: {len(subj_back)}"

    # 每个区域都有 rect 和 title
    for ref in front + back:
        r = doc.rect(ref)
        assert len(r) == 4
        assert all(isinstance(v, (int, float)) for v in r)
        t = doc.title(ref)
        assert isinstance(t, str) and len(t) > 0
        f = doc.face_of(ref)
        assert isinstance(f, str) and f in ("front", "back")


# ── test_set_rect_scales_grid ──────────────────────────────────


def test_set_rect_scales_grid():
    """set_rect 对客观题块做仿射缩放平移。"""
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    front = doc.regions("front")
    obj_refs = [r for r in front if r.kind == "objective"]
    ref = obj_refs[0]  # 第一个客观题块

    old_rect = doc.rect(ref)
    old_cells = doc.template.objective[ref.index].grid.cells

    # 平移 (+2, +3)
    new_rect = (old_rect[0] + 2, old_rect[1] + 3, old_rect[2], old_rect[3])
    doc.set_rect(ref, new_rect)
    new_cells = doc.template.objective[ref.index].grid.cells

    for g in range(len(old_cells)):
        for o in range(len(old_cells[g])):
            ox, oy, ow, oh = old_cells[g][o]
            nx, ny, nw, nh = new_cells[g][o]
            assert abs((nx - ox) - 2) < 0.01, f"平移 x: {nx - ox}"
            assert abs((ny - oy) - 3) < 0.01, f"平移 y: {ny - oy}"
            assert abs(nw - ow) < 0.01
            assert abs(nh - oh) < 0.01

    # 宽度放大 1.1 倍
    cur_rect = doc.rect(ref)
    scale_rect = (cur_rect[0], cur_rect[1], cur_rect[2] * 1.1, cur_rect[3])
    before_cells = [row[:] for row in doc.template.objective[ref.index].grid.cells]
    doc.set_rect(ref, scale_rect)
    after_cells = doc.template.objective[ref.index].grid.cells

    # 最右一列格子中心应按比例移动
    n_opt = len(before_cells[0])
    for g in range(len(before_cells)):
        # 最右列
        bx, by, bw, bh = before_cells[g][n_opt - 1]
        ax, ay, aw, ah = after_cells[g][n_opt - 1]
        b_cx = bx + bw / 2
        a_cx = ax + aw / 2
        # 中心相对于原左边界
        left = cur_rect[0]
        expected_cx = left + (b_cx - left) * 1.1
        assert abs(a_cx - expected_cx) < 0.1, f"最右列中心: {a_cx} vs {expected_cx}"
        # 宽度也按比例
        assert abs(aw / bw - 1.1) < 0.05, f"格子宽度比: {aw / bw}"

    assert doc.dirty


# ── test_set_params_regenerates_and_snap ────────────────────────


def test_set_params_regenerates_and_snap():
    """改题数后重新生成网格；对 199 原块 snap 通过。"""
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    front = doc.regions("front")
    obj_refs = [r for r in front if r.kind == "objective"]
    ref = obj_refs[0]  # 第一个客观题块 (1-15)

    # 改题数为 10
    p = doc.params(ref)
    p["n_questions"] = 10
    doc.set_params(ref, p)
    assert doc.dirty
    assert len(doc.template.objective[ref.index].grid.cells) == 10

    # 对 199 原块 snap，应该通过
    doc2 = TemplateDoc.load(TEMPLATE_DIR)
    ref2 = [r for r in doc2.regions("front") if r.kind == "objective"][0]
    result = doc2.snap(ref2)
    assert result.ok, f"snap 失败: {result.message}"
    # 格子变化 < 0.35 mm
    assert result.max_residual_mm <= 0.35, f"snap 偏差: {result.max_residual_mm}"


# ── test_undo_redo ──────────────────────────────────────────────


def test_undo_redo():
    """撤销重做基本功能。"""
    from scan_answer.template.doc import TemplateDoc, UndoStack

    doc = TemplateDoc.load(TEMPLATE_DIR)
    stack = UndoStack(doc)

    n_fields_before = len(doc.template.fields)

    # push → add → undo → redo → push 新操作清空 redo
    stack.push("添加字段")
    doc.add("field", "front", (100, 100, 20, 10))
    assert len(doc.template.fields) == n_fields_before + 1

    assert stack.can_undo()
    stack.undo()
    assert len(doc.template.fields) == n_fields_before

    assert stack.can_redo()
    stack.redo()
    assert len(doc.template.fields) == n_fields_before + 1

    # push 新操作后 can_redo 为 False
    stack.push("另一个操作")
    doc.add("field", "front", (150, 150, 20, 10))
    assert not stack.can_redo()


# ── test_save_bundled_goes_to_user_dir ──────────────────────────


def test_save_bundled_goes_to_user_dir(app_home):
    """保存随程序提供的模板时，写到用户模板目录。"""
    from scan_answer.app.library import list_templates, user_templates_dir
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    # 记录原文件修改时间
    orig_json = TEMPLATE_DIR / "template.json"
    orig_mtime = orig_json.stat().st_mtime

    # 修改一个字段
    doc.add("field", "front", (100, 100, 20, 10))

    # 保存
    saved = doc.save()
    assert saved.parent == user_templates_dir() or user_templates_dir() in saved.parents

    # 原模板未被修改
    assert orig_json.stat().st_mtime == orig_mtime

    # 新目录 version 为 3（原 version=2，保存时 +1）
    import json
    new_t = json.loads((saved / "template.json").read_text(encoding="utf-8"))
    assert new_t["version"] == 3

    # list_templates 能找到
    infos = list_templates([])
    names = [i.name for i in infos]
    assert doc.template.name in names


# ── test_save_blocked_on_problems ───────────────────────────────


def test_save_blocked_on_problems(app_home):
    """有校验问题时 save 抛 ValueError。"""
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)

    # 删掉 3 个定位块
    front = doc.regions("front")
    anchor_refs = [r for r in front if r.kind == "anchor"]
    doc.remove(anchor_refs[:3])

    with pytest.raises(ValueError, match="至少 4 个定位块"):
        doc.save()

    # 没有写出任何文件
    from scan_answer.app.library import user_templates_dir
    ut = user_templates_dir()
    assert not any(ut.iterdir()) if ut.exists() else True


# ── test_new_from_pdf_single_face ───────────────────────────────


def test_new_from_pdf_single_face(app_home, tmp_path):
    """单面模板全流程：PDF 导入 → 自动定位 → 加客观题 → 设参数 → snap → 保存 → 生成 → 识别。"""
    from scan_answer.template.auto import import_base, load_pages
    from scan_answer.template.doc import TemplateDoc

    # 用 PDF 第 1 页
    gray, dpi = load_pages(str(PDF_PATH))[0]
    ib = import_base(gray, dpi)

    doc = TemplateDoc.new("单面测试", {"front": ib})

    # 自动定位方式应为 anchors，恰好 4 个定位块
    assert doc.template.faces["front"].align.method == "anchors"
    assert len(doc.template.faces["front"].align.anchors) == 4

    # 恰好 1 组 sync
    assert len(doc.template.sync) == 1

    # 只有 front 面
    assert list(doc.template.faces.keys()) == ["front"]

    # 加一个客观题块，覆盖 199 block-1 区域（精确 bbox）
    ref = doc.add("objective", "front", (34.8, 154.7, 28.68, 57.4))

    # 设参数：15 题、5 选项 A-E、group 5、横排
    doc.set_params(ref, {
        "n_questions": 15,
        "labels": ["A", "B", "C", "D", "E"],
        "group": 5,
        "options_horizontal": True,
    })
    assert len(doc.template.objective[ref.index].grid.cells) == 15

    # snap 应通过
    result = doc.snap(ref)
    assert result.ok, f"snap 失败: {result.message}（inliers={result.inliers}/{result.total}, max_res={result.max_residual_mm}）"

    # 保存
    saved = doc.save(tmp_path / "single_face_tpl")

    # 生成 2 张卡并识别
    import json

    from scan_answer.batch import run_batch
    from scan_answer.synth.generate import generate_dataset
    from scan_answer.template import load_template

    t, bases = load_template(saved)
    ds = tmp_path / "dataset"
    generate_dataset(t, bases, ds, 2, seed=42, out_dpi=200)

    bd = run_batch(saved, ds, tmp_path / "out", batch_id="sf1")

    # 两张都识别成功（无物理异常）
    result_files = list(bd.glob("students/*/result.json"))
    assert len(result_files) == 2, f"结果文件数: {len(result_files)}"
    for rf in result_files:
        r = json.loads(rf.read_text(encoding="utf-8"))
        assert "objective" in r, f"缺少 objective: {list(r.keys())}"
        assert "front" in r.get("scan", {}).get("faces", {}), f"缺少 front 面"
        # 无物理异常：warnings 中没有 ALIGN_FAILED / MISSING_FACE 等
        physical = {w.get("code") for w in r.get("warnings", [])} & {
            "ALIGN_FAILED", "FACE_MISMATCH", "BLANK_PAGE", "MISSING_FACE", "SKEW_TOO_LARGE"}
        assert not physical, f"物理异常: {physical}"


# ── C1: 双面 anchors 模板的全局索引 ────────────────────────────


def test_two_face_anchors_disjoint():
    """两面都用 anchors 时，regions 互不交叉，face_of 正确，set_rect/remove 只改对应面。"""
    import copy
    from scan_answer.template.doc import TemplateDoc
    from scan_answer.template.model import AlignSpec

    doc = TemplateDoc.load(TEMPLATE_DIR)
    t = doc.template

    # 把 back 面改为 anchors 方式（复制 front 的定位块偏移 5 mm 作为不同坐标）
    front_anchors = t.faces["front"].align.anchors[:]
    back_anchors = [(x + 5, y + 5, w, h) for x, y, w, h in front_anchors]
    t.faces["back"].align = AlignSpec(method="anchors", anchors=back_anchors)

    front_refs = doc.regions("front")
    back_refs = doc.regions("back")

    front_anchors_refs = [r for r in front_refs if r.kind == "anchor"]
    back_anchors_refs = [r for r in back_refs if r.kind == "anchor"]

    # 各面 4 个定位块
    assert len(front_anchors_refs) == 4, f"前面 anchor: {len(front_anchors_refs)}"
    assert len(back_anchors_refs) == 4, f"背面 anchor: {len(back_anchors_refs)}"

    # 索引不重叠
    front_indices = {r.index for r in front_anchors_refs}
    back_indices = {r.index for r in back_anchors_refs}
    assert front_indices.isdisjoint(back_indices), f"前后索引重叠: {front_indices} & {back_indices}"

    # face_of 正确
    for r in front_anchors_refs:
        assert doc.face_of(r) == "front", f"前面 anchor {r.index} face_of={doc.face_of(r)}"
    for r in back_anchors_refs:
        assert doc.face_of(r) == "back", f"背面 anchor {r.index} face_of={doc.face_of(r)}"

    # rect 返回正确坐标
    fr0 = doc.rect(front_anchors_refs[0])
    br0 = doc.rect(back_anchors_refs[0])
    assert abs(br0[0] - fr0[0] - 5) < 0.01, "背面 anchor 坐标不对"

    # set_rect 只修改背面
    old_front_0 = doc.rect(front_anchors_refs[0])
    br = back_anchors_refs[0]
    doc.set_rect(br, (100, 100, 10, 10))
    assert doc.rect(front_anchors_refs[0]) == old_front_0, "set_rect 背面时不应改变前面"
    assert doc.rect(br) == (100, 100, 10, 10)

    # remove 只删背面第 0 个
    n_front_before = len(t.faces["front"].align.anchors)
    n_back_before = len(t.faces["back"].align.anchors)
    doc.remove([back_anchors_refs[0]])
    assert len(t.faces["front"].align.anchors) == n_front_before, "remove 背面时改了前面"
    assert len(t.faces["back"].align.anchors) == n_back_before - 1


# ── I2: set_params 仅布局参数变化才重新生成 ──────────────────────


def test_set_params_no_regen_on_start_or_qtype():
    """改 start / qtype / labels 文字时不重新生成格子。"""
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    ref = [r for r in doc.regions("front") if r.kind == "objective"][0]
    blk = doc.template.objective[ref.index]
    old_cells = [row[:] for row in blk.grid.cells]

    # 改 start
    doc.set_params(ref, {"start": 100})
    assert blk.start == 100
    assert blk.grid.cells == old_cells, "改 start 不应重新生成格子"

    # 改 qtype
    doc.set_params(ref, {"qtype": "multiple"})
    assert blk.qtype == "multiple"
    assert blk.grid.cells == old_cells, "改 qtype 不应重新生成格子"

    # 改 labels（相同数量，不同文字）
    doc.set_params(ref, {"labels": ["甲", "乙", "丙", "丁", "戊"]})
    assert blk.grid.labels == ["甲", "乙", "丙", "丁", "戊"]
    assert blk.grid.cells == old_cells, "改 labels 文字不应重新生成格子"


def test_exam_no_set_params_keeps_vertical():
    """exam_no 修改位数时保持纵向排列。"""
    from scan_answer.template.doc import TemplateDoc, _infer_horizontal

    doc = TemplateDoc.load(TEMPLATE_DIR)
    ref = [r for r in doc.regions("front") if r.kind == "exam_no"][0]

    # 199 考号格是纵向排列
    horiz_before = _infer_horizontal(doc.template.exam_no.grid.cells)

    doc.set_params(ref, {"n_digits": 15})
    assert len(doc.template.exam_no.grid.cells) == 15
    horiz_after = _infer_horizontal(doc.template.exam_no.grid.cells)
    assert horiz_after == horiz_before, f"方向改变: {horiz_before} → {horiz_after}"


# ── I3: version 回滚 ────────────────────────────────────────────


def test_save_version_rollback_on_error(app_home, monkeypatch):
    """save_template 失败时 version 和 dirty 不变。"""
    from scan_answer.template import doc as doc_module
    from scan_answer.template.doc import TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    doc.add("field", "front", (100, 100, 20, 10))
    old_version = doc.template.version
    old_dirty = doc.dirty

    # monkeypatch save_template to raise（在 doc 模块的命名空间中替换）
    def _fail(*a, **k):
        raise OSError("模拟写入失败")
    monkeypatch.setattr(doc_module, "save_template", _fail)

    with pytest.raises(OSError, match="模拟写入失败"):
        doc.save(app_home / "fail_tpl")

    assert doc.template.version == old_version, f"version 未回滚: {doc.template.version}"
    assert doc.dirty == old_dirty, f"dirty 改变了"


# ── I4: restore() 设置 dirty ────────────────────────────────────


def test_restore_sets_dirty():
    """add → save → undo 后 dirty 应为 True。"""
    from scan_answer.template.doc import TemplateDoc, UndoStack

    doc = TemplateDoc.load(TEMPLATE_DIR)
    stack = UndoStack(doc)

    # 初始状态 dirty=False
    assert not doc.dirty

    stack.push("add field")
    doc.add("field", "front", (100, 100, 20, 10))
    assert doc.dirty

    # 模拟保存成功后 dirty=False（直接设置，不实际写盘）
    doc.dirty = False
    assert not doc.dirty

    # undo 后 dirty 应为 True（模板与磁盘不同）
    stack.undo()
    assert doc.dirty, "undo 后 dirty 应为 True"

    # redo 后也应为 True
    doc.dirty = False
    stack.redo()
    assert doc.dirty, "redo 后 dirty 应为 True"


# ── exam_no / omr_field options_horizontal ──────────────────────


def test_exam_no_options_horizontal():
    """exam_no 的 params 支持 options_horizontal，set_params 改变方向时重新生成网格。"""
    from scan_answer.template.doc import RegionRef, TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    ref = RegionRef("exam_no", 0)
    p = doc.params(ref)
    assert "options_horizontal" in p
    old_n = p["n_digits"]
    old_horiz = p["options_horizontal"]

    # 翻转方向
    doc.set_params(ref, {"options_horizontal": not old_horiz})
    p2 = doc.params(ref)
    assert p2["options_horizontal"] == (not old_horiz)
    assert p2["n_digits"] == old_n  # 位数不变


def test_omr_field_options_horizontal():
    """omr_field 的 params 支持 options_horizontal；方向翻转重新生成网格；
    仅改 label/key 保持 cells 不变。"""
    from scan_answer.template.doc import RegionRef, TemplateDoc

    doc = TemplateDoc.load(TEMPLATE_DIR)
    # 通过 doc.add 创建 omr_field
    ref = doc.add("omr_field", "front", (50, 50, 30, 40))
    p = doc.params(ref)
    assert "options_horizontal" in p
    old_n = p["n_digits"]
    old_horiz = p["options_horizontal"]
    old_cells = [row[:] for row in doc.template.omr_fields[ref.index].grid.cells]

    # 翻转方向 → 重新生成网格
    doc.set_params(ref, {"options_horizontal": not old_horiz})
    p2 = doc.params(ref)
    assert p2["options_horizontal"] == (not old_horiz)
    assert p2["n_digits"] == old_n
    new_cells = doc.template.omr_fields[ref.index].grid.cells
    assert new_cells != old_cells, "翻转方向后 cells 应重新生成"

    # 仅改 label/key → cells 不变
    cells_before = [row[:] for row in doc.template.omr_fields[ref.index].grid.cells]
    doc.set_params(ref, {"key": "test_key", "label": "测试字段"})
    p3 = doc.params(ref)
    assert p3["key"] == "test_key"
    assert p3["label"] == "测试字段"
    cells_after = doc.template.omr_fields[ref.index].grid.cells
    assert cells_after == cells_before, "仅改 key/label 后 cells 应不变"
