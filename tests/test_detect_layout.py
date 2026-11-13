"""自动识别版面：以仓库自带的 6 套模板为标准答案。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from scan_answer.app.settings import bundled_templates_dir
from scan_answer.template.detect_layout import detect_layout
from scan_answer.template.doc import TemplateDoc

from conftest import settle

TEMPLATES = ["guanzong199", "kaoyan_cs408", "kaoyan_eng1", "kaoyan_eng2", "kaoyan_math1", "kaoyan_politics"]


@pytest.fixture(scope="module", params=TEMPLATES)
def case(request):
    doc = TemplateDoc.load(bundled_templates_dir() / request.param)
    return request.param, doc.template, detect_layout(doc.bases, doc.template.dpi)


def _centre(r):
    return r[0] + r[2] / 2, r[1] + r[3] / 2


def _dist(a, b):
    (ax, ay), (bx, by) = _centre(a), _centre(b)
    return math.hypot(ax - bx, ay - by)


def _questions(blocks):
    return {b.start + i: (b.grid.face, row) for b in blocks for i, row in enumerate(b.grid.cells)}


def test_exam_no_matches_bundled(case):
    _, t, r = case
    assert r.exam_no is not None
    truth, got = t.exam_no.grid.cells, r.exam_no.grid.cells
    assert len(got) == len(truth) and all(len(row) == 10 for row in got)
    assert r.exam_no.grid.labels == [str(i) for i in range(10)]
    assert max(_dist(a, b) for ta, ga in zip(truth, got) for a, b in zip(ta, ga)) <= 1.0


def test_objective_matches_bundled(case):
    _, t, r = case
    truth, got = _questions(t.objective), _questions(r.objective)
    assert set(got) == set(truth)
    dists = []
    for no, (face, row) in truth.items():
        assert got[no][0] == face and len(got[no][1]) == len(row), no
        dists += [_dist(a, b) for a, b in zip(row, got[no][1])]
    assert np.mean(np.array(dists) <= 1.0) >= 0.95
    assert all(b.qtype == "single" for b in r.objective)


@pytest.mark.parametrize("name", ["kaoyan_cs408", "kaoyan_eng2", "kaoyan_politics"])
def test_subjective_areas_match_bundled(name):
    doc = TemplateDoc.load(bundled_templates_dir() / name)
    r = detect_layout(doc.bases, doc.template.dpi)
    truth = [(s.no, s.regions[0].face) for s in doc.template.subjective]
    assert [(s.no, s.regions[0].face) for s in r.subjective] == truth
    for s, ts in zip(r.subjective, doc.template.subjective):
        assert _dist(s.regions[0].rect, ts.regions[0].rect) <= 10.0


def test_blank_page_gives_empty_result():
    r = detect_layout({"front": np.full((3508, 4961), 255, np.uint8)}, 300)
    assert r.exam_no is None and not r.objective and not r.subjective
    assert r.notes


def test_front_only():
    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    r = detect_layout({"front": doc.bases["front"]}, 300)
    assert [b.name for b in r.objective] == ["1–15", "16–30", "31–45", "46–55"]
    assert all(b.grid.face == "front" for b in r.objective)
    assert all(s.regions[0].face == "front" for s in r.subjective)


def _new_199() -> TemplateDoc:
    from scan_answer.template.auto import ImportedBase
    src = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    faces = {f: ImportedBase(img, src.template.paper, 0.0, "anchors") for f, img in src.bases.items()}
    return TemplateDoc.new("199", faces)


def test_new_doc_auto_layout():
    doc = _new_199()
    t = doc.template
    assert len(t.exam_no.grid.cells) == 15
    assert [b.start for b in t.objective] == [1, 16, 31, 46]
    assert t.subjective and t.subjective[0].no == "56"
    assert "考号 15 位" in doc.layout_message and "选择题 1–55" in doc.layout_message
    assert doc.dirty


def test_auto_layout_button_confirms_and_undoes(qtbot, app_home, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.template_page import TemplatePage
    page = TemplatePage(AppSettings(template_dirs=[str(bundled_templates_dir())]))
    qtbot.addWidget(page)
    page._enter(_new_199(), fresh=True)
    page.doc.template.objective = []
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a) or QMessageBox.StandardButton.No)
    page.layout_btn.click()
    assert asked and page.doc.template.objective == []  # 还有考号和主观题，先问；选“否”不动
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.layout_btn.click()
    assert not page.editor_view.isEnabled()  # 识别版面在后台做：编辑区先锁住
    settle()
    assert len(page.doc.template.objective) == 4
    assert "选择题 1–55" in page.msg_label.text()
    page.undo_stack.undo()
    assert page.doc.template.objective == []


def test_review_notes_and_groups():
    doc = TemplateDoc.load(bundled_templates_dir() / "kaoyan_eng1")
    r = detect_layout(doc.bases, 300)
    assert [b.group for b in r.objective] == [5, 5, 5, 5, 0]
    assert any("41–45" in n and "选项数" in n for n in r.notes)


@pytest.mark.parametrize("name", TEMPLATES)
def test_auto_template_reads_filled_cards(name):
    """模拟考生按印刷框涂卡（用自带模板的格子位置涂），再用自动识别出的模板识别：选择题全部读对。"""
    from scan_answer.engine.omr import Thresholds
    from scan_answer.engine.recognize import (
        TemplateContext,
        analyze_sheet,
        decide_sheet,
    )
    from scan_answer.synth.generate import make_scans, make_sheet
    from scan_answer.template.auto import ImportedBase
    ref = TemplateDoc.load(bundled_templates_dir() / name)
    faces = {f: ImportedBase(img, ref.template.paper, 0.0, "anchors") for f, img in ref.bases.items()}
    auto = TemplateDoc.new("auto", faces)
    ctx = TemplateContext(auto.template, auto.bases)
    rng = np.random.default_rng(0)
    paper, truth = make_sheet(ref.template, ref.bases, rng)
    d = decide_sheet(ctx, analyze_sheet(ctx, make_scans(ref.template, paper, truth, rng, 300, None), [300, 300]),
                     Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    if name != "kaoyan_politics":
        # 政治的灰底考号列模拟涂得很淡，自带模板和自动模板读数都只在门槛附近
        assert d.exam_no["value"] == truth["exam_no"]["value"]


@pytest.mark.parametrize("name", TEMPLATES)
@pytest.mark.parametrize("seed", [100, 101])
def test_scanned_blank_card(name, seed):
    """空白卡经模拟扫描（倾斜、平移、透视、起伏、模糊、噪声、JPEG）再按导入流程纠偏后识别：
    考号位数、题号与选项数都与自带模板一致。整体上约九成多的模拟扫描能完全识别对，这里固定两个样本防退化。"""
    from scan_answer.synth import generate as gen
    from scan_answer.template.auto import import_base
    ref = TemplateDoc.load(bundled_templates_dir() / name)
    rng = np.random.default_rng(seed)
    bases = {f: import_base(gen.scanify(img, 300, 300, gen.random_params(rng), rng), 300).image
             for f, img in ref.bases.items()}
    r = detect_layout(bases, 300)
    assert r.exam_no is not None and len(r.exam_no.grid.cells) == len(ref.template.exam_no.grid.cells)
    want = {b.start + i: len(b.grid.labels) for b in ref.template.objective for i in range(len(b.grid.cells))}
    assert {b.start + i: len(b.grid.labels) for b in r.objective for i in range(len(b.grid.cells))} == want
    # 主观题作答区与干净底图上认出的一样多（扫描后“注意事项”等文字框不能被当成作答区）
    clean = detect_layout(ref.bases, 300)
    assert [s.regions[0].face for s in r.subjective] == [s.regions[0].face for s in clean.subjective]


def test_scanned_template_reads_exam_number():
    """用扫描的空白卡自动建模板，再读模拟涂卡：考号区是一行行重复的数字，读卡前的位置微调曾错开约半行，
    15 位全部读成“?”（数学卡、这一组样本）。"""
    from dataclasses import replace

    from scan_answer.engine.omr import Thresholds
    from scan_answer.engine.recognize import (
        TemplateContext,
        analyze_sheet,
        decide_sheet,
    )
    from scan_answer.synth import generate as gen
    from scan_answer.template.auto import import_base
    ref = TemplateDoc.load(bundled_templates_dir() / "kaoyan_math1")
    rng = np.random.default_rng(700)

    def params():
        p = gen.random_params(rng)  # 扫描仪按纸张幅面扫：不留白边
        return replace(p, margin_mm=0.0, rotate_deg=p.rotate_deg / 2, shift_mm=(p.shift_mm[0] / 4, p.shift_mm[1] / 4))

    faces = {f: import_base(gen.scanify(img, 300, 300, params(), rng), 300) for f, img in ref.bases.items()}
    doc = TemplateDoc.new("scanned", faces)
    ctx = TemplateContext(doc.template, doc.bases)
    rng2 = np.random.default_rng(700001)
    paper, truth = gen.make_sheet(ref.template, ref.bases, rng2)
    d = decide_sheet(ctx, analyze_sheet(ctx, gen.make_scans(ref.template, paper, truth, rng2, 300, None), [300, 300]),
                     Thresholds())
    assert d.exam_no["value"] == truth["exam_no"]["value"]
