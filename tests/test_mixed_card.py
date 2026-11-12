"""混卡检测：模板标识区（identity）的读写、校验、识别通过 / 拒绝、编辑器画标识区。

阈值实测（engine/recognize.py 的 IDENTITY_MIN_SCORE = 0.75，100 DPI、σ=1 模糊、±1.5 mm 平移搜索）：
  同模板（6 个内置模板的底图自身、噪声 σ=25/50、模糊 σ=1.5/3、旋转 1.5°/3° + 平移、JPEG q30、
    对比度减弱、200 DPI、组合扰动）：全部标识区 0.940–0.999（最低为英语二“（二）”加 σ=50 噪声）
  模板 ← 卡（各标识区分数）：
    英语一 ← 英语二 0.351 / 0.444    英语一 ← 政治 0.325 / 0.053
    英语二 ← 英语一 0.361 / 0.605    英语二 ← 政治 0.321 / 0.070
    政治 ← 英语一 0.391 / 0.172      政治 ← 英语二 0.405 / 0.163
"""
from __future__ import annotations

import copy
import itertools
import json
import shutil
from pathlib import Path

import cv2
import numpy as np
import pytest

from scan_answer.engine.recognize import (IDENTITY_MIN_SCORE, PHYSICAL_CODES, PhysicalError, TemplateContext,
                                          WORK_DPI, analyze_sheet, identity_scores, oriented, place_faces)
from scan_answer.engine.align import warp_to_template
from scan_answer.template.io import load_template, save_template, template_from_dict, template_to_dict
from scan_answer.template.model import Region
from scan_answer.template.validate import validate_template

ROOT = Path(__file__).resolve().parents[1]
NAMES = ["kaoyan_eng1", "kaoyan_eng2", "kaoyan_math1", "kaoyan_politics", "kaoyan_cs408", "guanzong199"]


@pytest.fixture(scope="module")
def builtin():
    return {n: load_template(ROOT / "templates" / n) for n in NAMES}


@pytest.fixture(scope="module")
def contexts(builtin):
    return {n: TemplateContext(*builtin[n]) for n in NAMES}


def _card(builtin, name: str) -> list[np.ndarray]:
    """用模板的空白底图充当“扫描图”。"""
    bases = builtin[name][1]
    return [bases["front"], bases["back"]]


def _scores(ctx: TemplateContext, images: list[np.ndarray]) -> list[float | None]:
    placements, _ = place_faces(ctx, images, [300.0] * len(images))
    work = {f: warp_to_template(oriented(images[p.source], p.rotated), p.align.H, ctx.tpl.paper, WORK_DPI)
            for f, p in placements.items()}
    return identity_scores(ctx, work, {f for f, p in placements.items() if p.align.method != "derived"})


# ── 扰动 ──────────────────────────────────────────────────────────

def _perturb(img: np.ndarray, kind: str, rng: np.random.Generator) -> np.ndarray:
    if kind == "noise":
        return np.clip(img.astype(np.float32) + rng.normal(0, 25, img.shape), 0, 255).astype(np.uint8)
    if kind == "blur":
        return cv2.GaussianBlur(img, (0, 0), 1.5)
    if kind == "jpeg":
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])
        return cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    if kind == "affine":  # 旋转 1.5°、缩放 0.99、平移约 2 mm
        h, w = img.shape
        m = cv2.getRotationMatrix2D((w / 2, h / 2), 1.5, 0.99)
        m[:, 2] += (25, -18)
        return cv2.warpAffine(img, m, (w, h), borderValue=255)
    if kind == "all":
        for k in ("affine", "blur", "noise", "jpeg"):
            img = _perturb(img, k, rng)
        return img
    return img


# ── 模型读写与校验 ────────────────────────────────────────────────

def test_old_template_without_identity_loads(builtin):
    d = template_to_dict(builtin["guanzong199"][0])
    d.pop("identity")
    t = template_from_dict(d)
    assert t.identity == []
    assert validate_template(t) == []


def test_identity_roundtrip(builtin, tmp_path):
    t, bases = copy.deepcopy(builtin["kaoyan_eng1"])
    assert [r.face for r in t.identity] == ["front", "front"]
    d = template_to_dict(t)
    assert d["identity"][0] == {"face": "front", "rect": (90.5, 24.8, 13.0, 7.5)}
    assert template_from_dict(json.loads(json.dumps(d))).identity == t.identity
    t.identity.append(Region("back", (10.0, 10.0, 20.0, 5.0)))
    save_template(t, tmp_path / "tpl", bases)
    t2, _ = load_template(tmp_path / "tpl")
    assert t2.identity == t.identity


def test_identity_validation(builtin):
    t = copy.deepcopy(builtin["kaoyan_eng1"][0])
    t.identity = [Region("front", (410.0, 10.0, 20.0, 5.0)), Region("front", (10.0, 10.0, 0.0, 5.0))]
    msgs = validate_template(t)
    assert "标识区1：超出纸面" in msgs and "标识区2：宽或高不为正" in msgs
    del t.faces["back"]
    t.identity = [Region("back", (10.0, 10.0, 20.0, 5.0))]
    assert any("标识区1：引用了不存在的面" in m for m in validate_template(t))
    t.identity = [Region("side", (10.0, 10.0, 20.0, 5.0))]
    assert any("引用了不存在的面" in m for m in validate_template(t))


def test_all_builtin_templates_have_identity(builtin):
    for name, (t, _bases) in builtin.items():
        assert t.identity, name
        assert validate_template(t) == [], name


# ── 识别：通过与拒绝 ──────────────────────────────────────────────

def test_wrong_card_code_registered():
    assert "WRONG_CARD" in PHYSICAL_CODES
    from scan_answer.app.results import PHYSICAL_TEXT
    from scan_answer.ui.pages.scan_page import RELOCATABLE
    assert PHYSICAL_TEXT["WRONG_CARD"] == "别科的卡（混卡）"
    assert "WRONG_CARD" not in RELOCATABLE  # 手动定位修正不了混卡


@pytest.mark.parametrize("name", ["kaoyan_eng1", "kaoyan_eng2", "kaoyan_politics"])
@pytest.mark.parametrize("kind", ["clean", "noise", "blur", "jpeg", "affine", "all"])
def test_same_card_passes_with_perturbation(builtin, contexts, name, kind):
    rng = np.random.default_rng(7)
    images = [_perturb(g, kind, rng) for g in _card(builtin, name)]
    scores = _scores(contexts[name], images)
    assert scores and all(s is not None and s >= 0.93 for s in scores), scores
    analyze_sheet(contexts[name], images, [300.0, 300.0])  # 不抛异常


@pytest.mark.parametrize("tpl,card", [(a, b) for a, b in itertools.permutations(
    ["kaoyan_eng1", "kaoyan_eng2", "kaoyan_politics"], 2)])
def test_similar_cards_rejected(builtin, contexts, tpl, card):
    scores = _scores(contexts[tpl], _card(builtin, card))
    # 每个标识区都明显低于阈值（实测最高 0.605）
    assert scores and all(s is not None and s < 0.65 for s in scores), scores
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(contexts[tpl], _card(builtin, card), [300.0, 300.0])
    assert e.value.code == "WRONG_CARD"
    assert e.value.msg == "卡面标识与模板不符，可能混入了其他科目或版本的答题卡"


def test_similar_cards_rejected_with_perturbation(builtin, contexts):
    rng = np.random.default_rng(11)
    images = [_perturb(g, "all", rng) for g in _card(builtin, "kaoyan_eng2")]
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(contexts["kaoyan_eng1"], images, [300.0, 300.0])
    assert e.value.code == "WRONG_CARD"


def test_builtin_cross_matrix(builtin, contexts):
    """6×6：对角线通过，非对角线全部拒绝（原有的 FACE_MISMATCH / BLANK_PAGE 或新增的 WRONG_CARD）。"""
    result = {}
    for a, b in itertools.product(NAMES, NAMES):
        try:
            analyze_sheet(contexts[a], _card(builtin, b), [300.0, 300.0])
            result[a, b] = "ok"
        except PhysicalError as e:
            result[a, b] = e.code
    for (a, b), code in result.items():
        assert (code == "ok") == (a == b), (a, b, code)
    # 版面几乎相同、此前会被照常接受的三种卡，现在由标识区拒绝
    for a, b in itertools.permutations(["kaoyan_eng1", "kaoyan_eng2", "kaoyan_politics"], 2):
        assert result[a, b] == "WRONG_CARD", (a, b)


def test_template_without_identity_does_not_check(builtin):
    t, bases = copy.deepcopy(builtin["kaoyan_eng1"])
    t.identity = []
    analyze_sheet(TemplateContext(t, bases), _card(builtin, "kaoyan_eng2"), [300.0, 300.0])  # 旧行为：照常接受


def test_unplaced_or_blank_identity_skipped(builtin):
    t, bases = copy.deepcopy(builtin["kaoyan_eng1"])
    t.identity = [Region("front", (5.0, 100.0, 4.0, 4.0))]  # 底图该处没有印刷内容：不比较
    ctx = TemplateContext(t, bases)
    assert ctx.identity_bases[0][1] is None
    assert _scores(ctx, _card(builtin, "kaoyan_eng2")) == [None]
    # 推算定位（不在 faces 中）的面不比较
    ctx = TemplateContext(*builtin["kaoyan_eng1"])
    work = {"front": warp_to_template(bases["front"], np.linalg.inv(np.diag([300 / 25.4, 300 / 25.4, 1.0])),
                                      t.paper, WORK_DPI)}
    assert identity_scores(ctx, work, set()) == [None, None]
    assert all(s >= IDENTITY_MIN_SCORE for s in identity_scores(ctx, work, {"front"}))


def test_blank_identity_on_card_rejected(builtin, contexts):
    """卡面标识区一片空白（底图有字）也判混卡。"""
    images = [g.copy() for g in _card(builtin, "kaoyan_eng1")]
    s = 300 / 25.4
    for r in contexts["kaoyan_eng1"].tpl.identity:
        x, y, w, h = r.rect
        images[0][int((y - 2) * s):int((y + h + 2) * s), int((x - 2) * s):int((x + w + 2) * s)] = 255
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(contexts["kaoyan_eng1"], images, [300.0, 300.0])
    assert e.value.code == "WRONG_CARD"


def test_trial_reports_wrong_card(builtin, tmp_path):
    from scan_answer.app.trial import trial_recognize
    files = []
    for face, img in zip(("front", "back"), _card(builtin, "kaoyan_eng2")):
        p = tmp_path / f"eng2_{face}.png"
        cv2.imwrite(str(p), img)
        files.append(str(p))
    t, bases = builtin["kaoyan_eng1"]
    [sheet] = trial_recognize(t, bases, [files])
    assert not sheet.ok and sheet.message.startswith("WRONG_CARD: ")


# ── 编辑器 ────────────────────────────────────────────────────────

def test_doc_identity_add_edit_remove_undo(builtin, tmp_path):
    from scan_answer.template.doc import KIND_LABELS, NO_IDENTITY_NOTICE, TemplateDoc, UndoStack
    src = tmp_path / "eng1"
    shutil.copytree(ROOT / "templates" / "kaoyan_eng1", src)
    doc = TemplateDoc.load(src)
    assert KIND_LABELS["identity"] == "科目标识区"
    assert [r.kind for r in doc.regions("front")].count("identity") == 2
    assert doc.notices() == []
    stack = UndoStack(doc)
    stack.push("删除标识区")
    doc.remove([r for r in doc.regions("front") if r.kind == "identity"])
    assert doc.template.identity == [] and doc.notices() == [NO_IDENTITY_NOTICE]
    assert doc.problems() == []  # 没有标识区不阻止保存
    stack.undo()
    assert len(doc.template.identity) == 2
    stack.push("添加标识区")
    ref = doc.add("identity", "back", (3.0, 120.0, 8.0, 8.0))
    assert doc.title(ref) == "科目标识区 3" and doc.face_of(ref) == "back"
    doc.set_rect(ref, (4.0, 120.0, 8.0, 8.0))
    assert doc.rect(ref) == (4.0, 120.0, 8.0, 8.0)
    # 背面这一块是空白：提醒但不阻止保存
    assert any("科目标识区3里几乎没有印刷内容" in m for m in doc.notices())
    assert doc.problems() == []
    stack.undo()
    assert len(doc.template.identity) == 2
    stack.redo()
    assert doc.template.identity[2].rect == (4.0, 120.0, 8.0, 8.0)


def test_page_draw_identity_and_save_notice(qtbot, app_home, no_dialogs, tmp_path):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtCore import Qt

    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.template_page import TemplatePage

    src = tmp_path / "eng2"
    shutil.copytree(ROOT / "templates" / "kaoyan_eng2", src)
    page = TemplatePage(AppSettings(template_dirs=[str(tmp_path)]))
    qtbot.addWidget(page)
    page.resize(1400, 860)
    page.show()
    assert page.open_editor(src)
    groups = {page.tree.topLevelItem(i).text(0): page.tree.topLevelItem(i)
              for i in range(page.tree.topLevelItemCount())}
    assert groups["科目标识区（查别科的卡）"].text(1) == "2"
    # 删除全部标识区后保存：不阻止，状态行提醒
    page.canvas.select([r for r in page.doc.regions("front") if r.kind == "identity"])
    page.delete_selected()
    assert page.doc.template.identity == []
    assert page.save() is True
    assert "未设置科目标识区，发现不了别科的卡（混卡）" in page.msg_label.text()
    assert not [m for k, m in no_dialogs if k == "warning"]
    # 用区域工具画一个标识区（副标题处）
    page.tool_btns["identity"].click()
    page.canvas.fit()
    vp = page.canvas.viewport()

    def send(kind, pos):
        QApplication.sendEvent(vp, QMouseEvent(kind, pos, vp.mapToGlobal(pos), Qt.MouseButton.LeftButton,
                                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

    p1 = page.canvas.mapFromScene(QPointF(89.6, 26.0))
    p2 = page.canvas.mapFromScene(QPointF(102.6, 33.6))
    send(QEvent.Type.MouseButtonPress, p1)
    send(QEvent.Type.MouseMove, p2)
    send(QEvent.Type.MouseButtonRelease, p2)
    assert len(page.doc.template.identity) == 1
    assert "已添加科目标识区" in page.msg_label.text()
    assert page.canvas.selected() == [next(r for r in page.doc.regions("front") if r.kind == "identity")]
    assert page.props._header.text().startswith("科目标识区")
    page.undo()
    assert page.doc.template.identity == []
    page.redo()
    assert len(page.doc.template.identity) == 1
    assert page.save() is True
    assert "未设置科目标识区" not in page.msg_label.text()
    t, _ = load_template(page.doc.directory)
    assert len(t.identity) == 1 and t.identity[0].face == "front"


# ── 部分标识区不符与操作员确认 ─────────────────────────────────────

def _scribble(contexts, name, images, index):
    """在第 index 个标识区里画满粗笔划（模拟学生写画），返回新图像。"""
    images = [g.copy() for g in images]
    s = 300 / 25.4
    r = contexts[name].tpl.identity[index]
    x, y, w, h = r.rect
    face = 0 if r.face == "front" else 1
    for k in range(0, int(w * s), 12):
        cv2.line(images[face], (int(x * s) + k, int(y * s)), (int(x * s) + k + 40, int((y + h) * s)), 0, 6)
    return images


def _codes(ctx, a):
    from scan_answer.engine.omr import Thresholds
    from scan_answer.engine.recognize import decide_sheet
    return {w["code"] for w in decide_sheet(ctx, a, Thresholds()).warnings}


def test_partial_identity_mismatch_is_warning_not_wrong_card(builtin, contexts):
    """两个标识区只有一个对不上（学生写画）：不判混卡，结果带 IDENTITY_PARTIAL 警告。"""
    ctx = contexts["kaoyan_eng1"]
    images = _scribble(contexts, "kaoyan_eng1", _card(builtin, "kaoyan_eng1"), 0)
    scores = _scores(ctx, images)
    assert scores[0] < IDENTITY_MIN_SCORE <= scores[1]
    a = analyze_sheet(ctx, images, [300.0, 300.0])
    assert a.identity == "partial" and "IDENTITY_PARTIAL" in _codes(ctx, a)


def test_confirm_identity_overrides_wrong_card(builtin, contexts):
    """操作员确认无误：跳过标识检查，结果带 IDENTITY_CONFIRMED 警告，并能写回/读回分析状态。"""
    from scan_answer.engine.state import analysis_from_dict, analysis_to_dict
    ctx = contexts["kaoyan_eng1"]
    images = _card(builtin, "kaoyan_eng2")
    with pytest.raises(PhysicalError):
        analyze_sheet(ctx, images, [300.0, 300.0])
    a = analyze_sheet(ctx, images, [300.0, 300.0], confirm_identity=True)
    assert a.identity == "confirmed" and "IDENTITY_CONFIRMED" in _codes(ctx, a)
    assert analysis_from_dict(analysis_to_dict(a)).identity == "confirmed"


def test_exception_item_offers_confirm_only_for_wrong_card(qtbot):
    from PySide6.QtWidgets import QPushButton
    from scan_answer.ui.pages.scan_page import ExceptionItem

    def labels(code):
        w = ExceptionItem(1, code, "", None, raw_ok=True)
        qtbot.addWidget(w)
        return [b.text() for b in w.findChildren(QPushButton)]

    assert {"是本科目的卡，照常识别", "手动定位"} <= set(labels("WRONG_CARD")) and "重新识别" not in labels("WRONG_CARD")
    assert "是本科目的卡，照常识别" not in labels("ALIGN_FAILED")


def test_pipeline_confirm_identity_reprocess(builtin, tmp_path):
    """正式流水线：混卡（WRONG_CARD）经“确认无误”重新识别后成功，结果带 IDENTITY_CONFIRMED。"""
    from scan_answer.output.jsonfile import read_json
    from scan_answer.pipeline.runner import BatchPipeline
    from scan_answer.pipeline.session import BatchSession
    from scan_answer.source.documents import DocumentSource
    src = tmp_path / "in"
    src.mkdir()
    for face, img in zip(("1", "2"), _card(builtin, "kaoyan_eng2")):
        cv2.imwrite(str(src / f"card_{face}.png"), img)
    s = BatchSession.create(tmp_path / "out", ROOT / "templates" / "kaoyan_eng1", batch_id="b")
    with BatchPipeline(s) as p:
        p.run(DocumentSource(src, s.faces))
        assert s.sheets[1]["status"] == "physical" and s.sheets[1]["physical"] == "WRONG_CARD"
        ev = p.reprocess(1, confirm_identity=True)
    assert ev.ok and s.sheets[1]["status"] == "ok"
    result = read_json(s.batch_dir / s.sheets[1]["result"])
    assert any(w["code"] == "IDENTITY_CONFIRMED" for w in result["warnings"])
