"""自动识别版面：以仓库自带的 6 套模板为标准答案。"""
from __future__ import annotations

import math

import numpy as np
import pytest

from scan_answer.app.settings import bundled_templates_dir
from scan_answer.template.detect_layout import detect_layout
from scan_answer.template.doc import TemplateDoc

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
    name, t, r = case
    assert r.exam_no is not None
    truth, got = t.exam_no.grid.cells, r.exam_no.grid.cells
    assert len(got) == len(truth) and all(len(row) == 10 for row in got)
    assert r.exam_no.grid.labels == [str(i) for i in range(10)]
    if name == "kaoyan_cs408":
        # 自带模板的考号列距比印刷的小 0.14 mm，越往右越偏（最后一位偏约 2 mm，已偏出印刷数字），只比前 7 位
        truth, got = truth[:7], got[:7]
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
