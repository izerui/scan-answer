# Recognition Evidence Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans or bounded parallel
> workers. Parent owns integrated verification. Do not commit.

**Goal:** Prevent invalid samples and ambiguous barcodes from appearing certain,
and make delivered images match recognition evidence.

**Architecture:** Extend SheetAnalysis with sampling and barcode evidence,
serialize finite measurements/nulls, carry evidence through result and audit.

**Tech Stack:** Python, NumPy, OpenCV, zxing-cpp, PySide6, pytest.

**Spec:** `docs/superpowers/specs/2026-11-08-recognition-evidence-design.md`

## Global Constraints

Preserve previous packages and synthetic generation. A3 and smaller, 8K
390 x 270 mm. No commits, packaging, paid services or unrelated refactors.
Workers run focused tests only; parent runs the complete suites once integrated.

## Review Focus

- Source padding masquerading as valid white paper.
- Identical barcode text at distinct positions and candidate contention.
- Invalid numeric samples leaking nonfinite JSON or calibration.
- Crops, overlays and delivery geometry using different local offsets.
- Re-finalization rewriting OCR evidence or mislabeling legacy processing.

## Task 1: OMR Measurement

- [x] Add failing tests for all bounds, coverage, empty/nonfinite decisions and shallow batches.
- [x] Add `sample_cell(..., coverage=None) -> (float, reason | None)` and
  `sample_grid(..., coverage=None) -> (array, metadata)`; metadata contains
  `shift_mm`, nested `reasons`; retain grid_fills compatibility wrapper.
- [x] Preserve conservative fitting; exclude nonfinite values; diagnose shallow groups.
- [x] Run focused OMR tests (86 passed).

## Task 2: Barcode Evidence

- [x] Test duplicate text at distinct positions, variant union, ambiguity and contention.
- [x] Add spatial candidates and `read_barcode_regions(specs, sources, homographies, dpis)`
  returning legacy-compatible hits plus per-field evidence (status, candidates,
  selected_id, reason). Candidate records contain id, value, symbology, face,
  polygon, rect and located. Geometry is template mm.
- [x] Keep public read_barcode/find_barcodes compatible and run focused tests (27 passed).

## Task 3: Integration

- [x] Add failing tests for source coverage, JSON roundtrip, failed absent,
  effective crops and preserved back handwriting.
- [x] Extend analysis/decision/state and filter invalid calibration input.
- [x] Render effective OMR regions and barcode candidates; declare canonical paths.
- [x] Update audit, finalize expected paths, overlays and preview.
- [x] Verify legacy finalization retains existing evidence and truthful provenance.

## Task 4: Verification and Report

- [x] Run focused integration tests and independent review; fix three findings
  and actual 8K barcode priority regression (204 integration tests passed).
- [x] Run full fast suite (1379 passed) and original 200-card regression (passed).
- [x] Discover IDE checks (unavailable), run scoped Ruff F and diff check (passed).
- [x] Document exact verification and remaining device boundaries in package 5 report.
