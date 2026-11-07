"""TemplateDoc 可编辑模板文档测试。"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"
PDF_PATH = ROOT / "templates" / "guanzong199" / "front.png"  # 用 PNG 当作底图来源


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
    """单面模板全流程：导入 → 自动定位 → 加客观题 → snap → 保存 → 生成 → 识别。"""
    import cv2
    from scan_answer.template.auto import import_base
    from scan_answer.template.doc import TemplateDoc

    # 用 199 正面 PNG 作为单面底图
    gray = cv2.imread(str(TEMPLATE_DIR / "front.png"), cv2.IMREAD_GRAYSCALE)
    assert gray is not None
    ib = import_base(gray, 300)

    doc = TemplateDoc.new("单面测试", {"front": ib})

    # 自动定位方式应为 anchors，有 4 个定位块
    assert doc.template.faces["front"].align.method == "anchors"
    assert len(doc.template.faces["front"].align.anchors) == 4

    # sync 应该有 1 组
    assert len(doc.template.sync) >= 1

    # 只有 front 面
    assert list(doc.template.faces.keys()) == ["front"]

    # 加一个客观题块
    ref = doc.add("objective", "front", (30, 60, 80, 50))
    result = doc.snap(ref)
    # snap 不一定通过（新建的块是生成的网格），但不报错就行

    # 保存
    saved = doc.save(tmp_path / "single_face_tpl")

    # 生成 2 张卡并识别
    from scan_answer.synth.generate import generate_dataset
    from scan_answer.template import load_template

    t, bases = load_template(saved)
    ds = tmp_path / "dataset"
    generate_dataset(t, bases, ds, 2, seed=42, out_dpi=200)

    from scan_answer.batch import run_batch
    bd = run_batch(saved, ds, tmp_path / "out", batch_id="sf1")

    # 检查识别结果（result.json 在 students/<name>/result.json）
    import json
    result_files = list(bd.glob("students/*/result.json"))
    assert len(result_files) == 2, f"结果文件数: {len(result_files)}，目录内容: {list(bd.rglob('*'))}"
    for rf in result_files:
        r = json.loads(rf.read_text(encoding="utf-8"))
        # 单面模板识别成功：有 objective 字段，scan.faces.front 存在
        assert "objective" in r, f"缺少 objective: {list(r.keys())}"
        assert "front" in r.get("scan", {}).get("faces", {}), f"缺少 front 面"
