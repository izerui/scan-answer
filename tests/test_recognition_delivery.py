"""Evidence geometry and barcode candidates are part of the deliverable."""
from copy import deepcopy

import pytest

from test_delivery_audit import audit, batch as batch, dump, picture


def test_effective_objective_geometry_is_checked_against_template_and_shift(batch):
    root, _, _, result = batch
    item = result["objective"][0]
    item["sampling"] = {"face": "front", "shift_mm": [1., -.5], "reasons": [[None, None]]}
    region = item["regions"][0]
    region["template_rect"] = region["rect"]
    region["rect"] = [.5, 18., 20., 5.]
    dump(root / "students/0001/result.json", result)
    assert audit(root).ready
    region["rect"][0] += .1
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


@pytest.mark.parametrize("shift", [[5, 0], [True, 0], ["1", 0], [0], None])
def test_invalid_sampling_offsets_block_delivery(batch, shift):
    root, _, _, result = batch
    result["objective"][0]["sampling"] = {"face": "front", "shift_mm": shift, "reasons": [[None, None]]}
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


def with_barcode_candidates(batch):
    root, _, _, result = batch
    item = result["student"]["barcodes"]["id"]
    path = "images/barcode_candidates/key-id/candidate-1.jpg"
    item.update(status="ok", value="123", symbology="Code128", located="page",
                selected_id="front-1", hit_image=path, candidates=[{
                    "id": "front-1", "value": "123", "symbology": "Code128",
                    "face": "front", "polygon": [[30, 10], [40, 10], [40, 15], [30, 15]],
                    "rect": [30, 10, 10, 5], "image_rect": [28, 8, 14, 9],
                    "image": path, "located": "page",
                }])
    picture(root / "students/0001" / path)
    dump(root / "students/0001/result.json", result)
    return root, item


def test_barcode_candidates_are_inventoried_and_missing_images_block(batch):
    root, item = with_barcode_candidates(batch)
    report = audit(root)
    assert report.ready, report.issues
    relative = "students/0001/" + item["hit_image"]
    assert relative in report.files
    (root / relative).unlink()
    assert not audit(root).ready


@pytest.mark.parametrize("mutation", [
    lambda b: b.update(selected_id="missing"),
    lambda b: b.update(hit_image=b["image"]),
    lambda b: b.update(value="different"),
    lambda b: b.update(status="ambiguous"),
    lambda b: b["candidates"][0].update(face="back"),
    lambda b: b["candidates"][0].update(rect=[30, 10, 0, 5]),
    lambda b: b["candidates"][0].update(polygon=[[0, 0]]),
    lambda b: b["candidates"][0].update(image_rect=[0, 0, 14, 9]),
    lambda b: b["candidates"].append(deepcopy(b["candidates"][0])),
])
def test_inconsistent_barcode_candidate_metadata_blocks(batch, mutation):
    root, item = with_barcode_candidates(batch)
    mutation(item)
    dump(root / "students/0001/result.json", batch[3])
    assert not audit(root).ready


@pytest.mark.parametrize("identity", [[], {}, 12, "", None])
def test_malformed_candidate_identity_blocks_without_crashing(batch, identity):
    root, item = with_barcode_candidates(batch)
    item["selected_id"] = identity
    item["candidates"][0]["id"] = identity
    dump(root / "students/0001/result.json", batch[3])
    assert not audit(root).ready


@pytest.mark.parametrize("missing", ["candidates", "sampling"])
def test_v2_evidence_sections_cannot_be_deleted(batch, missing):
    root, item = with_barcode_candidates(batch)
    result = batch[3]
    result["scan"]["processing"] = {"evidence_version": 2, "resampling": "perspective_linear",
                                     "back_whitening": False, "back_whitening_level": None,
                                     "raw_stage": "as_received"}
    # Set up otherwise valid v2 evidence independently from the production builder.
    for entry, count in ((result["student"]["exam_no"], 2),
                         (result["student"]["omr_fields"]["room"], 1),
                         (result["objective"][0], 2)):
        entry["sampling"] = {"face": "front", "shift_mm": [0., 0.], "reasons": [[None] * count]}
    region = result["objective"][0]["regions"][0]
    region["template_rect"] = region["rect"]
    result["student"]["barcodes"]["room"].update(
        candidates=[], selected_id=None, hit_image=None, value=None, status="failed", located=None)
    dump(root / "students/0001/result.json", result)
    assert audit(root).ready
    if missing == "candidates":
        item.pop("candidates")
    else:
        result["objective"][0].pop("sampling")
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready
