import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import analyze_sheet, decide_sheet
from scan_answer.engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from scan_answer.output.local import write_json_atomic
from scan_answer.output.jsonfile import read_json
from scan_answer.synth.generate import make_scans, make_sheet


def test_round_trip_gives_same_decision(ctx, tmp_path):
    rng = np.random.default_rng(21)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    a = analyze_sheet(ctx, make_scans(ctx.tpl, paper, truth, rng, 300, "rot180"), [300, 300])
    write_json_atomic(tmp_path / "s.json", analysis_to_dict(a))  # 必须能写成 JSON
    b = analysis_from_dict(read_json(tmp_path / "s.json"))
    th = Thresholds()
    da, db = decide_sheet(ctx, a, th), decide_sheet(ctx, b, th)
    assert da.objective == db.objective and da.exam_no == db.exam_no and da.barcodes == db.barcodes
    assert b.placements["front"].rotated == 180
    assert np.allclose(b.placements["front"].align.H, a.placements["front"].align.H)
    assert analysis_fills(b).shape == analysis_fills(a).shape == (55 * 5 + 15 * 10,)


def test_missing_anchor_is_reported_and_survives_round_trip(ctx, tmp_path):
    from scan_answer.engine.result import build_result, render_images
    rng = np.random.default_rng(5)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    s = ctx.tpl.dpi / 25.4
    x, y, w, h = ctx.tpl.faces["front"].align.anchors[1]
    paper["front"][int((y - 3) * s):int((y + h + 3) * s), int((x - 3) * s):int((x + w + 3) * s)] = 255
    scans = make_scans(ctx.tpl, paper, truth, rng, 300, None)
    a = analyze_sheet(ctx, scans, [300, 300])
    assert a.placements["front"].align.method == "anchors" and a.placements["front"].align.missing == (1,)
    d = decide_sheet(ctx, a, Thresholds())
    images = render_images(ctx, a, d, scans)
    r = build_result(ctx, a, d, seq=1, batch_id="b", exam={}, scanner="folder", raw_paths=[], image_paths=set(images))
    assert r["scan"]["faces"]["front"]["missing_anchors"] == [2]
    assert "missing_anchors" not in r["scan"]["faces"]["back"]
    write_json_atomic(tmp_path / "s.json", analysis_to_dict(a))
    assert analysis_from_dict(read_json(tmp_path / "s.json")).placements["front"].align.missing == (1,)
