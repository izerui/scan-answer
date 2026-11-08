"""题图与题目身份的输出契约：正常题、特殊名称和路径完整性。"""
from copy import deepcopy
from pathlib import PurePosixPath

import numpy as np
import pytest

from scan_answer.engine.align import AlignResult
from scan_answer.engine.recognize import FacePlacement, SheetAnalysis, SheetDecision, TemplateContext
from scan_answer.engine.result import build_result, render_images
from scan_answer.template.model import (
    AlignSpec, BarcodeSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid, Region, SubjectiveSpec, Template,
)


@pytest.fixture
def evidence_case():
    t = Template(
        schema_version="1.0", name="小卡", version=1, paper=(80, 60), dpi=254,
        faces={"front": Face("front.png", AlignSpec("anchors"))},
        exam_no=None, absent=None,
        barcodes=[BarcodeSpec("name", "front", (5, 45, 15, 5))],
        fields=[FieldSpec("page_front", "姓名", "front", (25, 45, 15, 5)),
                FieldSpec("name", "单位", "front", (45, 45, 15, 5))],
        objective=[ObjectiveBlock("1-2", "single", 1, OmrGrid(
            "front", ["A", "B"],
            [[(10, 10, 3, 2), (15, 10, 3, 2)], [(10, 15, 3, 2), (15, 15, 3, 2)]],
        ))],
        subjective=[
            SubjectiveSpec("56", "short", "zh", [Region("front", (10, 25, 10, 10))], "(1)"),
            SubjectiveSpec("56", "short", "zh", [Region("front", (30, 25, 10, 10))], "[1]"),
        ],
    )
    gray = np.full((600, 800), 240, np.uint8)
    gray[250:350, 100:200] = 40
    gray[250:350, 300:400] = 120
    ctx = TemplateContext(t, {"front": gray})
    a = SheetAnalysis({"front": FacePlacement(
        0, 0, AlignResult(True, "anchors", H=np.diag([0.1, 0.1, 1.0])), 254,
    )}, False)
    d = SheetDecision(
        None, None,
        [{"no": "1", "type": "single", "answer": "A", "status": "ok", "fill": {"A": 0.9, "B": 0}},
         {"no": "2", "type": "single", "answer": "", "status": "blank", "fill": {"A": 0, "B": 0}}],
        {"name": {"value": "123", "status": "ok", "symbology": "Code128"}}, None, [],
    )
    return ctx, a, d, [gray]


def _result(ctx, a, d, images):
    return build_result(ctx, a, d, seq=7, batch_id="batch", exam={}, scanner="folder",
                        raw_paths=["../../raw/card.png"], image_paths=set(images))


def test_every_objective_has_image_even_when_recognition_is_ok(evidence_case):
    ctx, a, d, source = evidence_case
    images = render_images(ctx, a, d, source)
    result = _result(ctx, a, d, images)
    assert all("image" in q for q in result["objective"])
    assert all(q["images"] == [q["image"]] for q in result["objective"])
    assert all(q["image"] in images for q in result["objective"])
    assert result["objective"][0]["question_id"] == "objective/1"
    assert result["objective"][0]["regions"][0]["face"] == "front"


def test_subjective_names_do_not_collapse_to_the_same_picture(evidence_case):
    ctx, a, d, source = evidence_case
    images = render_images(ctx, a, d, source)
    result = _result(ctx, a, d, images)
    left, right = result["subjective"]
    assert left["images"] != right["images"]
    assert left["question_id"] != right["question_id"]
    assert float(images[left["images"][0]].mean()) == 40
    assert float(images[right["images"][0]].mean()) == 120


def test_field_names_cannot_overwrite_page_or_barcode_images(evidence_case):
    ctx, a, d, source = evidence_case
    images = render_images(ctx, a, d, source)
    result = _result(ctx, a, d, images)
    paths = [result["pages"]["front"], result["student"]["barcodes"]["name"]["image"],
             *(f["image"] for f in result["student"]["fields"].values())]
    assert len(paths) == len(set(paths))
    assert images[result["pages"]["front"]].shape == (600, 800)


def test_user_keys_remain_inside_the_image_directory(evidence_case):
    ctx, a, d, source = evidence_case
    ctx.tpl.fields[0].key = "../outside"
    images = render_images(ctx, a, d, source)
    assert all(".." not in PurePosixPath(p).parts for p in images)
    assert all(PurePosixPath(p).parts[0] == "images" for p in images)


def test_missing_question_picture_cannot_be_published(evidence_case):
    ctx, a, d, _source = evidence_case
    with pytest.raises(ValueError, match="图片"):
        _result(ctx, a, d, {})


def test_duplicate_logical_question_cannot_silently_overwrite(evidence_case):
    ctx, a, d, source = evidence_case
    ctx.tpl.subjective.append(deepcopy(ctx.tpl.subjective[0]))
    with pytest.raises(ValueError, match="重复"):
        render_images(ctx, a, d, source)


def test_keys_remain_distinct_on_case_insensitive_filesystems(evidence_case):
    ctx, a, d, source = evidence_case
    ctx.tpl.fields[0].key = "A"
    ctx.tpl.fields[1].key = "a"
    images = render_images(ctx, a, d, source)
    result = _result(ctx, a, d, images)
    upper = result["student"]["fields"]["A"]["image"]
    lower = result["student"]["fields"]["a"]["image"]
    assert upper.casefold() != lower.casefold()


@pytest.mark.parametrize("key", ["姓名" * 14, "A" * 83])
def test_long_field_keys_can_be_written_without_losing_identity(evidence_case, tmp_path, key):
    from scan_answer.output.local import LocalExporter
    ctx, a, d, source = evidence_case
    ctx.tpl.fields[0].key = key
    ctx.tpl.fields[1].key = key + "2"
    images = render_images(ctx, a, d, source)
    result = _result(ctx, a, d, images)
    path = LocalExporter(tmp_path).write_student(7, None, result, images)
    left = result["student"]["fields"][key]["image"]
    right = result["student"]["fields"][key + "2"]["image"]
    assert left != right
    assert (path.parent / left).is_file()
    assert (path.parent / right).is_file()


def test_question_without_region_cannot_publish_an_empty_image_list(evidence_case):
    ctx, a, d, source = evidence_case
    ctx.tpl.subjective[0].regions = []
    with pytest.raises(ValueError, match="区域"):
        render_images(ctx, a, d, source)
