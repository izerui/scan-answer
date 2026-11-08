# 采集可靠性 Implementation Plan

> Use executing-plans and test-driven-development. Disjoint domains may be delegated; parent owns integration and final full-suite verification.

**Goal:** 页面不丢、重扫可回滚、OCR 不被过期任务覆盖。
**Architecture:** Durable acquisition spool, journaled rescan transaction, bounded input pump and generation-checked OCR writes.
**Tech Stack:** Existing Python 3.14, PySide6, TWAIN, OpenCV and pytest.
**Spec:** `docs/superpowers/specs/2026-11-08-acquisition-reliability-design.md`

## Global Constraints

- Preserve previous uncommitted changes; use current shared checkout, no commits or packaging.
- No grading, external OCR requests, uploads or new database.
- A3 and smaller including 390×270 mm 8K.
- Native tests because IDEA MCP discovery returned no tools.

## Review Focus

1. Stop while paused, pending half-card or device release is delayed.
2. Crash between rescan file promotion and manifest commit.
3. Old OCR success/failure after cancellation or result replacement.
4. Same sequence in different batches and delayed Qt signals.
5. Acquisition recovery does not duplicate already registered pages or turn rescan leftovers into new students.

### Task 1: Durable scanner source

Files: source/twain_source.py, source/control.py, new source/spool.py, test_twain_lifecycle.py, test_acquisition_spool.py.

- [x] Red tests for mode mismatch, mandatory capability failure, durable callback pages, stop drain, half-card boundaries and delayed release.
- [x] `TwainSource.bind_storage(batch_dir, target_seq=None)` stores under acquisition/; `released` / `wait_released(timeout)` report actual release. `drain_on_stop=True` enables ControlledSource draining.
- [x] `recover_spooled(batch_dir)` returns recoverable SheetImages grouped by original round, with stable acquisition IDs in origins; rescan targets remain explicit.
- [x] Focused tests green, document exact interfaces for parent/Task2.

### Task 2: Rescan transaction and recovery

Files: pipeline/session.py, new pipeline/attempts.py, worker.py only transaction/recovery metadata, test_rescan_transaction.py.

- [x] Red tests preserving old raw/errors/manifest on new-input or publication failures, crash rollback and 10000+ sequence recovery.
- [x] `RescanAttempt(session, seq)` provides `.session` for isolated processing, `.commit(outcome)` for promotion and `.reject(outcome)` for retained failed attempts. Context manager rolls back incomplete promotion.
- [x] Parent runner.rescan uses staging session; session.open recovers journals and unregistered acquisition pages. Failed attempts never replace old active record.
- [x] Focused transaction/recovery tests green.

### Task 3: Unified OCR ownership

Files: ocr/queue.py, optional ocr/tasks.py, test_ocr_ownership.py.

- [x] Red tests for duplicate enqueue/retry, cancellation, old result overwritten, late failure, graceful close and result deletion.
- [x] Preserve OcrQueue/retry_item/reset_failed/enable_ocr APIs; expose process-wide `cancel_all_ocr()` and `pending_ocr()` covering temporary retries.
- [x] Store per-item generation and content fingerprint, validate under result JSON lock on write. Tell parent metadata fields to preserve in finalize.
- [x] Focused OCR tests green; no broad suite by workers.

### Task 4: Pipeline/control/UI integration

Files: pipeline/runner.py, ui/controller.py, ui/pages/scan_page.py, ui/pages/results_page.py, ui/main_window.py, relevant tests.

- [x] Baseline tests and failing tests for result collection while source paused, stop drain, busy rescan and stale events.
- [x] Bounded reader with acknowledgment; collect submitted workers independently of source waits.
- [x] Rescan staging integration, device storage binding and release state.
- [x] Session-bound event relays, effective OCR config, retry callback ownership and application shutdown.
- [x] Focused tests, independent review/fixes, full fast suite(943 passed), final affected-scope checks(38 passed),200-card regression and docs/report. See `docs/reviews/2026-11-08-package3-implementation.md` for exact validation scope.
