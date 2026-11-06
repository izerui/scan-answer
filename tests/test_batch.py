import json
import shutil
from pathlib import Path

from scan_answer.batch import pair_images, read_dpi, run_batch
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


# ---- F1: pair_images 按源文件名分组 ----

def test_pair_images_with_suffix_groups_by_source():
    """文件名带 _N 后缀时，按前缀分组、组内按 N 排序。"""
    paths = [Path(f"{n}.jpg") for n in ("0001_1", "0001_2", "0002_1", "0002_2", "0003_1", "0003_2")]
    groups = pair_images(paths, 2)
    assert len(groups) == 3
    assert [p.stem for p in groups[0]] == ["0001_1", "0001_2"]
    assert [p.stem for p in groups[2]] == ["0003_1", "0003_2"]


def test_pair_images_missing_face_via_suffix():
    """缺一面时，该组只有 1 个文件（后续 analyze_sheet 会报 MISSING_FACE）。"""
    paths = [Path(f"{n}.jpg") for n in ("0001_1", "0001_2", "0002_1", "0003_1", "0003_2")]
    groups = pair_images(paths, 2)
    assert len(groups) == 3
    missing = [g for g in groups if len(g) == 1]
    assert len(missing) == 1
    assert missing[0][0].stem == "0002_1"


def test_pair_images_no_suffix_falls_back():
    """无 _N 后缀时，退回按排序顺序每 faces 张一组。"""
    paths = [Path(f"{n}.jpg") for n in ("a", "b", "c", "d")]
    groups = pair_images(paths, 2)
    assert groups == [[paths[0], paths[1]], [paths[2], paths[3]]]


def test_middle_missing_face_no_cross_contamination(tpl_bases, tmp_path):
    """中间缺面：生成 4 张卡，删 0002_2.jpg → 0002 为 MISSING_FACE，其余正确识别、不串卷。"""
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 4, seed=42)
    # 读取每张卡的 truth 考号
    truths = {}
    for i in range(1, 5):
        tf = tmp_path / "ds" / f"{i:04d}.truth.json"
        truths[f"{i:04d}"] = json.loads(tf.read_text(encoding="utf-8"))
    # 删掉第 2 张卡的第 2 面
    (tmp_path / "ds" / "0002_2.jpg").unlink()
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["sheets"]:
        src = item["source"]
        if src == "0002":
            assert item["physical"] == "MISSING_FACE", f"0002 应为 MISSING_FACE，实际 {item}"
        else:
            assert "physical" not in item, f"{src} 不该有物理异常: {item}"
            assert item.get("result"), f"{src} 应有 result"
            result = json.loads((bd / item["result"]).read_text(encoding="utf-8"))
            truth = truths[src]
            if truth.get("exam_no") and truth["exam_no"]["value"]:
                assert result["student"]["exam_no"]["value"] == truth["exam_no"]["value"], \
                    f"{src} 考号串卷: 期望 {truth['exam_no']['value']}，实际 {result['student']['exam_no']['value']}"
