from scan_answer.app.results import OverlayMM, face_overlays, load_batch_template, load_rows, warning_text
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


def test_overlay_kinds_covered_by_overlay_color(ext_template):
    """face_overlays 产生的所有 kind 都在 OVERLAY_COLOR 中有定义。"""
    from scan_answer.ui.pages.results_page import OVERLAY_COLOR
    from scan_answer.template import load_template

    tpl, _ = load_template(ext_template)
    # 构造一个包含所有 overlay 类型的 result
    result = {
        "objective": [{"no": "1", "answer": "A", "status": "ok"}, {"no": "2", "answer": "", "status": "blank"}],
        "student": {
            "exam_no": {"value": "1?", "status": "partial",
                        "digits": [{"pos": 2, "value": None, "status": "blank"}]},
            "omr_fields": {
                "paper_type": {"label": "AB 卷", "value": "A", "status": "ok",
                               "digits": [], "image": "x.jpg"},
            },
        },
        "choose_one": [
            {"key": "opt1", "label": "选做", "questions": ["56", "57"],
             "chosen": ["56"], "status": "ok", "fill": {"56": 0.8, "57": 0.0}},
        ],
    }
    overlays = face_overlays(tpl, result, "front")
    kinds = {o.kind for o in overlays}
    for kind in kinds:
        assert kind in OVERLAY_COLOR, f"overlay kind {kind!r} not in OVERLAY_COLOR"
