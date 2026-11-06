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


def test_blank_base_has_no_marks(ctx):
    """空白底图自身的涂黑度应接近 0（底图减底图）。"""
    from scan_answer.engine.omr import grid_fills
    t = ctx.tpl
    for blk in t.objective:
        f = grid_fills(ctx.work_bases["front"], ctx.work_bases["front"], blk.grid.cells, ctx.work_dpi)
        assert float(f.max()) < 0.01
