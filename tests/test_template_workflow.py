"""Data operations for the template editing workflow."""
from __future__ import annotations

import copy
import numpy as np
import pytest

from scan_answer.template.auto import ImportedBase, SnapResult
from scan_answer.template.doc import RegionRef, TemplateDoc, UndoStack, _safe_filename
from scan_answer.template.model import AlignSpec, Face, Template


@pytest.fixture
def doc():
    template = Template(
        schema_version="1.0", name="workflow", version=0,
        paper=(210.0, 297.0), dpi=300,
        faces={
            "front": Face("front.png", AlignSpec("frames", frames=[(5, 5, 190, 280)])),
            "back": Face("back.png", AlignSpec("frames", frames=[(6, 6, 190, 280)])),
        },
        exam_no=None, absent=None, barcodes=[], fields=[], objective=[], subjective=[],
    )
    return TemplateDoc(template, {}, None)


def test_revision_tracks_content_and_undo_redo_but_not_noops(doc):
    assert doc.revision == 0
    stack = UndoStack(doc)
    stack.push("add")
    ref = doc.add("subjective", "front", (10, 20, 30, 40))
    assert doc.revision == 1
    doc.set_rect(ref, doc.rect(ref))
    doc.set_params(ref, doc.params(ref))
    doc.remove([])
    assert doc.revision == 1
    doc.set_rect(ref, (11, 20, 30, 40))
    assert doc.revision == 2
    assert stack.undo()
    assert doc.revision == 3
    assert not doc.template.subjective
    assert stack.redo()
    assert doc.revision == 4
    assert doc.rect(ref) == (11, 20, 30, 40)
    doc.remove([ref])
    assert doc.revision == 5


def test_undo_push_accepts_independent_pre_mutation_snapshot(doc):
    stack = UndoStack(doc)
    before = doc.snapshot()
    ref = doc.add("subjective", "back", (10, 20, 30, 40))
    stack.push("add", snapshot=before)
    before["name"] = "changed outside document"
    before["faces"]["front"]["align"]["frames"].clear()

    assert stack.undo()
    assert not doc.template.subjective
    assert doc.template.name == "workflow"
    assert doc.template.faces["front"].align.frames == [(5, 5, 190, 280)]
    assert doc.revision == 2
    assert stack.redo()
    assert doc.face_of(ref) == "back"
    assert doc.rect(ref) == (10, 20, 30, 40)
    assert doc.revision == 3


def test_save_versions_survive_undo_redo_and_failed_save(tpl_bases, tmp_path, monkeypatch):
    from scan_answer.template import doc as module
    from scan_answer.template.io import load_template

    template, bases = tpl_bases
    doc = TemplateDoc(copy.deepcopy(template), bases, None)
    stack = UndoStack(doc)
    initial_version = doc.template.version
    stack.push("add")
    doc.add("field", "front", (100, 100, 20, 10))
    doc.save(tmp_path / "saved")
    assert doc.revision == 1
    assert doc._saved_version == initial_version + 1
    assert stack.undo()
    assert doc.template.version == initial_version + 1
    doc.save()
    assert doc.template.version == initial_version + 2
    assert doc.revision == 2
    assert stack.redo()
    doc.save()
    assert doc.template.version == initial_version + 3
    assert doc.revision == 3
    saved, _ = load_template(doc.directory)
    assert saved.version == initial_version + 3
    assert saved.faces == doc.template.faces

    before = doc.snapshot()
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(module, "save_template", fail)
    with pytest.raises(OSError, match="disk full"):
        doc.save()
    assert doc.snapshot() == before
    assert doc._saved_version == initial_version + 3
    assert doc.revision == 3
    assert not doc.dirty


def test_new_template_first_save_validates_prospective_version(tpl_bases, tmp_path):
    from scan_answer.template.io import load_template
    from scan_answer.template.validate import validate_template

    template, bases = tpl_bases
    front = ImportedBase(bases["front"], template.paper, 0, "anchors")
    doc = TemplateDoc.new("first save", {"front": front})
    assert doc.template.version == 0
    assert validate_template(doc.template)  # Version 0 remains invalid on disk.
    assert doc.problems() == []
    assert doc.template.version == 0
    assert doc.revision == 0
    assert doc.dirty

    directory = doc.save(tmp_path / "first")
    saved, saved_bases = load_template(directory)
    assert validate_template(saved) == []
    assert saved.version == doc.template.version == doc._saved_version == 1
    assert doc.revision == 0
    assert not doc.dirty
    np.testing.assert_array_equal(saved_bases["front"], bases["front"])


@pytest.mark.parametrize("name", [".", "..", "...", "CON", "con.txt", "NUL", "AUX",
                                      "COM1", "LPT9.log", " name. ", "bad\x00name"])
def test_safe_filename_can_be_used_as_a_child_directory(tmp_path, name):
    filename = _safe_filename(name)
    assert filename not in {"", ".", ".."}
    assert filename == filename.rstrip(" .")
    assert filename.split(".")[0].upper() not in {
        "CON", "NUL", "AUX", "PRN", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    child = tmp_path / filename
    assert child.parent == tmp_path
    child.mkdir()


@pytest.mark.parametrize("kind,method", [("anchor", "anchors"), ("frame", "frames")])
def test_manual_alignment_activates_mode_and_returns_own_global_index(doc, kind, method):
    doc.add(kind, "back", (15, 15, 3, 3))
    ref = doc.add(kind, "front", (20, 20, 4, 4))
    assert doc.template.faces["front"].align.method == method
    assert doc.face_of(ref) == "front"
    assert doc.rect(ref) == (20, 20, 4, 4)
    assert ref in doc.regions("front")
    assert ref not in doc.regions("back")


def test_switch_alignment_preserves_inactive_rectangles_and_is_undoable(doc):
    frame = copy.deepcopy(doc.template.faces["front"].align.frames)
    doc.add("anchor", "front", (20, 20, 4, 4))
    stack = UndoStack(doc)
    stack.push("switch")
    doc.set_alignment_method("front", "frames")
    align = doc.template.faces["front"].align
    assert align.anchors == [(20, 20, 4, 4)]
    assert align.frames == frame
    assert [r.kind for r in doc.regions("front")] == ["frame"]
    revision = doc.revision
    doc.set_alignment_method("front", "frames")
    assert doc.revision == revision
    assert stack.undo()
    assert doc.template.faces["front"].align.method == "anchors"
    before = doc.snapshot()
    for face, method in [("missing", "frames"), ("front", "invalid")]:
        with pytest.raises(ValueError):
            doc.set_alignment_method(face, method)
        assert doc.snapshot() == before


def test_subjective_regions_append_across_faces_and_reorder_with_undo(doc):
    ref = doc.add("subjective", "front", (10, 20, 30, 40))
    second = doc.add_subjective_region(ref.index, "back", (20, 30, 40, 50))
    assert second == RegionRef("subjective", 0, 1)
    stack = UndoStack(doc)
    stack.push("reorder")
    moved = doc.move_subjective_region(0, 1, 0)
    assert moved == RegionRef("subjective", 0, 0)
    assert doc.face_of(moved) == "back"
    assert doc.rect(moved) == (20, 30, 40, 50)
    assert doc.revision == 3
    doc.move_subjective_region(0, 0, 0)
    assert doc.revision == 3
    assert stack.undo()
    assert [r.face for r in doc.template.subjective[0].regions] == ["front", "back"]
    assert stack.redo()
    assert [r.face for r in doc.template.subjective[0].regions] == ["back", "front"]


@pytest.mark.parametrize("index,face,rect", [
    (-1, "front", (10, 10, 10, 10)), (3, "front", (10, 10, 10, 10)),
    (0, "missing", (10, 10, 10, 10)), (0, "back", (-1, 10, 10, 10)),
    (0, "back", (200, 10, 20, 10)), (0, "back", (10, 10, 0, 10)),
    (0, "back", (float("nan"), 10, 10, 10)),
])
def test_invalid_append_is_rejected_without_mutation(doc, index, face, rect):
    doc.add("subjective", "front", (10, 10, 10, 10))
    before = doc.snapshot()
    with pytest.raises((ValueError, IndexError)):
        doc.add_subjective_region(index, face, rect)
    assert doc.snapshot() == before
    assert doc.revision == 1


@pytest.mark.parametrize("index,old,new", [(-1, 0, 0), (0, -1, 0), (0, 0, 1)])
def test_invalid_move_is_rejected_without_mutation(doc, index, old, new):
    doc.add("subjective", "front", (10, 10, 10, 10))
    before = doc.snapshot()
    with pytest.raises((ValueError, IndexError)):
        doc.move_subjective_region(index, old, new)
    assert doc.snapshot() == before
    assert doc.revision == 1


def _subjective_choice(doc):
    first = doc.add("subjective", "front", (10, 20, 30, 40))
    second = doc.add("subjective", "back", (10, 20, 30, 40))
    doc.set_params(first, {"no": "10", "sub": "a"})
    doc.set_params(second, {"no": "10", "sub": "b"})
    choice = doc.add("choose_one", "front", (100, 20, 30, 5))
    return first, second, choice


def test_subjective_full_keys_default_and_rename_updates_references(doc):
    first, _, choice = _subjective_choice(doc)
    assert doc.params(choice)["questions"] == ["10.a", "10.b"]
    stack = UndoStack(doc)
    stack.push("rename")
    doc.set_params(first, {"no": "11", "sub": "c"})
    assert doc.params(choice)["questions"] == ["11.c", "10.b"]
    assert stack.undo()
    assert doc.params(choice)["questions"] == ["10.a", "10.b"]
    assert stack.redo()
    assert doc.params(choice)["questions"] == ["11.c", "10.b"]


@pytest.mark.parametrize("params", [
    {"no": "10", "sub": "b", "content": "en"}, {"no": " ", "qtype": "fill"},
    {"no": "", "sub": "x"}, {"no": "10", "sub": "   "},
])
def test_subjective_key_errors_are_transactional(doc, params):
    first, _, _ = _subjective_choice(doc)
    before = doc.snapshot()
    revision = doc.revision
    with pytest.raises(ValueError):
        doc.set_params(first, params)
    assert doc.snapshot() == before
    assert doc.revision == revision


def test_objective_renumber_updates_references_by_position_without_moving_cells(doc):
    objective = doc.add("objective", "front", (10, 20, 30, 40))
    choice = doc.add("choose_one", "front", (100, 20, 30, 5))
    doc.set_params(choice, {"questions": ["1", "3"]})
    block = doc.template.objective[0]
    cells = copy.deepcopy(block.grid.cells)
    doc.set_params(objective, {"start": 2})
    assert block.start == 2
    assert block.grid.cells == cells
    assert doc.params(choice)["questions"] == ["2", "4"]
    doc.set_params(objective, {"start": 20, "n_questions": 3})
    assert doc.params(choice)["questions"] == ["20", "22"]


def test_referenced_questions_cannot_be_removed_or_truncated(doc):
    objective = doc.add("objective", "front", (10, 20, 30, 40))
    choice = doc.add("choose_one", "front", (100, 20, 30, 5))
    doc.set_params(choice, {"questions": ["1", "5"]})
    before = doc.snapshot()
    with pytest.raises(ValueError, match="选做"):
        doc.set_params(objective, {"start": 20, "n_questions": 4})
    assert doc.snapshot() == before
    with pytest.raises(ValueError, match="选做"):
        doc.remove([objective])
    assert doc.snapshot() == before
    doc.remove([objective, choice])
    assert not doc.template.objective
    assert not doc.template.choose_one


def test_referenced_subjective_allows_partial_region_removal_only(doc):
    first, _, choice = _subjective_choice(doc)
    doc.add_subjective_region(first.index, "back", (20, 20, 20, 20))
    doc.remove([first, first])
    assert len(doc.template.subjective[0].regions) == 1
    before = doc.snapshot()
    with pytest.raises(ValueError, match="选做"):
        doc.remove([first])
    assert doc.snapshot() == before
    doc.set_params(choice, {"questions": ["10.b", "10.a"]})
    doc.remove([choice, first])
    assert [s.key for s in doc.template.subjective] == ["10.b"]


@pytest.mark.parametrize("params", [
    {"start": 6}, {"start": 20}, {"n_questions": 0, "start": 100},
    {"n_options": 0}, {"labels": []}, {"n_questions": -1}, {"group": -1},
])
def test_objective_invalid_changes_leave_no_partial_updates(doc, params):
    first = doc.add("objective", "front", (10, 20, 30, 40))
    doc.add("objective", "back", (10, 20, 30, 40))
    subj = doc.add("subjective", "front", (50, 20, 30, 40))
    doc.set_params(subj, {"no": "20", "sub": "a"})
    before = doc.snapshot()
    with pytest.raises(ValueError):
        doc.set_params(first, params)
    assert doc.snapshot() == before


def test_subjective_cannot_collide_with_objective_even_with_subkey(doc):
    doc.add("objective", "front", (10, 20, 30, 40))
    subjective = doc.add("subjective", "front", (50, 20, 30, 40))
    with pytest.raises(ValueError):
        doc.set_params(subjective, {"no": "1", "sub": "a"})
    assert doc.params(subjective)["no"] == "6"


def test_choice_count_changes_resize_in_original_bbox_same_count_keeps_cells(doc):
    doc.add("objective", "front", (10, 20, 30, 40))
    choice = doc.add("choose_one", "front", (100, 20, 30, 6))
    doc.set_params(choice, {"questions": ["1", "2", "3"]})
    assert doc.template.choose_one[0].cells == [
        (100, 20, 10, 6), (110, 20, 10, 6), (120, 20, 10, 6),
    ]
    cells = copy.deepcopy(doc.template.choose_one[0].cells)
    doc.set_params(choice, {"questions": ["3", "4", "5"]})
    assert doc.template.choose_one[0].cells == cells
    doc.set_params(choice, {"questions": ["1", "2"]})
    assert doc.template.choose_one[0].cells == [(100, 20, 15, 6), (115, 20, 15, 6)]
    before = doc.snapshot()
    for questions in [[], ["1", "1"], ["1", "missing"]]:
        with pytest.raises(ValueError):
            doc.set_params(choice, {"key": "renamed", "questions": questions})
        assert doc.snapshot() == before


@pytest.mark.parametrize("kind", ["field", "barcode", "omr_field", "choose_one"])
def test_key_collisions_and_empty_keys_are_transactional(doc, kind):
    first = doc.add(kind, "front", (10, 20, 30, 40))
    second = doc.add(kind, "back", (10, 20, 30, 40))
    for key in ["", " ", doc.params(second)["key"]]:
        before = doc.snapshot()
        with pytest.raises(ValueError):
            doc.set_params(first, {"key": key, "label": "changed"})
        assert doc.snapshot() == before


def test_grid_regeneration_failure_is_transactional(doc, monkeypatch):
    from scan_answer.template import doc as module
    ref = doc.add("objective", "front", (10, 20, 30, 40))
    before = doc.snapshot()
    def fail(*args, **kwargs):
        raise ValueError("bad layout")
    monkeypatch.setattr(module, "grid_in_rect", fail)
    with pytest.raises(ValueError, match="bad layout"):
        doc.set_params(ref, {"start": 30, "n_questions": 3})
    assert doc.snapshot() == before


def _imported(paper, shape=None):
    if shape is None:
        shape = (round(paper[1] * 300 / 25.4), round(paper[0] * 300 / 25.4))
    return ImportedBase(np.full(shape, 255, dtype=np.uint8), paper, 0, "none")


@pytest.mark.parametrize("paper", [(297, 420), (420, 297), (390, 270), (421, 298)])
def test_new_accepts_a3_and_8k_in_both_orientations(paper):
    doc = TemplateDoc.new("paper", {"front": _imported(paper)})
    assert doc.template.paper == paper
    assert doc.template.dpi == 300
    assert doc.revision == 0
    assert doc.dirty


@pytest.mark.parametrize("paper", [(298.1, 421), (297, 421.1), (500, 200)])
def test_new_rejects_paper_over_a3(paper):
    with pytest.raises(ValueError, match="A3"):
        TemplateDoc.new("oversize", {"front": _imported(paper)})


def test_new_normalizes_tolerated_faces_to_first_pixels_before_detection(monkeypatch):
    from scan_answer.template import doc as module
    faces = {"front": _imported((210, 297)), "back": _imported((211, 296))}
    original_shape = faces["back"].image.shape
    assert TemplateDoc.validate_imported_faces(faces) is None
    seen = []
    def anchors(image, dpi, paper):
        seen.append((image.shape, dpi, paper))
        return None
    monkeypatch.setattr(module, "auto_anchors", anchors)
    doc = TemplateDoc.new("two faces", faces)
    assert doc.bases["back"].shape == faces["front"].image.shape
    assert doc.template.paper == (210, 297)
    assert seen == [(faces["front"].image.shape, 300, (210, 297))] * 2
    assert faces["back"].image.shape == original_shape


@pytest.mark.parametrize("back", [(297, 210), (212, 297), (210, 295.9)])
def test_new_and_preaccept_reject_mismatched_faces(back):
    faces = {"front": _imported((210, 297)), "back": _imported(back)}
    for operation in [TemplateDoc.validate_imported_faces,
                      lambda faces: TemplateDoc.new("bad", faces)]:
        with pytest.raises(ValueError, match="尺寸|方向"):
            operation(faces)


def test_new_rejects_empty_or_unknown_faces():
    for faces in [{}, {"sideways": _imported((210, 297))}]:
        with pytest.raises(ValueError):
            TemplateDoc.new("bad", faces)


def test_snap_and_auto_alignment_revision_only_changes_when_applied(doc, monkeypatch):
    from scan_answer.template import doc as module
    ref = doc.add("objective", "front", (10, 20, 30, 40))
    cells = copy.deepcopy(doc.template.objective[0].grid.cells)
    cells[0][0] = (11, 21, 2, 2)
    snap = SnapResult(cells, True, 20, 20, 0.1, "ok")
    doc.apply_snap(ref, snap)
    assert doc.revision == 2
    doc.apply_snap(ref, snap)
    assert doc.revision == 2
    doc.bases["front"] = np.full((10, 10), 255, dtype=np.uint8)
    monkeypatch.setattr(module, "auto_anchors", lambda *args: [(1, 1, 2, 2)])
    doc.auto_align("front", "anchors")
    assert doc.revision == 3
    doc.auto_align("front", "anchors")
    assert doc.revision == 3
