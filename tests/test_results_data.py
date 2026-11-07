from scan_answer.app.results import face_overlays, load_batch_template, load_rows, warning_text
from scan_answer.batch import run_batch
from scan_answer.output.jsonfile import read_json

from conftest import TEMPLATE_DIR


def test_rows_and_overlays(small_dataset, tmp_path):
    bd = run_batch(TEMPLATE_DIR, small_dataset, tmp_path, batch_id="b")
    rows = load_rows(bd)
    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert rows[2].status == "physical" and rows[2].result is None and rows[2].physical == "BLANK_PAGE"
    tpl = load_batch_template(bd)
    r = read_json(rows[0].result)
    ov = face_overlays(tpl, r, "front")
    answered = sum(len(o["answer"]) for o in r["objective"])
    bad_rows = sum(1 for o in r["objective"] if o["status"] != "ok")
    assert sum(1 for o in ov if o.kind in ("answer", "bad") and not o.label) == answered
    assert sum(1 for o in ov if o.label and o.kind == "bad") == bad_rows
    assert sum(1 for o in ov if o.kind in ("exam", "exam_bad")) == 15
    assert face_overlays(tpl, r, "back") == []


def test_warning_text():
    assert warning_text({"code": "OBJ_MULTIPLE", "field": "objective.12"}) == "第 12 题多涂"
    assert warning_text({"code": "ID_MISMATCH", "msg": "x"}) == "填涂考号与条码不一致"
    assert warning_text({"code": "EXAM_NO_PARTIAL", "msg": "考号第 3 位未识别"}) == "考号第 3 位未识别"
    assert warning_text({"code": "NEW_CODE"}) == "NEW_CODE"
