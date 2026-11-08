"""扩展识别引擎测试：自定义填涂字段、选做组、判断题、同步头偏移。"""
import hashlib
import json
import tempfile
from pathlib import Path

import numpy as np
import pytest

from conftest import TEMPLATE_DIR


# ---------------------------------------------------------------------------
# test_decide_idx_matches_decide
# ---------------------------------------------------------------------------

def test_decide_idx_matches_decide():
    """decide_idx 的下标 + labels 拼出的结果与 decide 完全一致。"""
    from scan_answer.engine.omr import Thresholds, decide, decide_idx

    rng = np.random.default_rng(0)
    th = Thresholds()
    for qtype in ("single", "multiple"):
        labels = list("ABCDE")
        for _ in range(200):
            fills = rng.uniform(0, 1, len(labels)).astype(np.float32)
            ans_d, st_d = decide(fills, labels, qtype, th)
            idxs, st_i = decide_idx(fills, qtype, th)
            ans_i = "".join(labels[i] for i in idxs)
            assert (ans_i, st_i) == (ans_d, st_d), f"qtype={qtype}, fills={fills}"


# ---------------------------------------------------------------------------
# test_ext_template_recognized
# ---------------------------------------------------------------------------

def test_ext_template_recognized(ext_template, tmp_path):
    """ext_template 上生成 20 张卡，全部识别，与 truth 逐张比对。"""
    from scan_answer.batch import run_batch
    from scan_answer.evaluate import evaluate_dirs
    from scan_answer.synth.generate import generate_dataset
    from scan_answer.template import load_template

    t, bases = load_template(ext_template)
    ds = tmp_path / "ds"
    generate_dataset(t, bases, ds, 20, seed=3)
    bd = run_batch(ext_template, ds, tmp_path / "out", batch_id="ext")
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))

    for entry in manifest["sheets"]:
        truth_path = ds / f"{entry['seq']:04d}.truth.json"
        truth = json.loads(truth_path.read_text(encoding="utf-8"))

        # 物理异常卡跳过
        if truth.get("physical"):
            assert entry["status"] == "physical"
            continue
        assert entry["status"] == "ok", f"seq={entry['seq']} 应为 ok，实际 {entry['status']}"
        result = json.loads((bd / entry["result"]).read_text(encoding="utf-8"))

        # omr_fields.paper_type
        omr = result["student"]["omr_fields"]["paper_type"]
        t_omr = truth["omr_fields"]["paper_type"]
        assert omr["value"] == t_omr["value"], f"seq={entry['seq']} paper_type value"
        assert omr["status"] == t_omr["status"], f"seq={entry['seq']} paper_type status"

        # choose_one
        c = result["choose_one"][0]
        t_c = truth["choose_one"]["opt1"]
        assert c["chosen"] == t_c["chosen"], f"seq={entry['seq']} choose chosen"
        assert c["status"] == t_c["status"], f"seq={entry['seq']} choose status"

        # 判断题 46–55
        obj_map = {o["no"]: o for o in result["objective"]}
        t_obj_map = {o["no"]: o for o in truth["objective"]}
        for no in [str(n) for n in range(46, 56)]:
            assert obj_map[no]["answer"] == t_obj_map[no]["answer"], f"seq={entry['seq']} q{no} answer"
            assert obj_map[no]["status"] == t_obj_map[no]["status"], f"seq={entry['seq']} q{no} status"

        # sync
        sync = result["scan"]["faces"]["front"]["sync"]
        assert sync["found"] == 12, f"seq={entry['seq']} sync found"
        assert sync["max_offset_mm"] is not None and sync["max_offset_mm"] < 1.0, \
            f"seq={entry['seq']} sync offset={sync.get('max_offset_mm')}"


# ---------------------------------------------------------------------------
# test_old_state_without_new_fields_reloads
# ---------------------------------------------------------------------------

def test_old_state_without_new_fields_reloads():
    """旧 state 缺少新字段时，analysis_from_dict 不报错，新字段为空 dict。"""
    from scan_answer.engine.state import analysis_from_dict, analysis_to_dict
    from scan_answer.engine.recognize import FacePlacement, SheetAnalysis
    from scan_answer.engine.align import AlignResult

    fake_align = AlignResult(True, "anchors", H=np.eye(3), skew_deg=0.0, residual_mm=0.0)
    a = SheetAnalysis(
        placements={"front": FacePlacement(0, 0, fake_align, 200.0)},
        swapped=False,
        exam_fills=np.zeros((8, 10), dtype=np.float32),
        absent_fill=0.0,
        objective_fills=[np.zeros((15, 5), dtype=np.float32)],
        barcodes={},
    )
    d = analysis_to_dict(a)
    # 删掉新键
    for key in ("omr_field_fills", "choose_fills", "sync"):
        d.pop(key, None)
    a2 = analysis_from_dict(d)
    assert a2.omr_field_fills == {}
    assert a2.choose_fills == {}
    assert a2.sync == {}


# ---------------------------------------------------------------------------
# test_ext_warnings_and_overlays
# ---------------------------------------------------------------------------

def test_ext_warnings_and_overlays(ext_template):
    """warnings 和 overlays 对新区域的处理。"""
    from scan_answer.app.results import face_overlays, warning_text
    from scan_answer.template import load_template

    tpl, _ = load_template(ext_template)

    # 构造 result dict
    result = {
        "objective": [],
        "student": {
            "exam_no": None,
            "omr_fields": {
                "paper_type": {
                    "label": "AB 卷",
                    "value": "A?",
                    "status": "partial",
                    "digits": [{"pos": 2, "value": None, "status": "uncertain"}],
                    "image": "images/omr_fields/key-paper_type.jpg",
                },
            },
        },
        "choose_one": [
            {
                "key": "opt1",
                "label": "选做",
                "questions": ["56", "57"],
                "chosen": ["56", "57"],
                "status": "multiple",
                "fill": {"56": 0.8, "57": 0.7},
            },
        ],
    }

    # OMR_FIELD_PARTIAL 警告
    w_partial = {"code": "OMR_FIELD_PARTIAL", "field": "student.omr_fields.paper_type", "msg": "AB 卷有位未识别"}
    assert warning_text(w_partial) == "AB 卷有位未识别"

    # OMR_FIELD_BLANK 警告
    w_blank = {"code": "OMR_FIELD_BLANK", "field": "student.omr_fields.paper_type", "msg": "AB 卷未填涂"}
    assert warning_text(w_blank) == "AB 卷未填涂"

    # CHOICE_MULTIPLE 警告
    w_choice = {"code": "CHOICE_MULTIPLE", "field": "choose_one.opt1"}
    assert warning_text(w_choice) == "选做题多涂"

    # CHOICE_BLANK 警告
    w_cblank = {"code": "CHOICE_BLANK", "field": "choose_one.opt1"}
    assert warning_text(w_cblank) == "选做题未填涂"

    # CHOICE_UNCERTAIN 警告
    w_cunc = {"code": "CHOICE_UNCERTAIN", "field": "choose_one.opt1"}
    assert warning_text(w_cunc) == "选做题涂得浅或擦除不净"

    # OMR_FIELD_PARTIAL / BLANK 无 msg 时使用 fallback
    w_partial_no_msg = {"code": "OMR_FIELD_PARTIAL", "field": "student.omr_fields.paper_type"}
    assert warning_text(w_partial_no_msg) == "填涂字段有位未识别"
    w_blank_no_msg = {"code": "OMR_FIELD_BLANK", "field": "student.omr_fields.paper_type"}
    assert warning_text(w_blank_no_msg) == "填涂字段未填涂"

    # overlays
    overlays = face_overlays(tpl, result, "front")
    choice_or_bad = [o for o in overlays if o.kind in ("choice", "bad")]
    assert len(choice_or_bad) >= 1


# ---------------------------------------------------------------------------
# test_decision_key_includes_new_fields
# ---------------------------------------------------------------------------

def test_decision_key_includes_new_fields():
    """_decision_key 区分仅 omr_fields 或 choose_one 不同的判定结果。"""
    from scan_answer.pipeline.worker import _decision_key

    base = {
        "objective": [{"no": "1", "answer": "A", "status": "ok"}],
        "student": {"exam_no": {"value": "123", "status": "ok"}, "absent": {"value": False},
                    "omr_fields": {"paper_type": {"value": "A", "status": "ok"}}},
        "choose_one": [{"key": "opt1", "chosen": ["56"], "status": "ok"}],
    }
    import copy
    # 仅 omr_fields 变化
    r2 = copy.deepcopy(base)
    r2["student"]["omr_fields"]["paper_type"]["value"] = "B"
    assert _decision_key(base) != _decision_key(r2)

    # 仅 choose_one 变化
    r3 = copy.deepcopy(base)
    r3["choose_one"][0]["chosen"] = ["57"]
    assert _decision_key(base) != _decision_key(r3)

    # 相同时 key 也相同
    r4 = copy.deepcopy(base)
    assert _decision_key(base) == _decision_key(r4)


# ---------------------------------------------------------------------------
# test_existing_dataset_unchanged
# ---------------------------------------------------------------------------

def test_existing_dataset_unchanged(tpl_bases, tmp_path):
    """没有新区域的模板生成的 truth 随机序列不变。"""
    from scan_answer.synth.generate import generate_dataset

    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 3, seed=17)

    expected = {
        1: "bd6ce0ecb575cda906a02057b79a6a7dd8b3bc66673e7b49c978355ec537d400",
        2: "9a116d5e61c50a4db80c3ada2752b029f3b46837853ddbbcc2b7f3652363cb53",
        3: "debb011278e0314606ae8525a96a695a21c25a5cecf55dd32b965a5b8b0974c6",
    }
    for seq, exp_hash in expected.items():
        p = tmp_path / "ds" / f"{seq:04d}.truth.json"
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        assert actual == exp_hash, f"seq={seq}: truth.json hash changed"
