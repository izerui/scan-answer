"""Regression tests for evidence assignment and explicit OCR delivery status."""
from copy import deepcopy
import json
import shutil

import pytest

from conftest import TEMPLATE_DIR
from scan_answer.output.audit import audit_batch
from scan_answer.output.delivery import DeliveryError, export_delivery
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.documents import DocumentSource
from test_delivery_audit import batch as legacy_batch_fixture
from test_delivery_audit import dump, picture

__all__ = ["legacy_batch_fixture"]


@pytest.fixture
def canonical_batch(legacy_batch_fixture):
    root, manifest, template, result = legacy_batch_fixture
    base = root / "students/0001"
    replacements = {
        "images/front.png": "images/pages/front.jpg",
        "images/exam.png": "images/identity/exam_no.jpg",
        "images/name.png": "images/fields/key-name.jpg",
        "images/barcode-id.png": "images/barcodes/key-id.jpg",
        "images/barcode-room.png": "images/barcodes/key-room.jpg",
        "images/room.png": "images/omr_fields/key-room.jpg",
        "images/q1.png": "images/objective/q-1.jpg",
        "images/q2-1.png": "images/subjective/q-2/sub-a/region-1.jpg",
        "images/q2-2.png": "images/subjective/q-2/sub-a/region-2.jpg",
    }
    for index, (old, new) in enumerate(replacements.items()):
        picture(base / new, color=20 + index * 20)
        (base / old).unlink(missing_ok=True)

    def replace_references(value):
        if isinstance(value, dict):
            return {key: replace_references(child) for key, child in value.items()}
        if isinstance(value, list):
            return [replace_references(child) for child in value]
        return replacements.get(value, value) if isinstance(value, str) else value

    result = replace_references(result)
    # Add a second objective and two distinct subjective identities so swaps
    # preserve coverage, geometry, order, and the set of referenced files.
    block = deepcopy(template["objective"][0])
    block.update(name="4", start=4)
    block["grid"]["cells"] = [[[10, 25, 3, 2], [15, 25, 3, 2]]]
    template["objective"].append(block)
    second = deepcopy(result["objective"][0])
    second.update(no="4", question_id="objective/4")
    second["regions"][0]["rect"] = [-0.5, 23.5, 20, 5]
    set_images(second, ["images/objective/q-4.jpg"])
    result["objective"].append(second)
    picture(base / second["image"], color=200)
    for no, sub, directory in (("2", "b", "q-2/sub-b"), ("3", "a", "q-3/sub-a")):
        spec = deepcopy(template["subjective"][0])
        spec.update(no=no, sub=sub)
        template["subjective"].append(spec)
        question = deepcopy(result["subjective"][0])
        question.update(no=no, sub=sub, question_id=f"subjective/{no}/{sub}")
        set_images(question, [f"images/subjective/{directory}/region-{i}.jpg" for i in (1, 2)])
        result["subjective"].append(question)
        for reference in question["images"]:
            picture(base / reference, color=220)
    dump(root / "template/template.json", template)
    dump(root / "template.json", template)
    dump(base / "result.json", result)
    return root, manifest, result, base / "result.json"


@pytest.fixture(scope="module")
def generated_seed(tmp_path_factory, small_dataset):
    session = BatchSession.create(
        tmp_path_factory.mktemp("audit-review-seed"), TEMPLATE_DIR, batch_id="review",
    )
    with BatchPipeline(session) as pipeline:
        pipeline.run(DocumentSource(sorted(small_dataset.glob("*.jpg"))[:2], 2))
        pipeline.finalize()
    return session.batch_dir


@pytest.fixture
def generated_batch(generated_seed, tmp_path):
    root = tmp_path / "generated"
    shutil.copytree(generated_seed, root)
    manifest = json.loads((root / "manifest.json").read_text())
    path = root / manifest["sheets"][0]["result"]
    return root, manifest, json.loads(path.read_text()), path


@pytest.fixture(params=["canonical_batch", "generated_batch"], ids=["hand-built", "pipeline"])
def review_batch(request):
    batch = request.getfixturevalue(request.param)
    report = audit_batch(batch[0])
    assert report.ready, report.issues
    return batch


def set_images(question, references):
    question["images"] = references
    for region, reference in zip(question["regions"], references, strict=True):
        region["image"] = reference
    if "image" in question:
        question["image"] = references[0]


def assert_blocked_delivery(root, path, result, tmp_path, code):
    dump(path, result)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    report = audit_batch(root)
    assert not report.ready, report.issues
    assert any(issue["code"] == code and issue["seq"] == 1
               and issue["severity"] == "error" for issue in report.issues), report.issues
    target = tmp_path / "rejected-delivery"
    with pytest.raises(DeliveryError):
        export_delivery(root, target)
    assert not target.exists()
    assert before == {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_synchronized_region_image_reversal_blocks_delivery(review_batch, tmp_path):
    root, _, result, path = review_batch
    question = result["subjective"][0]
    geometry = [(region["order"], region["face"], region["rect"])
                for region in question["regions"]]
    set_images(question, list(reversed(question["images"])))
    assert geometry == [(region["order"], region["face"], region["rect"])
                        for region in question["regions"]]
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


@pytest.mark.parametrize("category", ["objective", "subjective"])
def test_cross_question_image_swap_blocks_delivery(review_batch, tmp_path, category):
    root, _, result, path = review_batch
    left = result[category][0]
    right = next(q for q in result[category] if q["no"] != left["no"])
    left_images, right_images = left["images"], right["images"]
    set_images(left, right_images)
    set_images(right, left_images)
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


def test_cross_subquestion_image_swap_blocks_delivery(canonical_batch, tmp_path):
    root, _, result, path = canonical_batch
    assert audit_batch(root).ready
    left, right = result["subjective"][:2]
    left_images, right_images = left["images"], right["images"]
    set_images(left, right_images)
    set_images(right, left_images)
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


@pytest.mark.parametrize("category,key", [
    ("fields", "name"), ("barcodes", "id"), ("omr_fields", "room"), ("exam_no", None),
])
def test_student_evidence_cannot_exchange_with_question(canonical_batch, tmp_path, category, key):
    root, _, result, path = canonical_batch
    assert audit_batch(root).ready
    entry = result["student"][category]
    if key is not None:
        entry = entry[key]
    question = result["objective"][0]
    student_image = entry["image"]
    entry["image"] = question["image"]
    set_images(question, [student_image])
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


def test_barcode_images_cannot_exchange_keys(canonical_batch, tmp_path):
    root, _, result, path = canonical_batch
    assert audit_batch(root).ready
    left, right = result["student"]["barcodes"].values()
    left["image"], right["image"] = right["image"], left["image"]
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


@pytest.mark.parametrize("category,key", [
    ("fields", "name"), ("barcodes", "id"), ("omr_fields", "room"), ("exam_no", None),
])
def test_student_evidence_must_use_its_expected_path(canonical_batch, tmp_path, category, key):
    root, _, result, path = canonical_batch
    assert audit_batch(root).ready
    entry = result["student"][category]
    if key is not None:
        entry = entry[key]
    duplicate = "images/unassigned.jpg"
    shutil.copyfile(path.parent / entry["image"], path.parent / duplicate)
    entry["image"] = duplicate
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


def test_noncanonical_duplicate_image_is_not_valid_evidence(review_batch, tmp_path):
    root, _, result, path = review_batch
    question = result["objective"][0]
    duplicate = "images/unassigned.jpg"
    shutil.copyfile(path.parent / question["image"], path.parent / duplicate)
    set_images(question, [duplicate])
    assert_blocked_delivery(root, path, result, tmp_path, "evidence_identity")


def test_relative_aliases_of_expected_evidence_remain_deliverable(review_batch, tmp_path):
    root, _, result, path = review_batch
    for question in result["objective"] + result["subjective"]:
        question["images"] = ["./" + reference for reference in question["images"]]
        for region in question["regions"]:
            region["image"] = "images/../" + region["image"]
    for category in ("fields", "barcodes", "omr_fields"):
        for entry in result["student"][category].values():
            entry["image"] = entry["image"].replace("/", "\\")
    result["student"]["exam_no"]["image"] = "./" + result["student"]["exam_no"]["image"]
    dump(path, result)
    report = audit_batch(root)
    assert report.ready, report.issues
    target = export_delivery(root, tmp_path / "aliased-delivery")
    assert audit_batch(target).ready


@pytest.mark.parametrize("where", ["subjective", "field"])
@pytest.mark.parametrize("manifest_ocr", [True, False, None], ids=["enabled", "disabled", "absent"])
def test_removing_ocr_status_cannot_clear_delivery_gate(review_batch, tmp_path, where, manifest_ocr):
    root, manifest, result, path = review_batch
    if manifest_ocr is None:
        manifest.pop("ocr", None)
    else:
        manifest["ocr"] = manifest_ocr
    dump(root / "manifest.json", manifest)
    entry = result["subjective"][0] if where == "subjective" else next(iter(result["student"]["fields"].values()))
    entry["ocr_status"] = "pending"
    dump(path, result)
    pending = audit_batch(root)
    assert not pending.ready
    assert pending.summary["ocr_pending"] == 1
    del entry["ocr_status"]
    assert_blocked_delivery(root, path, result, tmp_path, "ocr_status_invalid")


@pytest.mark.parametrize("where", ["subjective", "field"])
@pytest.mark.parametrize("status,ready", [
    ("none", True), ("done", True), ("failed", True),
    ("pending", False), (None, False), ("unknown", False), (False, False), ([], False),
])
def test_ocr_capable_entries_require_explicit_valid_status(canonical_batch, where, status, ready):
    root, manifest, result, path = canonical_batch
    manifest["ocr"] = True
    dump(root / "manifest.json", manifest)
    entry = result["subjective"][0] if where == "subjective" else result["student"]["fields"]["name"]
    entry["ocr_status"] = status
    dump(path, result)
    report = audit_batch(root)
    assert report.ready is ready, report.issues
    if status == "failed":
        assert report.summary["ocr_failed"] == 1
        assert any(issue["code"] == "ocr_failed" and issue["severity"] == "warning"
                   for issue in report.issues)
    elif status not in ("none", "done", "pending"):
        assert any(issue["code"] == "ocr_status_invalid" for issue in report.issues)
