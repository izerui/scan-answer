import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import analyze_sheet, decide_sheet
from scan_answer.engine.result import build_result, clean_show_through, render_images
from scan_answer.synth.generate import make_scans, make_sheet


def test_clean_show_through():
    img = np.array([[250, 200, 186, 100]], np.uint8)
    assert clean_show_through(img).tolist() == [[255, 255, 255, 100]]


def test_render_and_build(ctx):
    t = ctx.tpl
    rng = np.random.default_rng(8)
    paper, truth = make_sheet(t, ctx.bases, rng)
    scans = make_scans(t, paper, truth, rng, 300, None)
    a = analyze_sheet(ctx, scans, [300, 300])
    d = decide_sheet(ctx, a, Thresholds())
    images = render_images(ctx, a, d, scans)
    for key in ("images/pages/front.jpg", "images/pages/back.jpg", "images/identity/exam_no.jpg",
                "images/barcodes/key-student_barcode.jpg", "images/fields/key-name.jpg",
                "images/fields/key-name_back.jpg", "images/subjective/q-56/sub-/region-1.jpg",
                "images/subjective/q-56/sub-/region-2.jpg", "images/subjective/q-57/sub-/region-1.jpg",
                "images/subjective/q-57/sub-/region-2.jpg"):
        assert key in images, key
    assert all(f"images/objective/q-{o['no']}.jpg" in images for o in d.objective)
    h, w = images["images/pages/front.jpg"].shape  # 按扫描图实际分辨率输出整页
    assert abs(w / h - 390 / 270) < 0.01 and 4400 < w < 4800
    r = build_result(ctx, a, d, seq=3, batch_id="b", exam={"name": "测试"}, scanner="folder",
                     raw_paths=["../../raw/a.jpg", "../../raw/b.jpg"], image_paths=set(images))
    assert r["seq"] == 3 and r["exam"]["name"] == "测试" and r["manual"] is None
    assert r["scan"]["faces"]["front"]["align_method"] == "anchors"
    assert r["scan"]["faces"]["back"]["align_method"] == "frames"
    assert r["pages"]["raw_1"] == "../../raw/a.jpg"
    assert r["subjective"][0]["images"] == ["images/subjective/q-56/sub-/region-1.jpg",
                                          "images/subjective/q-56/sub-/region-2.jpg"]
    assert r["student"]["fields"]["name"]["ocr_status"] == "none"
