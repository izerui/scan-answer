"""模板模型扩展与校验的测试。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from scan_answer.template import (
    load_template, save_template, template_from_dict, template_to_dict,
)
from scan_answer.template.model import (
    ChooseOneSpec, OmrFieldSpec, OmrGrid, SyncSpec,
)
from scan_answer.template.validate import check_alignment, validate_template

BUNDLED = Path(__file__).resolve().parent.parent / "templates" / "guanzong199"


# ---------- 旧模板兼容 ----------

def test_old_template_loads_with_defaults():
    """旧模板缺少新键时，template_from_dict 填入默认值。"""
    raw = json.loads((BUNDLED / "template.json").read_text(encoding="utf-8"))
    raw.pop("sync", None)
    raw.pop("omr_fields", None)
    raw.pop("choose_one", None)
    for o in raw.get("objective", []):
        o.pop("group", None)
    t = template_from_dict(raw)
    assert t.sync == []
    assert t.omr_fields == []
    assert t.choose_one == []
    assert t.objective[0].group == 0


# ---------- 新字段往返 ----------

def test_roundtrip_new_fields():
    """含新字段的模板经 dict → JSON → dict 往返后不丢数据。"""
    t, _ = load_template(BUNDLED)
    t.sync = [
        SyncSpec(
            face="front",
            direction="vertical",
            marks=[(18, 50, 4, 2.2), (18, 54, 4, 2.2)],
        )
    ]
    t.omr_fields = [
        OmrFieldSpec(
            key="paper_type",
            label="AB 卷",
            grid=OmrGrid(
                face="front",
                labels=["A", "B"],
                cells=[[(10, 10, 3, 2), (10, 14, 3, 2)]],
            ),
        )
    ]
    t.choose_one = [
        ChooseOneSpec(
            key="choose_56_57",
            label="选做题",
            face="front",
            questions=["56", "57"],
            cells=[(50, 100, 4, 2.2), (50, 104, 4, 2.2)],
        )
    ]
    d = template_to_dict(t)
    j = json.dumps(d, ensure_ascii=False)
    t2 = template_from_dict(json.loads(j))
    assert t2.sync == t.sync
    assert t2.omr_fields == t.omr_fields
    assert t2.choose_one == t.choose_one


# ---------- 校验：通过 ----------

def test_validate_ok_on_bundled():
    t, _ = load_template(BUNDLED)
    assert validate_template(t) == []


# ---------- 校验：8 种破坏 ----------

def _load_bundled():
    return load_template(BUNDLED)


@pytest.mark.parametrize(
    "mutation, keyword",
    [
        ("zero_width", "宽或高"),
        ("out_of_paper", "超出纸面"),
        ("bad_face", "不存在的面"),
        ("few_anchors", "至少 4 个定位块"),
        ("dup_qno", "题号重复"),
        ("dup_field_key", "键重复"),
        ("bad_choose_qno", "99"),
        ("few_sync", "同步头"),
    ],
    ids=lambda x: x if isinstance(x, str) else "",
)
def test_validate_catches_each_problem(mutation, keyword):
    t, _ = _load_bundled()
    if mutation == "zero_width":
        # 把客观题块第一个格的宽改成 0
        cells = t.objective[0].grid.cells
        old = cells[0][0]
        cells[0][0] = (old[0], old[1], 0, old[3])
    elif mutation == "out_of_paper":
        # 把 barcode 的 x 改成 389（纸宽 390，rect 宽 > 1，必然超出）
        old = t.barcodes[0].rect
        t.barcodes[0].rect = (389, old[1], old[2], old[3])
    elif mutation == "bad_face":
        t.fields[0].face = "middle"
    elif mutation == "few_anchors":
        t.faces["front"].align.anchors = t.faces["front"].align.anchors[:3]
    elif mutation == "dup_qno":
        # 把第二个客观题块的 start 改成 1（与第一块重叠）
        t.objective[1].start = 1
    elif mutation == "dup_field_key":
        # 复制一个 field
        t.fields.append(copy.deepcopy(t.fields[0]))
    elif mutation == "bad_choose_qno":
        t.choose_one = [
            ChooseOneSpec(
                key="c1", label="选做", face="front",
                questions=["99", "57"],
                cells=[(50, 100, 4, 2.2), (50, 104, 4, 2.2)],
            )
        ]
    elif mutation == "few_sync":
        t.sync = [
            SyncSpec(face="front", direction="vertical", marks=[(18, 50, 4, 2.2)])
        ]
    else:
        raise ValueError(mutation)

    problems = validate_template(t)
    assert problems, f"mutation {mutation!r} 应产生校验问题"
    assert any(keyword in msg for msg in problems), (
        f"mutation {mutation!r} 应包含关键字 {keyword!r}，实际: {problems}"
    )


# ---------- 对齐检查 ----------

def test_check_alignment_bundled_ok_and_broken():
    t, bases = _load_bundled()
    # 正常模板应通过
    assert check_alignment(t, bases) == []
    # 把正面定位块尺寸改成极小值，使检测无法匹配
    t2 = copy.deepcopy(t)
    anchors = t2.faces["front"].align.anchors
    t2.faces["front"].align.anchors = [
        (a[0], a[1], 0.5, 0.5) for a in anchors
    ]
    problems = check_alignment(t2, bases)
    assert any("正面" in msg for msg in problems)


# ---------- save_template 带底图 ----------

def test_save_template_with_bases_atomic(tmp_path):
    t, bases = _load_bundled()
    save_template(t, tmp_path / "out", bases=bases)
    out = tmp_path / "out"
    assert (out / "front.png").exists()
    assert (out / "back.png").exists()
    assert (out / "template.json").exists()
    # 没有 *.tmp 残留
    assert not list(out.glob("*.tmp"))
    # 重新加载后底图尺寸不变
    t2, bases2 = load_template(out)
    for name in bases:
        assert bases2[name].shape == bases[name].shape
