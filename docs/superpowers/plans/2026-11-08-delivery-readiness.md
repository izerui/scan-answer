# 批次交付 Implementation Plan

> Use writing-plans / test-driven-development / executing-plans; disjoint sidecar tasks may be delegated. Parent owns full-suite verification.

**Goal:** 结算不误报，当前 JSON/图片证据可核验并生成固定可搬移交付目录。
**Architecture:** Read-only audit inventory, atomic directory export, explicit finalized and sealed state, shared lazy evidence viewer.
**Tech Stack:** Python 3.14, PySide6, OpenCV/Pillow, PDFium, pytest.
**Spec:** `docs/superpowers/specs/2026-11-08-delivery-readiness-design.md`

## Constraints

- Preserve all existing dirty work; no commits/push/package.
- No grading, ZIP, business API or real paid OCR.
- No IDE tools discovered; native test/lint checks.
- Worker agents run focused checks only; parent runs full suite after integrations settle.

## Review Focus

1. Physical error arrives after UI precheck; rejudge failure or empty batch must not become finalized.
2. Path escape, bad/zero-byte image, duplicate question, missing region or mixed-student references.
3. OCR or source JSON changes during export; failed publication leaves no target.
4. Move a batch while its old original still exists; always read local new-root files.
5. Sealed delivery can be viewed and checked but cannot mutate through GUI, controller, CLI or OCR backend.

### Task 1: Audit and path inventory
Own output/audit.py, output/paths.py, source/pdf.py(page count only), tests/test_delivery_audit.py.

- [x] Red tests for required files, identities, question coverage, PDF provenance, OCR statuses, invalid shapes/paths.
- [x] `resolve_path(root: Path, base: Path, reference: str) -> Path`: confined relative reference; result ../raw is legal within batch.
- [x] `audit_batch(batch_dir) -> AuditReport`: dataclass ready(bool),status(str),issues(list[dict severity/code/message/seq/path]),files(dict relative->sha256),summary(dict),checked_at(str),to_dict().
- [x] Hash inventory includes manifest/template/assets and referenced current payload; sealed copy additionally checks delivery.json files/revision.
- [x] Focused checks pass; notify parent API.

### Task 2: Portable state and complete redecision
Own pipeline/worker.py, new pipeline/paths.py, tests/test_portable_state.py and test_final_decisions.py.

- [x] Red tests for relocation with old original still present, legacy Windows paths, full exam digit-only changes and metadata preservation.
- [x] Store state raw_paths relative; resolve legacy paths to local batch raw. Use strict local boundaries.
- [x] Complete decision comparison, upgrade old result evidence metadata even when answers unchanged if needed; preserve OCR/content identity and immutable existing crops.
- [x] Targeted worker/portability tests pass; intentional old path assertions updated.

### Task 3: Complete result viewing
Own app/results.py, ui/pages/results_page.py, ui/editor/trial_preview.py, new ui/widgets/evidence_preview.py, tests/test_result_evidence_ui.py.

- [x] Red tests for all fields/barcodes/objective images, lazy loading, clearing stale image and OCR-only warning filtering.
- [x] Shared evidence widget, TrialPreview public interface preserved. Results page adds evidence/JSON view.
- [x] ResultsPage embeds `delivery_panel` via ui/widgets/delivery_panel.py and calls set_batch on open; action guard uses _active_batch_fn.
- [x] Sealed manifest disables retries; focused UI checks pass.

### Task 4: Settlement, export and integration
Own pipeline/runner.py/session.py/events.py, output/delivery.py, batch/CLI, controller, scan/main/settings UI integration, ui/widgets/delivery_panel.py, OCR sealed guards and tests.

- [x] Failure-first settlement tests; FinalizedEvent extends completed/blocked/failed fields compatibly. Mutations invalidate settlement.
- [x] Snapshot export compares inventory and atomically publishes sealed target; guards at mutation entrypoints.
- [x] GUI/CLI check/export integration, settings applied at defined boundary.
- [x] Independent review/fixes,1232 fast tests,200-card regression,1440/1280 screenshots and report: `docs/reviews/2026-11-08-package4-implementation.md`.
