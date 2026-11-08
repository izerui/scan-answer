"""Recognition failures and their persisted, visible image evidence."""
import json
from copy import deepcopy

import numpy as np
import pytest

from scan_answer.app.results import face_overlays
from scan_answer.engine.align import AlignResult
from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import (
    FacePlacement, SheetAnalysis, TemplateContext, analyze_sheet, decide_sheet,
)
from scan_answer.engine.result import build_result, render_images
from scan_answer.engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from scan_answer.template.model import (
    AlignSpec, Face, ObjectiveBlock, OmrGrid, Region, SubjectiveSpec, Template,
)


@pytest.fixture
def case():
    template = Template(
        schema_version="1.0", name="small", version=1, paper=(40, 30), dpi=254,
        faces={face: Face(f"{face}.png", AlignSpec("anchors")) for face in ("front", "back")},
        exam_no=None, absent=Region("front", (32, 10, 3, 2)),
        objective=[ObjectiveBlock("one", "single", 1,
                                  OmrGrid("front", ["A", "B"],
                                          [[(30, 5, 3, 2), (35, 5, 3, 2)]]))],
        subjective=[SubjectiveSpec("2", "short", "zh", [Region("back", (10, 10, 5, 5))])],
        barcodes=[], fields=[],
    )
    base = np.full((300, 400), 255, np.uint8)
    base[10:50, 10:50] = 0
    ctx = TemplateContext(template, {face: base.copy() for face in template.faces}, work_dpi=254)
    placements = {face: FacePlacement(i, 0, AlignResult(True, "anchors", H=np.diag([.1, .1, 1.])), 254)
                  for i, face in enumerate(template.faces)}
    return ctx, placements, base


def result_for(ctx, analysis, sources):
    decision = decide_sheet(ctx, analysis, Thresholds())
    images = render_images(ctx, analysis, decision, sources)
    result = build_result(ctx, analysis, decision, seq=1, batch_id="b", exam={},
                          scanner="folder", raw_paths=["a", "b"], image_paths=set(images))
    return result, images


def test_white_warp_padding_is_failed_not_blank_or_not_absent(case, monkeypatch):
    ctx, placements, base = case
    monkeypatch.setattr("scan_answer.engine.recognize.place_faces", lambda *args: (placements, False))
    analysis = analyze_sheet(ctx, [base[:, :300], base], [254, 254])
    decision = decide_sheet(ctx, analysis, Thresholds())
    assert decision.objective[0]["status"] == "failed"
    assert decision.objective[0]["fill"] == {"A": None, "B": None}
    assert decision.absent["value"] is None and decision.absent["status"] == "failed"
    assert analysis.sampling["objective/0"]["reasons"][0] == ["source_uncovered", "source_uncovered"]
    assert any(w["code"] == "SAMPLE_INVALID" for w in decision.warnings)
    assert analysis_fills(analysis).size == 0
    stored = json.loads(json.dumps(analysis_to_dict(analysis), allow_nan=False))
    restored = analysis_from_dict(stored)
    assert decide_sheet(ctx, restored, Thresholds()).absent["status"] == "failed"
    assert analysis_fills(restored).size == 0


def test_state_roundtrip_keeps_offsets_candidates_and_finite_json(case):
    ctx, placements, _ = case
    analysis = SheetAnalysis(placements, False, objective_fills=[np.array([[np.nan, .8]])])
    analysis.sampling = {"objective/0": {"face": "front", "shift_mm": [1., -.5],
                                         "reasons": [["source_uncovered", None]]}}
    analysis.barcode_evidence = {"id": {"status": "ambiguous", "selected_id": None, "candidates": []}}
    analysis.evidence_version = 2
    stored = analysis_to_dict(analysis)
    assert stored["objective_fills"] == [[[None, pytest.approx(.8)]]]
    json.dumps(stored, allow_nan=False)
    restored = analysis_from_dict(stored)
    assert restored.sampling == analysis.sampling
    assert restored.barcode_evidence == analysis.barcode_evidence
    assert restored.evidence_version == 2
    assert analysis_fills(restored).tolist() == [pytest.approx(.8)]
    assert decide_sheet(ctx, restored, Thresholds()).objective[0]["status"] == "failed"


def test_crop_and_overlay_use_effective_offset_and_back_keeps_light_ink(case):
    ctx, placements, base = case
    analysis = SheetAnalysis(placements, False, objective_fills=[np.array([[.8, 0.]])])
    analysis.evidence_version = 2
    analysis.sampling = {"objective/0": {"face": "front", "shift_mm": [1., .5],
                                         "reasons": [[None, None]]}}
    front = base.copy()
    front[55:75, 310:340] = 80
    back = base.copy()
    back[100:150, 100:150] = 210
    result, images = result_for(ctx, analysis, [front, back])
    region = result["objective"][0]["regions"][0]
    assert region["template_rect"] == [19.5, 3.5, 20., 5.]
    assert region["rect"] == [20.5, 4., 20., 5.]
    # The center of the marked A cell falls at (120, 25) in this crop.
    assert images[result["objective"][0]["image"]][25, 120] == 80
    assert face_overlays(ctx.tpl, result, "front")[0].rect == (31., 5.5, 3, 2)
    assert np.all(images[result["subjective"][0]["images"][0]] == 210)
    assert result["scan"]["processing"]["back_whitening"] is False


def test_legacy_state_retains_old_crop_policy_without_claiming_new_measurements(case):
    ctx, placements, base = case
    analysis = SheetAnalysis(placements, False, objective_fills=[np.array([[.8, 0.]])])
    old_state = analysis_to_dict(analysis)
    for key in ("sampling", "barcode_evidence", "evidence_version"):
        old_state.pop(key, None)
    restored = analysis_from_dict(old_state)
    back = base.copy()
    back[100:150, 100:150] = 210
    result, images = result_for(ctx, restored, [base, back])
    assert result["scan"]["processing"]["back_whitening"] is True
    assert np.all(images[result["subjective"][0]["images"][0]] == 255)
    assert not result["objective"][0].get("sampling")


def test_barcode_candidate_evidence_gets_images_and_keeps_region_crop(case):
    from scan_answer.template.model import BarcodeSpec
    ctx, placements, base = case
    ctx.tpl.barcodes = [BarcodeSpec("id", "front", (3, 20, 5, 3))]
    analysis = SheetAnalysis(placements, False, objective_fills=[np.array([[.8, 0.]])])
    analysis.evidence_version = 2
    analysis.barcodes = {"id": ("123", "Code128", "page")}
    analysis.barcode_evidence = {"id": {
        "status": "ok", "selected_id": "front-1", "reason": None,
        "candidates": [{"id": "front-1", "value": "123", "symbology": "Code128",
                        "face": "front", "polygon": [[20, 20], [25, 20], [25, 23], [20, 23]],
                        "rect": [20, 20, 5, 3], "located": "page"}],
    }}
    result, images = result_for(ctx, analysis, [base, base])
    barcode = result["student"]["barcodes"]["id"]
    candidate = barcode["candidates"][0]
    assert barcode["image"] == "images/barcodes/key-id.jpg"
    assert candidate["image"] == "images/barcode_candidates/key-id/candidate-1.jpg"
    assert barcode["hit_image"] == candidate["image"]
    assert candidate["image"] in images
    restored = analysis_from_dict(analysis_to_dict(analysis))
    assert restored.barcode_evidence == analysis.barcode_evidence
    ambiguous = deepcopy(analysis)
    ambiguous.barcodes["id"] = None
    ambiguous.barcode_evidence["id"].update(status="ambiguous", selected_id=None)
    decision = decide_sheet(ctx, ambiguous, Thresholds())
    assert decision.barcodes["id"]["status"] == "ambiguous"
    assert decision.barcodes["id"]["value"] is None
    assert any(w["code"] == "BARCODE_AMBIGUOUS" for w in decision.warnings)


@pytest.mark.parametrize("fills", [
    [np.array([[.8]])], [], [np.empty((0, 2))], [np.array([[.8, 0., 0.]])],
])
def test_incomplete_objective_measurements_cannot_be_confident_or_omitted(case, fills):
    ctx, placements, _ = case
    analysis = SheetAnalysis(placements, False, objective_fills=fills)
    decision = decide_sheet(ctx, analysis, Thresholds())
    assert len(decision.objective) == 1
    assert decision.objective[0]["status"] == "failed"
    assert decision.objective[0]["answer"] == ""


@pytest.mark.parametrize("kind", ["exam", "field", "choice"])
def test_incomplete_identity_or_choice_measurements_fail(case, kind):
    from scan_answer.template.model import ChooseOneSpec, ExamNoSpec, OmrFieldSpec
    ctx, placements, _ = case
    grid = deepcopy(ctx.tpl.objective[0].grid)
    analysis = SheetAnalysis(placements, False, objective_fills=[np.array([[.8, 0.]])])
    if kind == "exam":
        ctx.tpl.exam_no = ExamNoSpec(grid)
        analysis.exam_fills = np.array([[.8]])
    elif kind == "field":
        ctx.tpl.omr_fields = [OmrFieldSpec("group", "Group", grid)]
        analysis.omr_field_fills = {"group": np.array([[.8]])}
    else:
        ctx.tpl.choose_one = [ChooseOneSpec("group", "Group", "front", ["1", "2"], grid.cells[0])]
        analysis.choose_fills = {"group": np.array([.8])}
    decision = decide_sheet(ctx, analysis, Thresholds())
    item = (decision.exam_no if kind == "exam" else decision.omr_fields["group"]
            if kind == "field" else decision.choose_one[0])
    assert item["status"] == "failed"
