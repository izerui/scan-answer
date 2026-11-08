import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_jpg
from scan_answer.pipeline.worker import (FinalizeJob, SheetJob, clear_outputs, finalize_sheet, process_sheet,
                                         state_path)
from scan_answer.synth.generate import make_scans, make_sheet

from conftest import TEMPLATE_DIR


def _raw(ctx, tmp_path, seed, special=None, seq=1):
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    scans = make_scans(ctx.tpl, paper, truth, rng, 300, special)
    paths = []
    for i, img in enumerate(scans, start=1):
        p = tmp_path / "raw" / f"{seq:04d}_s_{i}.jpg"
        write_jpg(p, img, quality=95)
        paths.append(str(p))
    return paths, truth


def _job(tmp_path, seq, paths, th=Thresholds(), ocr=False):
    return SheetJob(seq=seq, raw_paths=paths, dpis=[300.0] * len(paths), mark=th.mark, uncertain=th.uncertain,
                    batch_dir=str(tmp_path), template_dir=str(TEMPLATE_DIR), batch_id="b", exam={"name": "考试"},
                    scanner="folder", ocr=ocr)


def test_process_sheet_writes_result_and_state(ctx, tmp_path):
    paths, truth = _raw(ctx, tmp_path, 101)
    out = process_sheet(_job(tmp_path, 1, paths, ocr=True))
    assert out.ok and out.seq == 1 and out.exam_no == truth["exam_no"]["value"]
    assert len(out.fills) == 55 * 5 + 15 * 10
    r = read_json(tmp_path / out.result)
    assert r["exam"] == {"name": "考试"} and r["pages"]["raw_1"] == "../../raw/0001_s_1.jpg"
    assert all(q["ocr_status"] == "pending" for q in r["subjective"])
    st = read_json(state_path(tmp_path, 1))
    assert st["result"] == out.result
    assert st["raw_paths"] == ["raw/0001_s_1.jpg", "raw/0001_s_2.jpg"]


def test_process_sheet_physical_and_unreadable(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 102, "blank", seq=2)
    out = process_sheet(_job(tmp_path, 2, paths))
    assert not out.ok and out.physical == "BLANK_PAGE"
    assert (tmp_path / "errors" / "0002_BLANK_PAGE" / "error.json").exists()
    bad = tmp_path / "raw" / "0003_x_1.jpg"
    bad.write_bytes(b"not an image")
    out = process_sheet(_job(tmp_path, 3, [str(bad), paths[0]]))
    assert out.physical == "ALIGN_FAILED" and "无法读取图片" in out.message


def test_finalize_sheet_only_rewrites_on_change(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 103)
    out = process_sheet(_job(tmp_path, 1, paths, th=Thresholds(0.97, 0.5)))
    same = finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(tmp_path), str(TEMPLATE_DIR)))
    assert not same.changed
    fixed = finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(tmp_path), str(TEMPLATE_DIR)))
    assert fixed.changed
    r = read_json(tmp_path / out.result)
    assert sum(o["status"] == "uncertain" for o in r["objective"]) < 5


def test_clear_outputs(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 104, seq=12)
    out = process_sheet(_job(tmp_path, 12, paths))
    (tmp_path / "students" / "0120_x").mkdir(parents=True)  # 序号 120 不应被误删
    clear_outputs(tmp_path, 12)
    assert not (tmp_path / out.result).exists() and not state_path(tmp_path, 12).exists()
    assert (tmp_path / "students" / "0120_x").exists()
