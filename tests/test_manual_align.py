"""人工定位：自动定位失败（如倾斜超过上限）的卡，由操作员点出定位点后重新识别。"""
import cv2
import numpy as np
import pytest

from scan_answer.engine.align import manual_alignment, reference_points
from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import PhysicalError, analyze_sheet, decide_sheet
from scan_answer.output.jsonfile import read_json
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.base import SheetImages
from scan_answer.synth.generate import make_sheet

from conftest import TEMPLATE_DIR


def _tilted(ctx, seed, deg, back_deg=None, pad=300):
    """内容整体转 deg 度（背面可单独指定；四周留白，定位块不出界），返回 (图像列表, 每面的像素变换, 标准答案)。"""
    t = ctx.tpl
    paper, truth = make_sheet(t, ctx.bases, np.random.default_rng(seed))
    images, transforms = [], []
    for face in t.faces:
        angle = deg if face == "front" or back_deg is None else back_deg
        img = paper[face]
        h, w = img.shape
        M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        M[:, 2] += pad
        images.append(cv2.warpAffine(img, M, (w + 2 * pad, h + 2 * pad), borderValue=250))
        transforms.append(np.vstack([M, [0, 0, 1]]))
    return images, transforms, truth


def _clicks(ctx, face, M, skip=()):
    """模拟操作员按顺序点击该面的参照点（skip 中的序号不点）。"""
    s = ctx.tpl.dpi / 25.4
    out = []
    for k, (_label, (x, y)) in enumerate(reference_points(ctx.tpl.faces[face].align)):
        out.append(None if k in skip else (M @ np.array([x * s, y * s, 1.0]))[:2].tolist())
    return out


def _assert_answers(ctx, a, truth):
    d = decide_sheet(ctx, a, Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    assert d.exam_no["value"] == truth["exam_no"]["value"]
    return d


def test_reference_points_follow_alignment_spec(ctx):
    front = reference_points(ctx.tpl.faces["front"].align)
    assert [label for label, _ in front] == ["定位块 1", "定位块 2", "定位块 3", "定位块 4"]
    back = reference_points(ctx.tpl.faces["back"].align)
    assert len(back) == 4 * len(ctx.tpl.faces["back"].align.frames)
    assert back[0][0] == "外框 1 左上"


def test_manual_alignment_needs_three_points(ctx):
    spec = ctx.tpl.faces["front"].align
    assert not manual_alignment(spec, [[100, 100], [900, 100], None, None]).ok
    assert not manual_alignment(spec, [[100, 100]]).ok  # 点数与参照点数不一致
    res = manual_alignment(spec, [[100, 100], [4600, 120], [4580, 3200], None])
    assert res.ok and res.method == "manual"


def test_front_beyond_limit_manual_front_and_back_stays_automatic(ctx):
    images, transforms, truth = _tilted(ctx, 31, 12.0, back_deg=1.0)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, images, [300, 300])
    assert e.value.code == "SKEW_TOO_LARGE"
    manual = {"front": {"image": 0, "points": _clicks(ctx, "front", transforms[0])}}
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert a.placements["front"].align.method == "manual"
    assert abs(abs(a.placements["front"].align.skew_deg) - 12.0) < 0.3
    assert a.placements["back"].align.method == "frames" and a.placements["back"].source == 1
    d = _assert_answers(ctx, a, truth)
    assert [w["field"] for w in d.warnings if w["code"] == "ALIGN_MANUAL"] == ["scan.faces.front"]


def test_remaining_face_that_also_fails_asks_for_manual(ctx):
    images, transforms, _ = _tilted(ctx, 30, 12.0)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, images, [300, 300], manual={"front": {"image": 0, "points": _clicks(ctx, "front", transforms[0])}})
    assert e.value.code == "SKEW_TOO_LARGE" and "手动定位" in e.value.msg


def test_both_faces_manual_with_three_points_each(ctx):
    images, transforms, truth = _tilted(ctx, 32, -12.0)
    manual = {"front": {"image": 0, "points": _clicks(ctx, "front", transforms[0], skip=(3,))},
              "back": {"image": 1, "points": _clicks(ctx, "back", transforms[1], skip=range(3, 99))}}
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert {f: p.align.method for f, p in a.placements.items()} == {"front": "manual", "back": "manual"}
    assert not a.swapped
    _assert_answers(ctx, a, truth)


def test_manual_points_on_upside_down_image(ctx):
    images, transforms, truth = _tilted(ctx, 33, 10.0)
    h, w = images[0].shape
    images[0] = cv2.rotate(images[0], cv2.ROTATE_180)
    flip = np.array([[-1, 0, w - 1], [0, -1, h - 1], [0, 0, 1]], dtype=np.float64)
    manual = {"front": {"image": 0, "points": _clicks(ctx, "front", flip @ transforms[0])},
              "back": {"image": 1, "points": _clicks(ctx, "back", transforms[1])}}
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert a.placements["front"].rotated == 180
    _assert_answers(ctx, a, truth)


def test_wrong_click_order_is_rejected(ctx):
    images, transforms, _ = _tilted(ctx, 34, 12.0)
    pts = _clicks(ctx, "front", transforms[0])
    pts[0], pts[2] = pts[2], pts[0]
    manual = {"front": {"image": 0, "points": pts}, "back": {"image": 1, "points": _clicks(ctx, "back", transforms[1])}}
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert e.value.code == "ALIGN_FAILED" and "人工定位" in e.value.msg


def test_same_image_for_two_faces_is_rejected(ctx):
    images, transforms, _ = _tilted(ctx, 35, 12.0)
    manual = {"front": {"image": 0, "points": _clicks(ctx, "front", transforms[0])},
              "back": {"image": 0, "points": _clicks(ctx, "back", transforms[1])}}
    with pytest.raises(ValueError):
        analyze_sheet(ctx, images, [300, 300], manual=manual)


def test_pipeline_manual_relocate_replaces_physical_error(ctx, tmp_path):
    images, transforms, truth = _tilted(ctx, 36, 12.0)
    sheet = SheetImages(source="tilt", names=["tilt_1.png", "tilt_2.png"], dpis=[300.0, 300.0], images=images)
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(iter_source([sheet]))
        assert s.sheets[1]["status"] == "physical" and s.sheets[1]["physical"] == "SKEW_TOO_LARGE"
        manual = {f: {"image": i, "points": _clicks(ctx, f, transforms[i])} for i, f in enumerate(("front", "back"))}
        ev = p.relocate(1, manual)
        assert ev.ok and ev.rescan
        assert sorted(x.name for x in (s.batch_dir / "raw").glob("0001_*")) == ["0001_tilt_1.png", "0001_tilt_2.png"]
        r = read_json(s.batch_dir / s.sheets[1]["result"])
        assert r["scan"]["faces"]["front"]["align_method"] == "manual"
        assert r["student"]["exam_no"]["value"] == truth["exam_no"]["value"]


def test_pipeline_bad_manual_points_keep_old_evidence(ctx, tmp_path):
    images, transforms, _ = _tilted(ctx, 37, 12.0)
    sheet = SheetImages(source="tilt", names=["tilt_1.png", "tilt_2.png"], dpis=[300.0, 300.0], images=images)
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(iter_source([sheet]))
        pts = _clicks(ctx, "front", transforms[0])
        pts[0], pts[2] = pts[2], pts[0]
        ev = p.relocate(1, {"front": {"image": 0, "points": pts}})
        assert not ev.ok
        assert s.sheets[1]["status"] == "physical" and s.sheets[1]["physical"] == "SKEW_TOO_LARGE"
        assert sorted(x.name for x in (s.batch_dir / "raw").glob("0001_*")) == ["0001_tilt_1.png", "0001_tilt_2.png"]


class iter_source:
    def __init__(self, items):
        self.items = items

    def sheets(self):
        yield from self.items


def test_remaining_face_falls_back_to_features_when_anchor_match_scores_low(ctx):
    """另一面的定位块被整体挪位（几何仍自洽，内容对不上）：与全自动流程一样改用特征匹配。"""
    images, transforms, truth = _tilted(ctx, 38, 1.0, back_deg=12.0)
    s = ctx.tpl.dpi / 25.4
    front = images[0]
    M = transforms[0]
    for x, y, w, h in ctx.tpl.faces["front"].align.anchors:
        pts = np.array([[x - 1, y - 1, 1], [x + w + 1, y + h + 1, 1]]) * [s, s, 1]
        (x0, y0), (x1, y1) = [(M @ p)[:2] for p in pts]
        x0, x1, y0, y1 = int(min(x0, x1)), int(max(x0, x1)), int(min(y0, y1)), int(max(y0, y1))
        block = front[y0:y1, x0:x1].copy()
        front[y0:y1, x0:x1] = 250
        dx, dy = int(-12 * s), int(20 * s)  # 定位块整体挪位（几何仍自洽），页面内容不动
        front[y0 + dy:y1 + dy, x0 + dx:x1 + dx] = block
    manual = {"back": {"image": 1, "points": _clicks(ctx, "back", transforms[1])}}
    a = analyze_sheet(ctx, images, [300, 300], manual=manual)
    assert a.placements["front"].align.method == "features"
    _assert_answers(ctx, a, truth)
