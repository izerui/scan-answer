"""忽略区：模板编辑器自动检测时当作白纸；识别时整页比对与特征匹配兜底不看它。"""
import copy
from pathlib import Path

import cv2
import numpy as np
import pytest

import scan_answer.template.doc as doc_module
from scan_answer.engine.align import align_features
from scan_answer.engine.recognize import TemplateContext, _score
from scan_answer.template.auto import auto_frames
from scan_answer.template.doc import TemplateDoc
from scan_answer.template.io import load_template
from scan_answer.template.layout import ignore_mask, mask_ignored
from scan_answer.template.model import Region

B199 = Path(__file__).resolve().parents[1] / "templates" / "guanzong199"


@pytest.fixture(scope="module")
def t199():
    return load_template(B199)


def test_ignore_mask_geometry(t199):
    t, _ = t199
    t = copy.deepcopy(t)
    t.ignore = [Region("front", (10.0, 20.0, 5.0, 4.0)), Region("back", (0.0, 0.0, 1.0, 1.0))]
    mask = ignore_mask(t, "front", (400, 300), 25.4)  # 1 px = 1 mm
    assert mask is not None and mask.sum() == 5 * 4 and mask[20:24, 10:15].all()
    assert ignore_mask(t, "front", (10, 10), 25.4) is None  # 完全在图外
    t.ignore = []
    assert ignore_mask(t, "front", (400, 300), 25.4) is None


def test_ignored_block_is_not_detected_as_frame(t199):
    """大块深色阴影（如“阴影部分请勿作答”）会被认成外框；框成忽略区后自动检测不再理会。"""
    t, _ = t199
    t = copy.deepcopy(t)
    dpi = 100
    img = np.full((int(297 / 25.4 * dpi), int(210 / 25.4 * dpi)), 255, np.uint8)
    s = dpi / 25.4
    cv2.rectangle(img, (int(30 * s), int(150 * s)), (int(180 * s), int(250 * s)), 0, int(2 * s))
    assert auto_frames(img, dpi, (210.0, 297.0))
    t.ignore = [Region("front", (25.0, 145.0, 160.0, 110.0))]
    assert not auto_frames(mask_ignored(img, t, "front", dpi), dpi, (210.0, 297.0))


def test_editor_auto_detection_masks_ignore(monkeypatch):
    doc = TemplateDoc.load(B199)
    rect = doc.template.ignore[0].rect
    seen = {}

    def fake_frames(image, dpi, paper):
        s = dpi / 25.4
        x, y, w, h = rect
        seen["patch"] = image[int(y * s) + 2:int((y + h) * s) - 2, int(x * s) + 2:int((x + w) * s) - 2]
        return []

    monkeypatch.setattr(doc_module, "auto_frames", fake_frames)
    assert doc.auto_align("front", "frames") == "未检测到外框"
    assert (seen["patch"] == 255).all()  # 忽略区已被当作白纸


def test_score_ignores_scribbles_in_ignore_region(t199):
    """忽略区里的大块涂写不拉低整页比对分数。"""
    t, bases = t199
    plain = copy.deepcopy(t)
    plain.ignore = []
    card = bases["front"].copy()
    s = t.dpi / 25.4
    x, y, w, h = t.ignore[0].rect
    card[int(y * s):int((y + h) * s), int(x * s):int((x + w) * s)] = 0
    H = np.diag([1 / s, 1 / s, 1.0])  # 像素 -> mm，图即底图
    with_ignore = _score(TemplateContext(t, bases), "front", card, H)
    without = _score(TemplateContext(plain, bases), "front", card, H)
    assert with_ignore > without
    clean = [_score(TemplateContext(x, bases), "front", bases["front"], H) for x in (t, plain)]
    assert abs(clean[0] - clean[1]) < 0.01  # 卡面没涂写时，有无忽略区分数基本一样


def test_align_features_accepts_base_ignore(t199):
    t, bases = t199
    ctx = TemplateContext(t, bases)
    res = align_features(bases["front"], bases["front"], t.dpi, t.dpi, base_ignore=ctx.base_ignore["front"])
    assert res.ok and res.residual_mm < 1.0
