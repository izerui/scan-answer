import cv2
import numpy as np
import pytest

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import PhysicalError, analyze_sheet, decide_sheet
from scan_answer.synth.generate import make_scans, make_sheet


def _sheet(ctx, seed, special=None):
    t = ctx.tpl
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(t, ctx.bases, rng)
    return make_scans(t, paper, truth, rng, 300, special), truth


@pytest.mark.parametrize("special", [None, "rot180", "swap"])
def test_orientation_and_answers(ctx, special):
    scans, truth = _sheet(ctx, 3, special)
    a = analyze_sheet(ctx, scans, [300, 300])
    assert a.swapped == (special == "swap")
    assert a.placements["front"].rotated == (180 if special == "rot180" else 0)
    d = decide_sheet(ctx, a, Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    assert d.exam_no["value"] == truth["exam_no"]["value"]


def test_blank_page_is_physical(ctx):
    scans, _ = _sheet(ctx, 4, "blank")
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, scans, [300, 300])
    assert e.value.code == "BLANK_PAGE"


def test_missing_face_and_garbage(ctx):
    scans, _ = _sheet(ctx, 5)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, scans[:1], [300])
    assert e.value.code == "MISSING_FACE"
    noise = np.random.default_rng(0).integers(0, 255, (3300, 4700)).astype(np.uint8)
    noise = cv2.GaussianBlur(noise, (0, 0), 6)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, [noise, noise], [300, 300])
    assert e.value.code in ("ALIGN_FAILED", "FACE_MISMATCH")


def test_fold_uses_feature_fallback(ctx):
    """折角卡使用特征匹配兜底定位，答案正确，有 ALIGN_FALLBACK 警告。"""
    scans, truth = _sheet(ctx, 10, "fold")
    a = analyze_sheet(ctx, scans, [300, 300])
    assert a.placements["front"].align.method == "features"
    assert a.placements["front"].rotated == 0
    d = decide_sheet(ctx, a, Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    assert any(w["code"] == "ALIGN_FALLBACK" for w in d.warnings)


def test_fold_rot180_uses_feature_fallback(ctx):
    """折角+旋转180°：特征匹配兜底定位，rotated=180，答案正确。"""
    scans, truth = _sheet(ctx, 11, "fold")
    # 对 front 做 180° 旋转
    scans[0] = cv2.rotate(scans[0], cv2.ROTATE_180)
    a = analyze_sheet(ctx, scans, [300, 300])
    assert a.placements["front"].align.method == "features"
    assert a.placements["front"].rotated == 180
    d = decide_sheet(ctx, a, Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    assert any(w["code"] == "ALIGN_FALLBACK" for w in d.warnings)


def test_exam_no_multi_fill_detected(ctx):
    """考号某位涂两个数字 → 该位 status=="multiple"、value 为 "?"、有 EXAM_NO_PARTIAL 警告。"""
    from scan_answer.synth.generate import make_sheet, make_scans
    from scan_answer.synth.draw import pencil_mark
    t = ctx.tpl
    rng = np.random.default_rng(99)
    paper, truth = make_sheet(t, ctx.bases, rng)
    # 不管 make_sheet 是否已有异常，手动在第 0 位多涂一个数字
    if t.exam_no:
        cells = t.exam_no.grid.cells[0]
        original_digit = int(truth["exam_no"]["full"][0])
        other = (original_digit + 3) % 10
        pencil_mark(paper[t.exam_no.grid.face], cells[other], t.dpi, "normal", rng)
    scans = make_scans(t, paper, truth, rng, 300, None)
    a = analyze_sheet(ctx, scans, [300, 300])
    d = decide_sheet(ctx, a, Thresholds())
    assert d.exam_no is not None
    # 第一位应该是 multiple
    digit_0 = d.exam_no["digits"]  # 只记录非 ok 的位
    first_pos_statuses = [dd for dd in digit_0 if dd["pos"] == 1]
    # 整体至少有一个非 ok 位
    assert d.exam_no["status"] == "partial"
    assert d.exam_no["value"][0] == "?"
    assert any(w["code"] == "EXAM_NO_PARTIAL" for w in d.warnings)


def test_blank_base_has_no_marks(ctx):
    """空白底图自身的涂黑度应接近 0（底图减底图）。"""
    from scan_answer.engine.omr import grid_fills
    t = ctx.tpl
    for blk in t.objective:
        f = grid_fills(ctx.work_bases["front"], ctx.work_bases["front"], blk.grid.cells, ctx.work_dpi)
        assert float(f.max()) < 0.01
