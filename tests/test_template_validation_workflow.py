"""Malformed editor state must produce validation errors, never indexing crashes."""
from copy import deepcopy

import pytest

from scan_answer.template.model import (
    AlignSpec, BarcodeSpec, ChooseOneSpec, ExamNoSpec, Face, FieldSpec,
    ObjectiveBlock, OmrFieldSpec, OmrGrid, Region, SubjectiveSpec, SyncSpec,
    Template,
)
from scan_answer.template.validate import validate_template


def valid_template():
    grid = OmrGrid("front", ["A", "B"], [[(10, 10, 3, 2), (15, 10, 3, 2)]])
    return Template(
        "1.0", "Validation", 1, (210.0, 297.0), 300,
        {"front": Face("front.png", AlignSpec("frames", frames=[(5, 5, 200, 287)]))},
        ExamNoSpec(deepcopy(grid)), Region("front", (20, 20, 3, 2)),
        [BarcodeSpec("barcode", "front", (20, 30, 30, 10))],
        [FieldSpec("name", "Name", "front", (20, 45, 30, 10))],
        [ObjectiveBlock("1", "single", 1, deepcopy(grid))],
        [
            SubjectiveSpec("2", "short", "zh", [Region("front", (20, 60, 30, 20))], "1"),
            SubjectiveSpec("2", "short", "zh", [Region("front", (20, 90, 30, 20))], "2"),
        ],
        [Region("front", (20, 120, 3, 2))],
        [SyncSpec("front", "vertical", [(5, 20, 3, 2), (5, 25, 3, 2)])],
        [OmrFieldSpec("room", "Room", deepcopy(grid))],
        [ChooseOneSpec("choice", "Choice", "front", ["2.1", "2.2"],
                       [(20, 150, 3, 2), (25, 150, 3, 2)])],
    )


def test_valid_subquestion_choices():
    assert validate_template(valid_template()) == []


@pytest.mark.parametrize("attr,value", [
    ("paper", (float("nan"), 297)), ("paper", (210, float("inf"))),
    ("paper", (0, 297)), ("paper", (-1, 297)), ("paper", (298.1, 421)),
    ("paper", (210, 422)), ("paper", None), ("paper", [210]),
    ("dpi", 0), ("dpi", -300), ("dpi", 1.5), ("dpi", float("nan")),
    ("dpi", True), ("faces", {}), ("faces", None),
    ("version", 0), ("name", " "), ("schema_version", "9.9"),
    ("fields", None), ("objective", [None]), ("subjective", [None]),
])
def test_invalid_template_structure(attr, value):
    t = valid_template()
    setattr(t, attr, value)
    assert validate_template(t)


@pytest.mark.parametrize("paper", [(297, 420), (420, 297), (298, 421), (390, 270)])
def test_paper_orientation_and_metadata_tolerance(paper):
    t = valid_template()
    t.paper = paper
    t.faces["front"].align.frames = [(5, 5, paper[0] - 10, paper[1] - 10)]
    assert validate_template(t) == []


@pytest.mark.parametrize("mutation", [
    lambda t: t.faces.update(middle=t.faces.pop("front")),
    lambda t: setattr(t.faces["front"].align, "method", "unknown"),
    lambda t: setattr(t.faces["front"], "align", None),
    lambda t: setattr(t.faces["front"].align, "anchors", None),
    lambda t: setattr(t.faces["front"].align, "frames", []),
    lambda t: setattr(t.faces["front"], "image", ""),
])
def test_invalid_faces(mutation):
    t = valid_template()
    mutation(t)
    assert validate_template(t)


@pytest.mark.parametrize("owner", [
    "exam", "objective", "omr", "absent", "barcode", "field", "subjective",
    "ignore", "sync", "choice", "anchors", "frames",
])
@pytest.mark.parametrize("rect", [
    (float("nan"), 10, 3, 2), (10, 10, float("inf"), 2),
    (10, 10, 0, 2), (-1, 10, 3, 2), (209, 10, 3, 2),
    (10, 10, 3), None,
])
def test_every_rectangle_is_checked(owner, rect):
    t = valid_template()
    if owner in ("exam", "objective", "omr"):
        grid = {"exam": t.exam_no.grid, "objective": t.objective[0].grid,
                "omr": t.omr_fields[0].grid}[owner]
        grid.cells[0][0] = rect
    elif owner in ("anchors", "frames"):
        setattr(t.faces["front"].align, owner, [rect])
    elif owner == "sync":
        t.sync[0].marks[0] = rect
    elif owner == "choice":
        t.choose_one[0].cells[0] = rect
    else:
        region = {
            "absent": t.absent, "barcode": t.barcodes[0], "field": t.fields[0],
            "subjective": t.subjective[0].regions[0], "ignore": t.ignore[0],
        }[owner]
        region.rect = rect
    assert validate_template(t)


@pytest.mark.parametrize("owner", ["exam", "objective", "omr"])
@pytest.mark.parametrize("attr,value", [
    ("labels", []), ("labels", ["A", "A"]), ("labels", [" ", "B"]),
    ("labels", ["A"]), ("labels", None), ("labels", [None, "B"]),
    ("cells", []), ("cells", [[]]), ("cells", [None]), ("cells", None),
    ("face", "back"), ("face", []),
])
def test_every_grid_checks_shape_and_labels(owner, attr, value):
    t = valid_template()
    grid = {"exam": t.exam_no.grid, "objective": t.objective[0].grid,
            "omr": t.omr_fields[0].grid}[owner]
    setattr(grid, attr, value)
    assert validate_template(t)


@pytest.mark.parametrize("category", ["barcodes", "fields", "omr_fields", "choose_one"])
@pytest.mark.parametrize("key", ["", " ", None, []])
def test_field_keys_must_be_nonempty_strings(category, key):
    t = valid_template()
    getattr(t, category)[0].key = key
    assert validate_template(t)


@pytest.mark.parametrize("category", ["barcodes", "fields", "omr_fields", "choose_one"])
def test_field_keys_must_be_unique_within_category(category):
    t = valid_template()
    getattr(t, category).append(deepcopy(getattr(t, category)[0]))
    assert validate_template(t)


@pytest.mark.parametrize("attr,value", [
    ("start", 0), ("start", -1), ("start", 1.5), ("start", "1"),
    ("group", -1), ("qtype", "unknown"),
])
def test_invalid_objective_number_and_type(attr, value):
    t = valid_template()
    setattr(t.objective[0], attr, value)
    assert validate_template(t)


@pytest.mark.parametrize("attr,value", [
    ("no", ""), ("no", " "), ("no", None),
    ("sub", " "), ("sub", None), ("regions", []), ("regions", None),
    ("qtype", "unknown"), ("content", "unknown"),
])
def test_invalid_subjective_structure(attr, value):
    t = valid_template()
    t.choose_one = []
    setattr(t.subjective[0], attr, value)
    assert validate_template(t)


@pytest.mark.parametrize("questions", [
    ["2.1", "2.1"], ["2", "2.2"], ["1", ""], [None, "2.2"], [], None,
])
def test_choice_references_must_be_unique_existing_full_keys(questions):
    t = valid_template()
    t.choose_one[0].questions = questions
    assert validate_template(t)


def test_validation_collects_independent_semantic_problems():
    t = valid_template()
    t.dpi = 0
    t.subjective[0].regions = []
    t.fields[0].key = ""
    t.objective[0].grid.labels = ["A"]
    problems = validate_template(t)
    assert len(problems) >= 4
    assert any("标签" in p for p in problems)


@pytest.mark.parametrize("mutation", [
    lambda t: t.subjective.append(deepcopy(t.subjective[0])),
    lambda t: setattr(t.subjective[0], "no", "1"),
    lambda t: t.objective.append(deepcopy(t.objective[0])),
])
def test_duplicate_question_keys_are_rejected(mutation):
    t = valid_template()
    t.choose_one = []
    mutation(t)
    assert any("题号重复" in p for p in validate_template(t))


def test_empty_template_requires_a_face():
    t = Template("1.0", "Empty", 1, (210, 297), 300, {}, None, None, [], [], [], [])
    assert validate_template(t)


@pytest.mark.parametrize("value", [None, [], {}, "template"])
def test_non_template_input_returns_errors(value):
    assert validate_template(value)
