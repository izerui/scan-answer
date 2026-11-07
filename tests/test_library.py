from scan_answer.app.library import (ExamInfo, create_exam, exam_totals, list_batches, list_exams, list_templates,
                                     load_exam)
from scan_answer.app.settings import bundled_templates_dir
from scan_answer.batch import run_batch

from conftest import TEMPLATE_DIR


def test_list_templates_finds_bundled_199(tmp_path):
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / "template.json").write_text("{", encoding="utf-8")
    ts = list_templates([str(bundled_templates_dir()), str(tmp_path), str(TEMPLATE_DIR)])
    assert [t.name for t in ts] == ["管综199"]  # 去重、跳过损坏模板
    assert ts[0].faces == 2 and ts[0].questions == 57 and ts[0].paper == (390.0, 270.0)


def test_exams_and_batches(small_dataset, tmp_path):
    info = ExamInfo("2026 研考", "管综", "199", "第一考点", operator="李老师")
    e = create_exam(tmp_path, info, TEMPLATE_DIR)
    assert load_exam(e.directory).info == info
    assert info.meta() == {"name": "2026 研考", "subject": "管综", "subject_code": "199", "site": "第一考点",
                           "operator": "李老师"}
    create_exam(tmp_path, ExamInfo("另一场"), TEMPLATE_DIR)
    assert {x.info.name for x in list_exams(tmp_path)} == {"2026 研考", "另一场"}
    run_batch(TEMPLATE_DIR, small_dataset, e.directory, batch_id="b1", exam=info.meta())
    bs = list_batches(e.directory)
    assert len(bs) == 1 and bs[0].finalized and bs[0].state == "已结束"
    assert bs[0].counts["total"] == 4 and bs[0].counts["physical"] == 1
    assert exam_totals(e.directory)["ok"] == 3
    assert list_exams(tmp_path / "nope") == []
    assert list_batches(tmp_path / "missing") == []


def _write(p, obj):
    import json
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def make_bad_tree(root):
    """输出根目录与模板目录里放各种合法 JSON 但结构不对的文件（界面启动测试也用）。"""
    good = create_exam(root / "out", ExamInfo("好考试"), TEMPLATE_DIR)
    out = root / "out"
    _write(out / "e_list" / "exam.json", [])
    _write(out / "e_info" / "exam.json", {"info": "x", "template_dir": "t"})
    _write(out / "e_tpl" / "exam.json", {"info": {"name": "缺模板"}, "template_dir": 5})
    _write(out / "e_created" / "exam.json", {"info": {"name": "时间不对", "subject": 3}, "template_dir": "t",
                                             "created": 123})
    _write(good.directory / "b_list" / "manifest.json", [])
    _write(good.directory / "b_counts" / "manifest.json",
           {"batch_id": 7, "created": 1, "counts": {"total": "3", "ok": 2}, "ocr_progress": {"done": 1}})
    _write(good.directory / "b_types" / "manifest.json", {"counts": [], "ocr_progress": [], "finalized": "yes"})
    tpls = root / "tpls"
    _write(tpls / "t_list" / "template.json", [])
    _write(tpls / "t_paper" / "template.json", {"name": "x", "version": 1, "paper": [390], "faces": {}})
    _write(tpls / "t_paper2" / "template.json", {"name": "x", "version": 1, "paper": "ab", "faces": {}})
    return good, out, tpls


def test_malformed_files_are_skipped_or_defaulted(tmp_path):
    good, out, tpls = make_bad_tree(tmp_path)
    exams = list_exams(out)
    assert {e.info.name for e in exams} == {"好考试", "时间不对"}
    odd = next(e for e in exams if e.info.name == "时间不对")
    assert odd.created == "" and odd.info.subject == ""
    bs = {b.directory.name: b for b in list_batches(good.directory)}
    assert set(bs) == {"b_counts", "b_types"}
    b = bs["b_counts"]
    assert b.batch_id == "b_counts" and b.created == "" and b.counts == {"total": 0, "ok": 2}
    assert b.ocr_progress == {"done": 1, "failed": 0, "total": 0}
    assert bs["b_types"].counts == {} and bs["b_types"].ocr_progress is None and not bs["b_types"].finalized
    assert exam_totals(good.directory)["ok"] == 2
    assert [t.name for t in list_templates([str(tpls), str(TEMPLATE_DIR)])] == ["管综199"]


def test_unreadable_dirs_are_skipped(tmp_path):
    import os
    import sys
    if sys.platform == "win32" or os.geteuid() == 0:
        import pytest
        pytest.skip("需要 POSIX 权限语义")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        assert [t.name for t in list_templates([str(locked), str(TEMPLATE_DIR)])] == ["管综199"]
        assert list_exams(locked) == [] and list_batches(locked) == []
    finally:
        locked.chmod(0o755)
