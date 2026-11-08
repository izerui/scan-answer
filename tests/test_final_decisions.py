import copy

import pytest

from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.worker import FinalizeJob, finalize_sheet, state_path
from test_portable_state import copied_batch as copied_batch, worker_batch as worker_batch


def test_finalization_updates_exam_digit_status_when_value_and_aggregate_do_not_change(copied_batch):
    batch, template, result_ref = copied_batch
    state = read_json(state_path(batch, 1))
    analysis = state["analysis"]
    analysis["exam_fills"] = [[0.9] + [0.0] * 9 for _ in analysis["exam_fills"]]
    analysis["exam_fills"][0][0] = 0.3
    for block in analysis["objective_fills"]:
        for row in block:
            row[:] = [0.9] + [0.0] * (len(row) - 1)
    for rows in analysis["omr_field_fills"].values():
        for row in rows:
            row[:] = [0.9] + [0.0] * (len(row) - 1)
    for row in analysis["choose_fills"].values():
        row[:] = [0.9] + [0.0] * (len(row) - 1)
    analysis["absent_fill"] = 0.0
    write_json_atomic(state_path(batch, 1), state)
    finalize_sheet(FinalizeJob(1, 0.45, 0.2, str(batch), str(template)))
    before = read_json(batch / result_ref)
    assert before["student"]["exam_no"]["digits"][0]["status"] == "uncertain"
    outcome = finalize_sheet(FinalizeJob(1, 0.6, 0.4, str(batch), str(template)))
    after = read_json(batch / result_ref)
    assert outcome.changed
    assert after["student"]["exam_no"]["value"] == before["student"]["exam_no"]["value"]
    assert after["student"]["exam_no"]["status"] == before["student"]["exam_no"]["status"]
    assert after["student"]["exam_no"]["digits"][0]["status"] == "blank"
    assert after["warnings"] == before["warnings"]


@pytest.mark.parametrize("detail", [
    "omr_digits", "consistent", "warnings", "absent", "options", "objective_fill", "barcode",
])
def test_finalization_replaces_stale_decision_details(copied_batch, detail):
    batch, template, result_ref = copied_batch
    path = batch / result_ref
    expected = read_json(path)
    stale = copy.deepcopy(expected)
    if detail == "omr_digits":
        stale["student"]["omr_fields"]["paper_type"]["digits"] = [
            {"pos": 1, "value": None, "status": "failed"},
        ]
    elif detail == "consistent":
        stale["student"]["consistent"] = not expected["student"]["consistent"]
    elif detail == "warnings":
        stale["warnings"].append({"code": "EXAM_NO_PARTIAL", "msg": "obsolete"})
    elif detail == "absent":
        stale["student"]["absent"]["status"] = "failed"
    elif detail == "options":
        stale["choose_one"][0]["questions"] = ["obsolete"]
    elif detail == "objective_fill":
        stale["objective"][0]["fill"] = {}
    elif detail == "barcode":
        stale["student"]["barcodes"]["student_barcode"]["located"] = "obsolete"
    write_json_atomic(path, stale)
    outcome = finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))
    assert outcome.changed
    assert read_json(path) == expected


@pytest.mark.parametrize(("legacy", "missing_crop"), [(False, True), (True, True), (True, False)])
def test_finalization_repairs_missing_evidence_and_preserves_ocr_identity(copied_batch, legacy, missing_crop):
    batch, template, result_ref = copied_batch
    path = batch / result_ref
    old = read_json(path)
    old["ocr_result_id"] = "result-generation"
    old["manual"] = {"exam_no": "reviewed"}
    for entry in old["subjective"] + list(old["student"]["fields"].values()):
        entry.update(text="already transcribed", ocr_status="failed", ocr_model="mock",
                     ocr_confidence=0.9, ocr_generation="active", ocr_fingerprint="fingerprint")
    old["warnings"].append({"code": "OCR_FAILED", "field": "subjective.56"})
    missing = path.parent / old["objective"][0]["image"]
    if missing_crop:
        missing.unlink()
    if legacy:
        for entry in old["objective"] + old["subjective"]:
            for key in ("question_id", "regions"):
                entry.pop(key)
        for entry in old["objective"]:
            entry.pop("image")
            entry.pop("images")
        old.pop("sheet_id")
        old.pop("input_pages")
    write_json_atomic(path, old)
    existing = {p: (p.read_bytes(), p.stat().st_mtime_ns)
                for p in (path.parent / "images").rglob("*.jpg")}
    outcome = finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template)))
    assert outcome.changed
    new = read_json(path)
    assert missing.is_file()
    for entry in new["objective"] + new["subjective"]:
        assert entry["question_id"] and entry["regions"]
        assert entry["images"] == [region["image"] for region in entry["regions"]]
        assert all((path.parent / ref).is_file() for ref in entry["images"])
    for key in ("content_revision", "ocr_result_id", "manual"):
        assert new[key] == old[key]
    for before, after in zip(old["subjective"] + list(old["student"]["fields"].values()),
                             new["subjective"] + list(new["student"]["fields"].values())):
        for key in ("text", "ocr_status", "ocr_model", "ocr_confidence", "ocr_generation", "ocr_fingerprint"):
            assert after[key] == before[key]
    assert new["warnings"] == old["warnings"]
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in existing} == existing
    stamp = path.stat().st_mtime_ns
    assert not finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template))).changed
    assert path.stat().st_mtime_ns == stamp


def test_finalization_restores_missing_input_metadata_from_state(copied_batch):
    batch, template, result_ref = copied_batch
    state = read_json(state_path(batch, 1))
    state["input_pages"] = [
        {"kind": "pdf", "raw": f"raw/0001_{face}.jpg", "document": "sources/source.pdf",
         "page": face + 2, "sha256": "source-digest"}
        for face in (1, 2)
    ]
    write_json_atomic(state_path(batch, 1), state)
    path = batch / result_ref
    old = read_json(path)
    old.pop("input_pages")
    write_json_atomic(path, old)
    assert finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(batch), str(template))).changed
    assert read_json(path)["input_pages"] == [
        {"kind": "pdf", "raw": "../../raw/0001_1.jpg", "document": "../../sources/source.pdf",
         "page": 3, "sha256": "source-digest"},
        {"kind": "pdf", "raw": "../../raw/0001_2.jpg", "document": "../../sources/source.pdf",
         "page": 4, "sha256": "source-digest"},
    ]


def test_actual_v1_state_finalizes_without_rewriting_active_ocr_evidence(copied_batch):
    from scan_answer.engine.omr import Thresholds
    from scan_answer.engine.recognize import decide_sheet
    from scan_answer.engine.result import build_result, render_images
    from scan_answer.engine.state import analysis_from_dict
    from scan_answer.ocr.queue import OcrQueue
    from scan_answer.output.local import write_jpg
    from scan_answer.pipeline.worker import get_context
    from scan_answer.template.model import subjective_item_key
    from scan_answer.imageio import imread_gray
    from test_ocr_ownership import CFG, GatedClient

    batch, template, result_ref = copied_batch
    path = batch / result_ref
    state = read_json(state_path(batch, 1))
    for key in ("sampling", "barcode_evidence", "evidence_version", "absent_invalid"):
        state["analysis"].pop(key, None)
    write_json_atomic(state_path(batch, 1), state)
    analysis = analysis_from_dict(state["analysis"])
    ctx = get_context(str(template))
    decision = decide_sheet(ctx, analysis, Thresholds())
    sources = [imread_gray(batch / reference) for reference in state["raw_paths"]]
    images = render_images(ctx, analysis, decision, sources)
    for reference, image in images.items():
        write_jpg(path.parent / reference, image)
    current = read_json(path)
    legacy = build_result(ctx, analysis, decision, seq=1, batch_id=current["batch_id"],
                          exam=current["exam"], scanner="folder",
                          raw_paths=[current["pages"]["raw_1"], current["pages"]["raw_2"]],
                          image_paths=set(images), ocr=True)
    legacy["scan"].pop("processing")
    write_json_atomic(path, legacy)
    client = GatedClient("legacy transcription")
    queue = OcrQueue(CFG, client=client)
    try:
        key = subjective_item_key(legacy["subjective"][0])
        assert queue.enqueue_item(path, "subjective", key) == 1
        assert client.entered.wait(5)
        claimed = read_json(path)
        inventory = {reference: ((path.parent / reference).read_bytes(),
                                 (path.parent / reference).stat().st_mtime_ns) for reference in images}
        outcome = finalize_sheet(FinalizeJob(1, .45, .2025, str(batch), str(template)))
        assert outcome.changed
        finalized = read_json(path)
        assert finalized["scan"]["processing"]["evidence_version"] == 1
        assert finalized["scan"]["processing"]["back_whitening"] is True
        assert "sampling" not in finalized["objective"][0]
        assert finalized["subjective"][0]["ocr_generation"] == claimed["subjective"][0]["ocr_generation"]
        assert inventory == {reference: ((path.parent / reference).read_bytes(),
                                         (path.parent / reference).stat().st_mtime_ns) for reference in images}
    finally:
        client.release.set()
        queue.close()
    completed = read_json(path)["subjective"][0]
    assert completed["ocr_status"] == "done"
    assert completed["text"] == "legacy transcription"
