"""Spatial barcode evidence and conservative field association."""
import importlib
import json
from types import SimpleNamespace

import numpy as np
import pytest
import zxingcpp

from scan_answer.engine import barcode
from scan_answer.template.model import BarcodeSpec


def _reader():
    name = "scan_answer.engine.barcode_regions"
    assert importlib.util.find_spec(name) is not None, "spatial association is missing"
    return importlib.import_module(name).read_barcode_regions


def _code(text="100015000123456"):
    code = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.Code128)
    return np.asarray(zxingcpp.write_barcode_to_image(code, scale=3, add_quiet_zones=True))


def _page(*placements):
    image = np.full((1600, 2000), 255, np.uint8)
    for x, y, text in placements:
        code = _code(text)
        image[y:y + code.shape[0], x:x + code.shape[1]] = code
    return image


def _read(specs, image, **kwargs):
    return _reader()(
        specs, {"front": image},
        {"front": kwargs.get("H", np.diag([0.1, 0.1, 1.0]))},
        {"front": kwargs.get("dpi", 254.0)},
    )


def _decoded(text, x, y, width=100, height=40):
    points = [SimpleNamespace(x=a, y=b) for a, b in (
        (x, y), (x + width, y), (x + width, y + height), (x, y + height),
    )]
    return SimpleNamespace(
        valid=True, text=text, format=SimpleNamespace(name="Code128"),
        position=SimpleNamespace(
            top_left=points[0], top_right=points[1],
            bottom_right=points[2], bottom_left=points[3],
        ),
    )


def test_find_barcodes_keeps_real_duplicate_text_at_different_positions():
    image = _page((100, 100, "100015000123456"), (100, 800, "100015000123456"))
    hits = barcode.find_barcodes(image)
    assert len(hits) == 2
    assert {hit[0] for hit in hits} == {"100015000123456"}
    assert sorted(hit[2][1] for hit in hits) == pytest.approx([174.5, 874.5], abs=2)


def test_original_and_otsu_union_deduplicates_only_the_same_physical_hit(monkeypatch):
    variants = iter([
        [_decoded("same", 10, 10)],
        [_decoded("same", 11, 10), _decoded("same", 10, 100), _decoded("other", 150, 10)],
    ])
    monkeypatch.setattr(zxingcpp, "read_barcodes", lambda image: next(variants))
    hits = barcode.find_barcodes(np.full((200, 300), 128, np.uint8))
    assert len(hits) == 3
    assert sorted(hit[0] for hit in hits) == ["other", "same", "same"]


def test_real_region_candidate_has_template_polygon_and_legacy_hit():
    image = _page((100, 100, "100015000123456"))
    hits, evidence = _read([BarcodeSpec("student", "front", (8, 8, 52, 20))], image)
    assert hits == {"student": ("100015000123456", "Code128", "region")}
    item = evidence["student"]
    assert item["status"] == "ok"
    assert item["reason"] is None
    assert len(item["candidates"]) == 1
    candidate = item["candidates"][0]
    assert item["selected_id"] == candidate["id"]
    assert candidate["face"] == "front"
    assert candidate["value"] == "100015000123456"
    assert candidate["symbology"] == "Code128"
    assert candidate["located"] == "region"
    np.testing.assert_allclose(candidate["polygon"], [[13, 10], [53.1, 10], [53.1, 24.9], [13, 24.9]], atol=0.2)
    assert candidate["rect"] == pytest.approx([13, 10, 40.1, 14.9], abs=0.2)
    json.dumps(evidence, allow_nan=False)


def test_single_region_does_not_decode_the_full_page(monkeypatch):
    image = _page((100, 100, "100015000123456"))
    original = zxingcpp.read_barcodes

    def decode(crop):
        assert crop.shape != image.shape, "unnecessary full page decode"
        return original(crop)

    monkeypatch.setattr(zxingcpp, "read_barcodes", decode)
    hits, evidence = _read([BarcodeSpec("student", "front", (8, 8, 52, 20))], image)
    assert hits["student"] is not None
    assert evidence["student"]["status"] == "ok"


def test_identical_text_at_two_positions_can_fill_two_fields():
    image = _page((100, 100, "100015000123456"), (100, 800, "100015000123456"))
    specs = [
        BarcodeSpec("a", "front", (8, 8, 52, 20)),
        BarcodeSpec("b", "front", (8, 78, 52, 20)),
    ]
    hits, evidence = _read(specs, image)
    assert hits["a"] == hits["b"] == ("100015000123456", "Code128", "region")
    assert evidence["a"]["selected_id"] != evidence["b"]["selected_id"]
    assert _read(list(reversed(specs)), image) == (hits, evidence)


def test_multiple_direct_candidates_are_ambiguous_even_for_identical_text():
    image = _page((100, 100, "100015000123456"), (700, 100, "100015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 8, 110, 20))], image)
    assert hits == {"a": None}
    assert evidence["a"]["status"] == "ambiguous"
    assert evidence["a"]["selected_id"] is None
    assert evidence["a"]["reason"]
    assert len(evidence["a"]["candidates"]) == 2


@pytest.mark.parametrize("reverse", [False, True])
def test_overlapping_fields_contend_without_order_dependent_assignment(reverse):
    image = _page((100, 100, "100015000123456"))
    specs = [
        BarcodeSpec("a", "front", (8, 8, 52, 20)),
        BarcodeSpec("b", "front", (7, 7, 54, 22)),
    ]
    hits, evidence = _read(specs[::-1] if reverse else specs, image)
    assert hits == {"a": None, "b": None}
    assert {item["status"] for item in evidence.values()} == {"ambiguous"}
    assert all(item["selected_id"] is None and item["reason"] for item in evidence.values())
    assert evidence["a"]["candidates"][0]["id"] == evidence["b"]["candidates"][0]["id"]


def test_expanded_hit_keeps_actual_location():
    image = _page((100, 300, "100015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (18, 28, 42, 20))], image)
    assert hits["a"] == ("100015000123456", "Code128", "expanded")
    candidate = evidence["a"]["candidates"][0]
    assert candidate["located"] == "expanded"
    assert candidate["rect"] == pytest.approx([13, 30, 40.1, 14.9], abs=0.2)


@pytest.mark.parametrize("offset, status", [(0, "ambiguous"), (10, "ambiguous"), (40, "ok")])
def test_nearest_page_fallback_rejects_ties_within_two_mm(offset, status):
    image = _page((369, 225, "100015000123456"), (369, 825 + offset, "200015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (55, 55, 10, 10))], image)
    assert evidence["a"]["status"] == status
    assert len(evidence["a"]["candidates"]) == 2
    if status == "ambiguous":
        assert hits["a"] is None
        assert evidence["a"]["selected_id"] is None
    else:
        assert hits["a"] == ("100015000123456", "Code128", "page")
        selected = next(c for c in evidence["a"]["candidates"] if c["id"] == evidence["a"]["selected_id"])
        assert selected["rect"][1] == pytest.approx(22.5, abs=0.4)
        assert selected["distance_mm"] == pytest.approx(30.05, abs=0.2)


def test_distant_page_hit_is_retained_but_not_assigned():
    image = _page((369, 1100, "100015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (55, 55, 10, 10))], image)
    assert hits == {"a": None}
    assert evidence["a"]["status"] == "failed"
    assert evidence["a"]["selected_id"] is None
    assert evidence["a"]["reason"]
    assert len(evidence["a"]["candidates"]) == 1
    assert evidence["a"]["candidates"][0]["distance_mm"] > 40


def test_page_fallback_cannot_contend_with_an_established_region_hit():
    image = _page((369, 225, "100015000123456"))
    specs = [
        BarcodeSpec("direct", "front", (35, 20, 52, 20)),
        BarcodeSpec("fallback", "front", (55, 55, 10, 10)),
    ]
    hits, evidence = _read(specs, image)
    assert hits == {"direct": ("100015000123456", "Code128", "region"), "fallback": None}
    assert evidence["direct"]["status"] == "ok"
    assert evidence["fallback"]["status"] == "failed"
    assert evidence["fallback"]["reason"] == "candidate_reserved"
    assert len(evidence["direct"]["candidates"]) == len(evidence["fallback"]["candidates"]) == 1
    assert evidence["direct"]["candidates"][0]["id"] == evidence["fallback"]["candidates"][0]["id"]
    assert _read(specs[::-1], image) == (hits, evidence)


def test_fallback_uses_remaining_candidate_after_stronger_region_assignment():
    image = _page((369, 225, "100015000123456"), (369, 925, "200015000123456"))
    specs = [
        BarcodeSpec("direct", "front", (35, 20, 52, 20)),
        BarcodeSpec("fallback", "front", (55, 55, 10, 10)),
    ]
    hits, evidence = _read(specs, image)
    assert hits["direct"] == ("100015000123456", "Code128", "region")
    assert hits["fallback"] == ("200015000123456", "Code128", "page")
    assert len(evidence["fallback"]["candidates"]) == 2
    assert _read(specs[::-1], image) == (hits, evidence)


def test_real_8k_missing_paper_code_does_not_invalidate_student_code(ctx, small_dataset):
    from scan_answer.engine.recognize import analyze_sheet, decide_sheet
    from scan_answer.engine.omr import Thresholds
    from scan_answer.imageio import imread_gray
    images = [imread_gray(small_dataset / f"0001_{face}.jpg") for face in (1, 2)]
    analysis = analyze_sheet(ctx, images, [200, 200])
    decision = decide_sheet(ctx, analysis, Thresholds())
    student = decision.barcodes["student_barcode"]
    assert student["status"] == "ok"
    assert student["value"] == "065092213138494"
    assert decision.barcodes["paper_barcode"]["status"] == "failed"
    assert decision.consistent is True


def test_faces_do_not_contend_and_page_geometry_uses_homography():
    image = _page((369, 225, "100015000123456"))
    specs = [BarcodeSpec(face, face, (60, 62, 10, 10)) for face in ("front", "back")]
    H = np.array([[0.1, 0, 5], [0, 0.1, 7], [0, 0, 1]])
    hits, evidence = _reader()(specs, dict.fromkeys(("front", "back"), image),
                               dict.fromkeys(("front", "back"), H),
                               dict.fromkeys(("front", "back"), 254.0))
    assert hits["front"] == hits["back"] == ("100015000123456", "Code128", "page")
    assert evidence["front"]["selected_id"] != evidence["back"]["selected_id"]
    for face in ("front", "back"):
        candidate = evidence[face]["candidates"][0]
        assert candidate["face"] == face
        assert candidate["rect"] == pytest.approx([44.9, 29.5, 40.1, 14.9], abs=0.4)


def test_blank_and_missing_face_return_failed_evidence():
    specs = [BarcodeSpec("a", "front", (8, 8, 52, 20)), BarcodeSpec("b", "back", (8, 8, 52, 20))]
    hits, evidence = _read(specs, _page())
    assert hits == {"a": None, "b": None}
    for item in evidence.values():
        assert item["status"] == "failed"
        assert item["candidates"] == []
        assert item["selected_id"] is None
        assert item["reason"]


def test_page_tie_straddling_distance_limit_is_still_ambiguous():
    image = _page((369, 135, "100015000123456"), (369, 930, "200015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (55, 55, 10, 10))], image)
    assert hits["a"] is None
    assert evidence["a"]["status"] == "ambiguous"
    assert len(evidence["a"]["candidates"]) == 2


def test_unselected_distant_page_candidate_is_preserved_for_review():
    image = _page((369, 225, "100015000123456"), (369, 1100, "200015000123456"))
    hits, evidence = _read([BarcodeSpec("a", "front", (55, 55, 10, 10))], image)
    assert hits["a"] == ("100015000123456", "Code128", "page")
    assert len(evidence["a"]["candidates"]) == 2
    assert {c["value"] for c in evidence["a"]["candidates"]} == {"100015000123456", "200015000123456"}


def test_disjoint_partial_crops_of_one_barcode_contend_as_one_physical_hit():
    image = _page((100, 100, "100015000123456"))
    specs = [
        BarcodeSpec("top", "front", (8, 10, 52, 6)),
        BarcodeSpec("bottom", "front", (8, 19, 52, 6)),
    ]
    hits, evidence = _read(specs, image)
    assert hits == {"top": None, "bottom": None}
    assert all(item["status"] == "ambiguous" for item in evidence.values())
    assert len(evidence["top"]["candidates"]) == len(evidence["bottom"]["candidates"]) == 1
    assert evidence["top"]["candidates"][0]["id"] == evidence["bottom"]["candidates"][0]["id"]
    assert evidence["top"]["candidates"][0]["rect"] == pytest.approx([13, 10, 40.1, 14.9], abs=0.4)


def test_conflicting_payloads_have_unique_candidate_ids_at_one_physical_position(monkeypatch):
    variants = iter([[_decoded("a", 30, 30)], [_decoded("b", 30, 30)]])
    monkeypatch.setattr(zxingcpp, "read_barcodes", lambda image: next(variants))
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 8, 52, 20))], _page())
    assert hits["a"] is None
    item = evidence["a"]
    assert item["status"] == "ambiguous"
    assert item["selected_id"] is None
    assert {c["value"] for c in item["candidates"]} == {"a", "b"}
    assert len({c["id"] for c in item["candidates"]}) == 2


def test_conflicting_payloads_still_contend_by_physical_position(monkeypatch):
    variants = iter([[_decoded(value, 30, 30)] for value in ("a", "a", "b", "b")])
    monkeypatch.setattr(zxingcpp, "read_barcodes", lambda image: next(variants))
    specs = [BarcodeSpec(key, "front", (8, 8, 52, 20)) for key in ("a", "b")]
    hits, evidence = _read(specs, _page())
    assert hits == {"a": None, "b": None}
    for item in evidence.values():
        assert item["status"] == "ambiguous"
        assert item["reason"] == "candidate_contended"
        assert len({c["id"] for c in item["candidates"]}) == 2


@pytest.mark.parametrize("width,height", [(100, 0), (0, 40)])
def test_scanline_only_decode_has_positive_finite_crop_geometry(monkeypatch, width, height):
    monkeypatch.setattr(zxingcpp, "read_barcodes",
                        lambda image: [_decoded("line", 30, 30, width, height)])
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 8, 52, 20))], _page())
    assert hits["a"] == ("line", "Code128", "region")
    candidate = evidence["a"]["candidates"][0]
    polygon = np.asarray(candidate["polygon"])
    x, y, w, h = candidate["rect"]
    assert np.isfinite(polygon).all()
    assert w > 0 and h > 0
    np.testing.assert_allclose(polygon.min(axis=0), [x, y])
    np.testing.assert_allclose(polygon.max(axis=0), [x + w, y + h])
    assert sum(c["id"] == evidence["a"]["selected_id"] for c in evidence["a"]["candidates"]) == 1
    json.dumps(evidence, allow_nan=False)


def test_singular_homography_cannot_produce_successful_geometry(monkeypatch):
    monkeypatch.setattr(zxingcpp, "read_barcodes", lambda image: [_decoded("a", 30, 30)])
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 8, 52, 20))],
                           _page(), H=np.diag([0.1, 0, 1]))
    assert hits["a"] is None
    assert evidence["a"]["status"] == "failed"
    assert evidence["a"]["candidates"] == []


def test_failed_expansion_does_not_hide_truncated_region_location(monkeypatch):
    def decode(image):
        if image.shape == (60, 520):
            return [_decoded("a", 50, 0, 401, 59)]
        if image.shape == (1600, 2000):
            return [_decoded("a", 130, 100, 401, 149)]
        return []

    monkeypatch.setattr(zxingcpp, "read_barcodes", decode)
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 10, 52, 6))], _page())
    assert hits["a"] == ("a", "Code128", "region")
    assert len(evidence["a"]["candidates"]) == 1
    assert evidence["a"]["candidates"][0]["rect"] == pytest.approx([13, 10, 40.1, 14.9])


def test_upscaled_local_decode_maps_polygon_back_to_template_mm(monkeypatch):
    monkeypatch.setattr(
        zxingcpp, "read_barcodes",
        lambda image: [_decoded("a", 60, 60, 200, 80)] if image.shape == (400, 1040) else [],
    )
    hits, evidence = _read([BarcodeSpec("a", "front", (8, 8, 52, 20))], _page())
    assert hits["a"] == ("a", "Code128", "region")
    assert evidence["a"]["candidates"][0]["rect"] == pytest.approx([11, 11, 10, 4])
