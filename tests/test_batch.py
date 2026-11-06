import json
import shutil

from scan_answer.batch import read_dpi, run_batch
from scan_answer.evaluate import evaluate_dirs
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR


def test_batch_end_to_end(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 8, seed=11)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    rep = evaluate_dirs(tmp_path / "ds", bd).report()
    assert rep["silent_errors"] == 0 and rep["physical_false"] == 0
    assert rep["normal_accuracy"] == 1.0
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    first = next(s for s in manifest["sheets"] if s.get("result"))
    result = json.loads((bd / first["result"]).read_text(encoding="utf-8"))
    for key in ("schema_version", "seq", "batch_id", "template", "exam", "scan", "pages", "student",
                "objective", "subjective", "choose_one", "manual", "warnings"):
        assert key in result
    assert len(result["objective"]) == 55
    student_dir = (bd / first["result"]).parent
    for q in result["subjective"]:
        for img in q["images"]:
            assert (student_dir / img).exists()


def test_odd_image_count_reports_missing_face(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 2, seed=12)
    (tmp_path / "ds" / "0002_2.jpg").unlink()
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sheets"][-1]["physical"] == "MISSING_FACE"
    assert (bd / "errors" / "0002_MISSING_FACE" / "error.json").exists()


def test_corrupt_image_reports_align_failed(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 2, seed=12)
    # 用垃圾字节覆盖其中一张面
    (tmp_path / "ds" / "0002_1.jpg").write_bytes(b"not an image")
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    corrupt_sheet = next(s for s in manifest["sheets"] if s["source"] == "0002")
    assert corrupt_sheet["physical"] == "ALIGN_FAILED"
    assert (bd / "errors" / f"{corrupt_sheet['seq']:04d}_ALIGN_FAILED" / "error.json").exists()
    # 另一张正常识别
    ok_sheet = next(s for s in manifest["sheets"] if s["source"] == "0001")
    assert ok_sheet.get("result")


def test_read_dpi_garbage_file(tmp_path):
    garbage = tmp_path / "garbage.jpg"
    garbage.write_bytes(b"not an image")
    assert read_dpi(garbage) is None
