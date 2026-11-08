"""Read-only delivery checks against independently assembled on-disk evidence."""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import importlib
import json
import os
import shutil

from PIL import Image
import pypdfium2
import pytest


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def picture(path, color=180):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", (24, 16), color).save(path)


def audit(root):
    return importlib.import_module("scan_answer.output.audit").audit_batch(root)


@pytest.fixture
def batch(tmp_path):
    root = tmp_path / "batch"
    template = {
        "schema_version": "1.0", "name": "card", "version": 1,
        "paper": [80, 60], "dpi": 200,
        "faces": {"front": {"image": "front.png", "align": {"method": "anchors",
                   "anchors": [[1, 1, 1, 1], [70, 1, 1, 1], [1, 50, 1, 1], [70, 50, 1, 1]]}}},
        "exam_no": {"grid": {"face": "front", "labels": ["0", "1"],
                            "cells": [[[2, 2, 2, 2], [5, 2, 2, 2]]]}},
        "fields": [{"key": "name", "label": "Name", "face": "front", "rect": [2, 5, 20, 5]}],
        "barcodes": [{"key": key, "face": "front", "rect": [2, 10, 20, 5]}
                     for key in ("id", "room")],
        "omr_fields": [{"key": "room", "label": "Room", "grid": {
            "face": "front", "labels": ["1"], "cells": [[[2, 15, 2, 2]]]}}],
        "objective": [{"name": "1", "qtype": "single", "start": 1, "grid": {
            "face": "front", "labels": ["A", "B"],
            "cells": [[[10, 20, 3, 2], [15, 20, 3, 2]]]}}],
        "subjective": [{"no": "2", "sub": "a", "qtype": "short", "content": "en",
                        "regions": [{"face": "front", "rect": rect}
                                    for rect in ([2, 30, 20, 5], [2, 40, 20, 5])]}],
    }
    origins = [{"kind": "image", "file": "scan.png", "page": 1, "raw": "raw/0001_scan.png"}]
    manifest = {
        "schema_version": "1.0", "batch_id": "batch", "finalized": True,
        "template": {"name": "card", "version": 1, "snapshot": "template/template.json"},
        "sheets": [{"seq": 1, "status": "ok", "files": ["scan.png"],
                    "input_pages": origins, "result": "students/0001/result.json", "warnings": 0}],
    }
    result = {
        "schema_version": "1.0", "seq": 1, "batch_id": "batch", "sheet_id": "batch:0001",
        "template": {"name": "card", "version": 1},
        "input_pages": [{**origins[0], "raw": "../../raw/0001_scan.png"}],
        "scan": {"faces": {"front": {"source": 1}}},
        "pages": {"front": "images/pages/front.jpg", "raw_1": "../../raw/0001_scan.png"},
        "student": {
            "exam_no": {"value": "0", "image": "images/identity/exam_no.jpg"},
            "fields": {"name": {"image": "images/fields/key-name.jpg", "ocr_status": "none"}},
            "barcodes": {key: {"image": f"images/barcodes/key-{key}.jpg"} for key in ("id", "room")},
            "omr_fields": {"room": {"image": "images/omr_fields/key-room.jpg"}},
        },
        "objective": [{"no": "1", "type": "single", "question_id": "objective/1",
                       "answer": "A", "status": "ok",
                       "image": "images/objective/q-1.jpg", "images": ["images/objective/q-1.jpg"],
                       "regions": [{"face": "front", "rect": [-0.5, 18.5, 20, 5],
                                    "order": 1, "image": "images/objective/q-1.jpg"}]}],
        "subjective": [{"no": "2", "sub": "a", "type": "short", "content": "en",
                        "question_id": "subjective/2/a", "ocr_status": "none",
                        "images": ["images/subjective/q-2/sub-a/region-1.jpg", "images/subjective/q-2/sub-a/region-2.jpg"],
                        "regions": [{"face": "front", "rect": rect, "order": i,
                                     "image": f"images/subjective/q-2/sub-a/region-{i}.jpg"}
                                    for i, rect in enumerate(([2, 30, 20, 5], [2, 40, 20, 5]), 1)]}],
        "warnings": [],
    }
    dump(root / "manifest.json", manifest)
    dump(root / "template/template.json", template)
    dump(root / "template.json", template)
    dump(root / "students/0001/result.json", result)
    picture(root / "template/front.png")
    picture(root / "raw/0001_scan.png")
    for name in ("pages/front.jpg", "identity/exam_no.jpg", "fields/key-name.jpg",
                 "barcodes/key-id.jpg", "barcodes/key-room.jpg", "omr_fields/key-room.jpg",
                 "objective/q-1.jpg", "subjective/q-2/sub-a/region-1.jpg", "subjective/q-2/sub-a/region-2.jpg"):
        picture(root / f"students/0001/images/{name}")
    return root, manifest, template, result


def test_valid_inventory_is_complete_and_audit_is_readonly(batch):
    root, _, _, _ = batch
    dump(root / "state/0001.json", {"private": True})
    dump(root / "acquisition/unused.json", {})
    dump(root / "attempts/old.json", {})
    before = {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
              for p in root.rglob("*") if p.is_file()}
    report = audit(root)
    assert report.ready, report.issues
    assert report.status == "ready"
    assert report.issues == []
    expected = {key: hashlib.sha256(data).hexdigest() for key, (data, _) in before.items()
                if key.split("/")[0] not in {"state", "acquisition", "attempts"}}
    assert report.files == expected
    assert report.to_dict() == asdict(report)
    assert set(report.to_dict()) == {"ready", "status", "issues", "files", "summary", "checked_at"}
    assert before == {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
                      for p in root.rglob("*") if p.is_file()}


def test_stale_settlement_revision_cannot_clear_delivery_gate(batch):
    root, manifest, _, _ = batch
    manifest["revision"] = 2
    manifest["finalization"] = {"status": "complete", "revision": 1, "blocked_seqs": [], "failed_seqs": []}
    dump(root / "manifest.json", manifest)
    assert not audit(root).ready


@pytest.mark.parametrize("reference", [
    "/tmp/escape.png", "../../../escape.png", r"C:\outside\file.png", "C:relative.png",
    r"\\server\share\file.png", r"\rooted.png", "bad\x00path",
])
def test_confined_paths_reject_platform_absolute_and_escape(batch, reference):
    root, _, _, _ = batch
    resolve = importlib.import_module("scan_answer.output.paths").resolve_path
    with pytest.raises(ValueError):
        resolve(root, root / "students/0001", reference)
    assert resolve(root, root / "students/0001", "../../raw/0001_scan.png") == root / "raw/0001_scan.png"


def test_symlink_escape_is_blocked(batch, tmp_path):
    root, _, _, _ = batch
    path = root / "students/0001/images/objective/q-1.jpg"
    path.unlink()
    outside = tmp_path / "outside.png"
    picture(outside)
    path.symlink_to(outside)
    assert not audit(root).ready


def test_parent_component_after_symlink_cannot_hide_escape(batch, tmp_path):
    root, _, _, _ = batch
    outside = tmp_path / "elsewhere/child"
    outside.mkdir(parents=True)
    (root / "students/0001/link").symlink_to(outside, target_is_directory=True)
    resolve = importlib.import_module("scan_answer.output.paths").resolve_path
    with pytest.raises(ValueError):
        resolve(root, root / "students/0001", "link/../images/objective/q-1.jpg")


def test_in_batch_symlink_is_inventoried_at_the_referenced_name(batch):
    root, _, _, _ = batch
    image = root / "students/0001/images/objective/q-1.jpg"
    image.rename(image.with_name("original.png"))
    image.symlink_to("original.png")
    report = audit(root)
    assert report.ready, report.issues
    assert "students/0001/images/objective/q-1.jpg" in report.files
    assert "students/0001/images/objective/original.png" not in report.files


@pytest.mark.parametrize("relative", [
    "manifest.json", "template/template.json", "template/front.png",
    "students/0001/result.json", "students/0001/images/objective/q-1.jpg",
    "students/0001/images/subjective/q-2/sub-a/region-2.jpg", "students/0001/images/barcodes/key-room.jpg",
    "students/0001/images/fields/key-name.jpg", "students/0001/images/omr_fields/key-room.jpg",
    "students/0001/images/identity/exam_no.jpg", "students/0001/images/pages/front.jpg", "raw/0001_scan.png",
])
def test_every_required_payload_is_checked(batch, relative):
    root, _, _, _ = batch
    (root / relative).unlink()
    report = audit(root)
    assert not report.ready
    assert report.status == "blocked"
    assert all(set(issue) == {"severity", "code", "message", "seq", "path"} for issue in report.issues)


@pytest.mark.parametrize("relative", ["students/0001/images/objective/q-1.jpg", "template/front.png", "raw/0001_scan.png"])
def test_image_must_decode_and_not_just_exist(batch, relative):
    root, _, _, _ = batch
    (root / relative).write_bytes(b"not an image")
    assert not audit(root).ready


@pytest.mark.parametrize("mutation", [
    lambda r: r.update(seq=2),
    lambda r: r.update(batch_id="other"),
    lambda r: r.update(sheet_id="batch:0002"),
    lambda r: r["template"].update(version=2),
    lambda r: r["template"].update(version=True),
    lambda r: r.update(schema_version="2.0"),
    lambda r: r["objective"].clear(),
    lambda r: r["objective"].append(deepcopy(r["objective"][0])),
    lambda r: r["objective"][0].update(question_id="objective/3"),
    lambda r: r["objective"][0]["regions"][0].update(rect=[0, 0, 1, 1]),
    lambda r: r["subjective"][0].update(sub="b"),
    lambda r: r["subjective"][0]["regions"].reverse(),
    lambda r: r["subjective"][0]["images"].pop(),
    lambda r: r["subjective"][0]["regions"][0].update(face="back"),
    lambda r: r["subjective"][0]["regions"][0].update(image="images/subjective/q-2/sub-a/region-2.jpg"),
    lambda r: r["student"]["barcodes"].pop("room"),
    lambda r: r["student"]["fields"].pop("name"),
    lambda r: r["student"]["omr_fields"].pop("room"),
    lambda r: r["student"].update(exam_no=None),
    lambda r: r["scan"]["faces"]["front"].update(source=2),
    lambda r: r["input_pages"][0].update(page=2),
    lambda r: r["pages"].update(raw_1="images/objective/q-1.jpg"),
])
def test_result_identity_coverage_and_mapping_must_match_template_and_manifest(batch, mutation):
    root, _, _, result = batch
    mutation(result)
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


@pytest.mark.parametrize("mutation", [
    lambda m: m.update(finalized=False),
    lambda m: m["sheets"][0].update(status="processing"),
    lambda m: m["sheets"][0].update(status="physical", physical="ALIGN_FAILED"),
    lambda m: m["sheets"].append(deepcopy(m["sheets"][0])),
    lambda m: m.update(rejudge_failures=[{"seq": 1, "message": "cannot rejudge"}]),
    lambda m: m["sheets"].clear(),
    lambda m: m["template"].update(name="wrong"),
    lambda m: m["template"].update(version=True),
    lambda m: m.update(schema_version="2.0"),
    lambda m: m.update(finalization={"status": "running"}),
    lambda m: m.update(finalization={"status": "complete", "failed_seqs": [1]}),
    lambda m: m["sheets"][0].update(finalize_error="rejudge failed"),
])
def test_unsettled_invalid_or_duplicate_manifest_blocks(batch, mutation):
    root, manifest, _, _ = batch
    mutation(manifest)
    dump(root / "manifest.json", manifest)
    assert not audit(root).ready


@pytest.mark.parametrize("where", ["subjective", "field"])
@pytest.mark.parametrize("status,ready", [("none", True), ("done", True), ("pending", False), ("failed", True), ("mystery", False)])
def test_ocr_policy(batch, where, status, ready):
    root, _, _, result = batch
    entry = result["subjective"][0] if where == "subjective" else result["student"]["fields"]["name"]
    entry["ocr_status"] = status
    dump(root / "students/0001/result.json", result)
    report = audit(root)
    assert report.ready is ready, report.issues
    if status == "failed":
        assert report.status == "warnings"
        assert any(i["code"] == "ocr_failed" and i["seq"] == 1 for i in report.issues)


def test_complete_finalization_is_accepted(batch):
    root, manifest, _, _ = batch
    manifest["finalization"] = {"status": "complete", "blocked_seqs": [], "failed_seqs": []}
    dump(root / "manifest.json", manifest)
    assert audit(root).ready


@pytest.mark.parametrize("target,bad", [
    ("manifest.json", []), ("manifest.json", {"sheets": None}),
    ("template/template.json", []), ("students/0001/result.json", []),
])
def test_malformed_json_shapes_report_issues(batch, target, bad):
    root, _, _, _ = batch
    dump(root / target, bad)
    assert not audit(root).ready


@pytest.mark.parametrize("field,value", [
    ("student", []), ("subjective", [None]), ("objective", "bad"), ("pages", None),
    ("scan", {"faces": []}), ("input_pages", [None]), ("template", None),
    ("warnings", {}), ("subjective", [{"no": [], "regions": None, "images": 5}]),
])
def test_malformed_nested_result_shapes_report_issues(batch, field, value):
    root, _, _, result = batch
    result[field] = value
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


def pdf_origin(batch):
    root, manifest, _, result = batch
    pdf = root / "source.pdf"
    with pypdfium2.PdfDocument.new() as document:
        page = document.new_page(80, 60)
        page.close()
        document.save(pdf)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    target = root / f"sources/{digest}.pdf"
    target.parent.mkdir()
    pdf.rename(target)
    manifest["sheets"][0]["input_pages"][0].update(
        kind="pdf", file="answers.pdf", page=1, document_id=digest, document=f"sources/{digest}.pdf")
    result["input_pages"][0].update(
        kind="pdf", file="answers.pdf", page=1, document_id=digest, document=f"../../sources/{digest}.pdf")
    dump(root / "manifest.json", manifest)
    dump(root / "students/0001/result.json", result)
    return target


def test_pdf_digest_actual_bounds_and_manifest_mapping(batch):
    root, manifest, _, result = batch
    target = pdf_origin(batch)
    assert audit(root).ready
    assert target.relative_to(root).as_posix() in audit(root).files
    for origin in (manifest["sheets"][0]["input_pages"][0], result["input_pages"][0]):
        origin["page"] = 2
    dump(root / "manifest.json", manifest)
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


@pytest.mark.parametrize("field,value", [
    ("answer", None), ("answer", {}), ("answer", []), ("answer", 1),
    ("status", None), ("status", {}), ("status", []), ("status", True),
    ("status", "invalid"),
])
def test_invalid_objective_fields_block_delivery(batch, tmp_path, field, value):
    from scan_answer.output.delivery import DeliveryError, export_delivery

    root, _, _, result = batch
    result["objective"][0][field] = value
    dump(root / "students/0001/result.json", result)
    report = audit(root)
    assert not report.ready
    assert any(issue["code"] == "question_invalid" for issue in report.issues)
    with pytest.raises(DeliveryError):
        export_delivery(root, tmp_path / "delivery")


@pytest.mark.parametrize("field", ["answer", "status"])
def test_missing_objective_fields_block_delivery(batch, tmp_path, field):
    from scan_answer.output.delivery import DeliveryError, export_delivery

    root, _, _, result = batch
    del result["objective"][0][field]
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready
    with pytest.raises(DeliveryError):
        export_delivery(root, tmp_path / "delivery")


@pytest.mark.parametrize("status", ["ok", "blank", "multiple", "uncertain", "partial", "failed"])
def test_contract_objective_statuses_are_accepted(batch, status):
    root, _, _, result = batch
    result["objective"][0].update(answer="", status=status)
    dump(root / "students/0001/result.json", result)
    assert audit(root).ready


def test_pdf_metadata_is_reused_across_sheets_and_export_audits(batch, tmp_path, monkeypatch):
    from scan_answer.output import audit as module
    from scan_answer.output.delivery import export_delivery

    root, manifest, _, result = batch
    target = pdf_origin(batch)
    # Several answer sheets may come from the same archived document.
    second = deepcopy(manifest["sheets"][0])
    second.update(seq=2, result="students/0002/result.json")
    second["input_pages"][0]["raw"] = "raw/0002_scan.png"
    manifest["sheets"].append(second)
    shutil.copytree(root / "students/0001", root / "students/0002")
    picture(root / "raw/0002_scan.png")
    copied = deepcopy(result)
    copied.update(seq=2, sheet_id="batch:0002")
    copied["input_pages"][0]["raw"] = "../../raw/0002_scan.png"
    copied["pages"]["raw_1"] = "../../raw/0002_scan.png"
    dump(root / "students/0002/result.json", copied)
    dump(root / "manifest.json", manifest)
    calls, reads = [], []
    count = module.pdf_page_count
    read_bytes = type(target).read_bytes

    def counted(data):
        calls.append(1)
        return count(data)

    def tracked(path):
        if path.suffix == ".pdf":
            reads.append(path)
        return read_bytes(path)

    monkeypatch.setattr(module, "pdf_page_count", counted)
    monkeypatch.setattr(type(target), "read_bytes", tracked)
    assert audit(root).ready
    assert len(calls) == len(reads) == 1
    calls.clear()
    reads.clear()
    export_delivery(root, tmp_path / "delivery")
    assert len(calls) == len(reads) == 3


def test_cached_pdf_still_detects_changes_during_audit(batch, monkeypatch):
    from scan_answer.output import audit as module

    root, _, _, _ = batch
    target = pdf_origin(batch)
    count = module.pdf_page_count

    def change_after_read(data):
        pages = count(data)
        before = target.stat()
        target.write_bytes(data.replace(b"/MediaBox", b"/Mediabox", 1))
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        return pages

    monkeypatch.setattr(module, "pdf_page_count", change_after_read)
    report = audit(root)
    assert not report.ready
    assert any(issue["code"] == "payload_changed" for issue in report.issues)


def test_pdf_hash_checked_again_despite_same_size_and_timestamp(batch):
    root, _, _, _ = batch
    target = pdf_origin(batch)
    assert audit(root).ready
    before = target.stat()
    contents = target.read_bytes()
    target.write_bytes(contents.replace(b"/MediaBox", b"/Mediabox", 1))
    assert target.stat().st_size == before.st_size
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not audit(root).ready


def test_another_students_picture_cannot_satisfy_required_evidence(batch):
    root, _, _, result = batch
    picture(root / "students/0002/images/objective/q-1.jpg")
    result["objective"][0].update(image="../0002/images/objective/q-1.jpg", images=["../0002/images/objective/q-1.jpg"])
    result["objective"][0]["regions"][0]["image"] = "../0002/images/objective/q-1.jpg"
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


def test_voided_evidence_is_in_inventory_but_explicitly_excluded(batch):
    root, manifest, _, _ = batch
    pdf = pdf_origin(batch)
    origin = {**manifest["sheets"][0]["input_pages"][0], "raw": "raw/0002_bad.png"}
    manifest["sheets"].append({"seq": 2, "status": "voided", "voided": True,
                                "physical": "ALIGN_FAILED", "message": "bad card",
                                "files": ["bad.png"], "input_pages": [origin]})
    dump(root / "manifest.json", manifest)
    picture(root / "raw/0002_bad.png")
    dump(root / "errors/0002_ALIGN_FAILED/error.json", {"seq": 2, "code": "ALIGN_FAILED", "msg": "bad card"})
    picture(root / "errors/0002_ALIGN_FAILED/face_1.jpg")
    report = audit(root)
    assert report.ready, report.issues
    assert report.status == "warnings"
    assert report.summary["excluded"][0]["seq"] == 2
    assert {"raw/0002_bad.png", "errors/0002_ALIGN_FAILED/error.json",
            "errors/0002_ALIGN_FAILED/face_1.jpg", pdf.relative_to(root).as_posix()} <= set(report.files)
    (root / "raw/0002_bad.png").unlink()
    report = audit(root)
    assert report.ready
    assert any(i["code"] == "void_evidence_missing" for i in report.issues)


def test_sealed_inventory_and_revision_detect_tampering_without_self_hash(batch):
    root, manifest, _, _ = batch
    manifest["delivery"] = {"sealed": True, "revision": "delivery-v1"}
    dump(root / "manifest.json", manifest)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    dump(root / "delivery.json", {"revision": "delivery-v1", "files": files})
    report = audit(root)
    assert report.ready, report.issues
    assert "delivery.json" not in report.files
    dump(root / "delivery.json", {"revision": "wrong", "files": files})
    assert not audit(root).ready
    dump(root / "delivery.json", {"revision": "delivery-v1", "files": files})
    picture(root / "students/0001/images/objective/q-1.jpg", color=30)
    assert not audit(root).ready


def test_sealed_inventory_cannot_omit_or_add_a_payload(batch):
    root, manifest, _, _ = batch
    manifest["delivery"] = {"sealed": True, "revision": "v1"}
    dump(root / "manifest.json", manifest)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    files.pop("students/0001/images/subjective/q-2/sub-a/region-2.jpg")
    dump(root / "delivery.json", {"revision": "v1", "files": files})
    assert not audit(root).ready


def test_sealed_metadata_symlink_escape_reports_instead_of_raising(batch, tmp_path):
    root, manifest, _, _ = batch
    manifest["delivery"] = {"sealed": True, "revision": "v1"}
    dump(root / "manifest.json", manifest)
    outside = tmp_path / "outside.json"
    dump(outside, {"revision": "v1", "files": {}})
    (root / "delivery.json").symlink_to(outside)
    report = audit(root)
    assert not report.ready
    assert any(i["code"] == "invalid_path" for i in report.issues)


def test_two_result_files_in_one_student_directory_are_not_separate_students(batch):
    root, manifest, _, result = batch
    other = deepcopy(result)
    other.update(seq=2, sheet_id="batch:0002")
    other["input_pages"][0]["raw"] = "../../raw/0002_scan.png"
    other["pages"]["raw_1"] = "../../raw/0002_scan.png"
    dump(root / "students/0001/another.json", other)
    picture(root / "raw/0002_scan.png")
    second = deepcopy(manifest["sheets"][0])
    second.update(seq=2, result="students/0001/another.json")
    second["input_pages"][0]["raw"] = "raw/0002_scan.png"
    manifest["sheets"].append(second)
    dump(root / "manifest.json", manifest)
    assert not audit(root).ready


def test_runtime_reference_is_rejected_even_if_it_is_a_valid_picture(batch):
    root, _, _, result = batch
    picture(root / "state/picture.png")
    result["student"]["fields"]["name"]["image"] = "../../state/picture.png"
    dump(root / "students/0001/result.json", result)
    report = audit(root)
    assert not report.ready
    assert "state/picture.png" not in report.files


def test_student_directory_sequence_cannot_disagree_with_result(batch):
    root, manifest, _, _ = batch
    (root / "students/0001").rename(root / "students/0002")
    manifest["sheets"][0]["result"] = "students/0002/result.json"
    dump(root / "manifest.json", manifest)
    assert not audit(root).ready


def test_legacy_void_keeps_available_raw_without_input_page_metadata(batch):
    root, manifest, _, _ = batch
    manifest["sheets"].append({"seq": 2, "status": "voided", "voided": True, "physical": "BLANK_PAGE"})
    dump(root / "manifest.json", manifest)
    picture(root / "raw/0002_retained.png")
    report = audit(root)
    assert report.ready, report.issues
    assert "raw/0002_retained.png" in report.files
    assert any(i["code"] == "void_evidence_missing" for i in report.issues)


def test_duplicate_pdf_page_cannot_supply_both_faces(batch):
    root, manifest, template, result = batch
    pdf_origin(batch)
    template["faces"]["back"] = deepcopy(template["faces"]["front"])
    template["faces"]["back"]["image"] = "back.png"
    picture(root / "template/back.png")
    dump(root / "template/template.json", template)
    dump(root / "template.json", template)
    second = deepcopy(manifest["sheets"][0]["input_pages"][0])
    second["raw"] = "raw/0001_back.png"
    manifest["sheets"][0]["input_pages"].append(second)
    manifest["sheets"][0]["files"].append("back.png")
    result["input_pages"].append({**second, "raw": "../../raw/0001_back.png",
                                  "document": "../../" + second["document"]})
    result["pages"].update(back="images/back.png", raw_2="../../raw/0001_back.png")
    result["scan"]["faces"]["back"] = {"source": 2}
    picture(root / "raw/0001_back.png")
    picture(root / "students/0001/images/back.png")
    dump(root / "manifest.json", manifest)
    dump(root / "students/0001/result.json", result)
    assert not audit(root).ready


@pytest.mark.parametrize("field,value", [
    ("faces", []), ("objective", [None]), ("subjective", [None]), ("exam_no", []),
    ("paper", None), ("fields", [{"key": []}]), ("barcodes", {}),
])
def test_malformed_template_shapes_block_without_crashing(batch, field, value):
    root, _, template, _ = batch
    template[field] = value
    dump(root / "template/template.json", template)
    assert not audit(root).ready


@pytest.mark.parametrize("field,value", [
    ("sheets", [None]), ("sheets", [{"seq": []}]), ("template", []),
    ("delivery", []), ("batch_id", None), ("finalized", "yes"),
])
def test_malformed_manifest_shapes_block_without_crashing(batch, field, value):
    root, manifest, _, _ = batch
    manifest[field] = value
    dump(root / "manifest.json", manifest)
    assert not audit(root).ready


def test_pdf_malformed_bytes_block_even_when_digest_matches(batch):
    root, manifest, _, result = batch
    target = pdf_origin(batch)
    target.write_bytes(b"definitely not PDF")
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest["sheets"][0]["input_pages"][0]["document_id"] = digest
    result["input_pages"][0]["document_id"] = digest
    dump(root / "manifest.json", manifest)
    dump(root / "students/0001/result.json", result)
    report = audit(root)
    assert not report.ready
    assert any(i["code"] == "pdf_invalid" for i in report.issues)


def test_seal_detects_same_stat_json_rewrite(batch):
    root, manifest, _, result = batch
    manifest["delivery"] = {"sealed": True, "revision": "v1"}
    dump(root / "manifest.json", manifest)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in root.rglob("*") if p.is_file()}
    dump(root / "delivery.json", {"revision": "v1", "files": files})
    assert audit(root).ready
    path = root / "students/0001/result.json"
    before = path.stat()
    result["student"]["exam_no"]["value"] = "1"
    dump(path, result)
    assert path.stat().st_size == before.st_size
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert not audit(root).ready


def test_real_generated_batch_is_auditable_and_portable(small_dataset, tmp_path):
    from scan_answer.batch import run_batch
    from conftest import TEMPLATE_DIR

    inputs = tmp_path / "input"
    inputs.mkdir()
    for path in sorted(small_dataset.glob("*.jpg"))[:2]:
        shutil.copy(path, inputs)
    root = run_batch(TEMPLATE_DIR, inputs, tmp_path / "output", batch_id="actual")
    original = audit(root)
    assert original.ready, original.issues
    relocated = tmp_path / "relocated"
    shutil.copytree(root, relocated)
    moved = audit(relocated)
    assert moved.ready, moved.issues
    assert moved.files == original.files
