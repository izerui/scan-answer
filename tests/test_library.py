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
