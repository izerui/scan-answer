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
