import numpy as np
import pytest

from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.session import BatchSession
from scan_answer.pipeline.worker import SheetOutcome, state_path
from scan_answer.source.spool import AcquisitionSpool, recover_spooled
from scan_answer.ui.controller import ScanController
from test_rescan_transaction import active_bytes, attempt_class, old_error, stage_success
from test_rescan_transaction import session as session


def test_committed_rescan_does_not_recover_archived_original_as_new_student(session):
    spool = AcquisitionSpool(session.batch_dir, 1)
    spool.append(1, np.full((8, 8), 100, np.uint8), 300)
    original = next(recover_spooled(session.batch_dir))
    session.register(1, original)
    session.save_raw(1, original)
    session.record(SheetOutcome(1, False, physical="BLANK_PAGE"))
    session.save(force=True)
    with attempt_class()(session, 1) as attempt:
        outcome = stage_success(attempt)
        state = read_json(state_path(attempt.session.batch_dir, 1))
        state["analysis"] = {
            "placements": {}, "swapped": False, "exam_fills": None, "absent_fill": None,
            "objective_fills": [[0.2]], "barcodes": {},
        }
        write_json_atomic(state_path(attempt.session.batch_dir, 1), state)
        attempt.commit(outcome)
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {1}
    assert reopened.sheets[1]["status"] == "ok"
    assert set(BatchSession.open(session.batch_dir).sheets) == {1}


def test_rollback_restores_partly_deleted_original_directory(session, monkeypatch):
    from scan_answer.pipeline import attempts
    old_error(session)
    evidence = session.batch_dir / "errors/0001_BLANK_PAGE/face_1.jpg"
    evidence.write_bytes(b"only-original-image")
    before = active_bytes(session)
    attempt = attempt_class()(session, 1)
    outcome = stage_success(attempt)
    remove = attempts._remove

    class PowerLoss(BaseException):
        pass

    def partial_delete(path):
        if path == evidence.parent:
            evidence.unlink()
            raise PowerLoss()
        remove(path)

    monkeypatch.setattr(attempts, "_remove", partial_delete)
    with pytest.raises(PowerLoss):
        attempt.commit(outcome)
    monkeypatch.setattr(attempts, "_remove", remove)
    reopened = BatchSession.open(session.batch_dir)
    assert active_bytes(reopened) == before
    assert evidence.read_bytes() == b"only-original-image"


@pytest.mark.parametrize("bad", ["{broken", '{"phase": "prepared", "seq": 1, "old_paths": "../outside"}'])
def test_bad_historical_journal_is_isolated_and_evidence_retained(session, bad):
    old_error(session)
    with attempt_class()(session, 1) as attempt:
        attempt.reject(SheetOutcome(1, False, physical="MISSING_FACE"))
    journal = attempt.folder / "journal.json"
    journal.write_text(bad)
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {1}
    assert (session.batch_dir / "raw/0001_old.jpg").exists()
    assert any(p.read_text() == bad for p in attempt.folder.glob("journal.corrupt*.json"))
    assert any(e["kind"] == "recovery_warning" for e in reopened.manifest["events"])
    assert set(BatchSession.open(session.batch_dir).sheets) == {1}


def test_retry_refresh_waits_for_background_final_listener(qtbot, tmp_path):
    controller = ScanController()

    class FinishingQueue:
        idle = False

        def close(self, cancel=False):
            self.idle = True

    queue = FinishingQueue()
    root = tmp_path / "batch"
    write_json_atomic(root / "manifest.json", {
        "sheets": [{"seq": 1, "status": "ok", "result": "students/0001/result.json"}],
        "ocr_progress": {"done": 1, "failed": 0, "total": 2},
    })
    write_json_atomic(root / "students/0001/result.json", {
        "subjective": [{"ocr_status": "done"}, {"ocr_status": "done"}], "student": {"fields": {}},
    })
    controller._bg[root.resolve()] = queue
    try:
        controller.refresh_ocr_progress(str(root))
        queue.idle = True
        qtbot.waitUntil(lambda: read_json(root / "manifest.json")["ocr_progress"]["done"] == 2, timeout=2000)
    finally:
        controller.shutdown()


def test_unfinished_publication_must_recover_before_another_rescan(session):
    old_error(session)
    attempt = attempt_class()(session, 1)
    write_json_atomic(attempt.folder / "journal.json", {
        "seq": 1, "phase": "prepared", "attempt_id": attempt.folder.name,
        "old_paths": ["raw/0001_old.jpg"], "new_paths": [],
    })
    before = active_bytes(session)
    with pytest.raises(RuntimeError, match="恢复|重开"):
        attempt_class()(session, 1)
    assert active_bytes(session) == before
