import pytest

from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.pipeline.worker import FinalizeOutcome
from scan_answer.source.base import SheetImages
from conftest import TEMPLATE_DIR


@pytest.fixture
def session(tmp_path):
    return BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="settlement")


def test_settlement_checks_unresolved_sheets_in_backend(session):
    session.sheets = {
        1: {"seq": 1, "status": "processing"},
        2: {"seq": 2, "status": "physical", "physical": "MISSING_FACE"},
    }
    session.save(force=True)
    with BatchPipeline(session) as pipeline:
        event = pipeline.finalize()
    assert not session.manifest["finalized"]
    assert not event.completed
    assert event.blocked_seqs == [1, 2]
    assert not read_json(session.batch_dir / "manifest.json")["finalized"]


def test_empty_batch_is_not_finalized(session):
    with BatchPipeline(session) as pipeline:
        event = pipeline.finalize()
    assert not event.completed
    assert not session.manifest["finalized"]


def test_failed_redecision_is_retryable_and_does_not_mark_complete(session, monkeypatch):
    from scan_answer.pipeline import runner
    session.sheets = {i: {"seq": i, "status": "ok", "source": str(i)} for i in (1, 2)}
    session.save(force=True)

    def one_failure(job):
        if job.seq == 1:
            raise OSError("unreadable evidence")
        return FinalizeOutcome(job.seq, True, "exam2", 1)

    monkeypatch.setattr(runner, "finalize_sheet", one_failure)
    with BatchPipeline(session) as pipeline:
        event = pipeline.finalize()
        assert not event.completed
        assert event.failed_seqs == [1]
        assert session.sheets[2]["exam_no"] == "exam2"
        assert "unreadable" in session.sheets[1]["finalize_error"]
        assert not session.manifest["finalized"]
        monkeypatch.setattr(runner, "finalize_sheet", lambda job: FinalizeOutcome(job.seq, False))
        event = pipeline.finalize()
    assert event.completed and session.manifest["finalized"]
    assert all("finalize_error" not in item for item in session.sheets.values())


@pytest.mark.parametrize("operation", ["register", "void", "record"])
def test_changing_effective_sheets_invalidates_settlement(session, operation):
    from scan_answer.pipeline.worker import SheetOutcome
    session.sheets[1] = {"seq": 1, "source": "old", "status": "physical"}
    session.manifest["finalized"] = True
    session.save(force=True)
    if operation == "register":
        session.register(2, SheetImages("new", ["new.jpg"], [300]))
    elif operation == "void":
        session.void(1)
    else:
        session.record(SheetOutcome(1, False, physical="MISSING_FACE"))
    assert not session.manifest["finalized"]


@pytest.mark.parametrize("operation", ["open", "save", "register", "void", "prepare_rescan", "create_nested", "exam"])
def test_sealed_delivery_is_not_a_mutable_batch(session, operation):
    session.sheets[1] = {"seq": 1, "source": "old", "status": "physical"}
    session.save(force=True)
    manifest = read_json(session.batch_dir / "manifest.json")
    manifest["delivery"] = {"sealed": True, "revision": "frozen"}
    write_json_atomic(session.batch_dir / "manifest.json", manifest)
    before = (session.batch_dir / "manifest.json").read_bytes()
    with pytest.raises(ValueError, match="交付|只读"):
        if operation == "open":
            BatchSession.open(session.batch_dir)
        elif operation == "save":
            session.save(force=True)
        elif operation == "register":
            session.register(2, SheetImages("new", ["new.jpg"], [300]))
        elif operation == "void":
            session.void(1)
        elif operation == "create_nested":
            BatchSession.create(session.batch_dir / "nested", TEMPLATE_DIR, batch_id="new")
        elif operation == "exam":
            from scan_answer.app.library import ExamInfo, create_exam
            create_exam(session.batch_dir / "nested", ExamInfo("new"), TEMPLATE_DIR)
        else:
            session.prepare_rescan(1)
    assert (session.batch_dir / "manifest.json").read_bytes() == before


def test_successful_finalization_event_keeps_legacy_constructor():
    from scan_answer.pipeline.events import FinalizedEvent
    event = FinalizedEvent(0.4, 0.2, 1, [1], [])
    assert event.completed and not event.failed_seqs and not event.blocked_seqs


def test_late_settlement_cannot_complete_newer_batch_generation(session, monkeypatch):
    import threading
    from scan_answer.pipeline import runner
    from scan_answer.pipeline.worker import SheetOutcome
    session.sheets[1] = {"seq": 1, "source": "first", "status": "ok"}
    session.save(force=True)
    entered, release = threading.Event(), threading.Event()

    def rejudge(job):
        entered.set()
        assert release.wait(5)
        return FinalizeOutcome(job.seq, False, "old-value")

    monkeypatch.setattr(runner, "finalize_sheet", rejudge)
    events = []
    with BatchPipeline(session, listener=events.append) as pipeline:
        thread = threading.Thread(target=pipeline.finalize)
        thread.start()
        try:
            assert entered.wait(3)
            session.register(2, SheetImages("new", ["new.jpg"], [300]))
            session.record(SheetOutcome(2, True, result="students/0002/result.json"))
        finally:
            release.set()
            thread.join(5)
    assert not session.manifest["finalized"]
    assert not events[-1].completed
    assert "exam_no" not in session.sheets[1], "stale redecision must not update current metadata"


def test_late_settlement_preserves_a_newer_completed_settlement(session, monkeypatch):
    import threading
    from scan_answer.pipeline import runner
    from scan_answer.pipeline.worker import SheetOutcome
    session.sheets[1] = {"seq": 1, "source": "first", "status": "ok"}
    entered, release = threading.Event(), threading.Event()

    def old_rejudge(job):
        entered.set()
        assert release.wait(5)
        return FinalizeOutcome(job.seq, False, "old")

    monkeypatch.setattr(runner, "finalize_sheet", old_rejudge)
    with BatchPipeline(session) as old:
        thread = threading.Thread(target=old.finalize)
        thread.start()
        try:
            assert entered.wait(3)
            session.register(2, SheetImages("new", ["new.jpg"], [300]))
            session.record(SheetOutcome(2, True))
            monkeypatch.setattr(runner, "finalize_sheet", lambda job: FinalizeOutcome(job.seq, False, "new"))
            with BatchPipeline(session) as current:
                assert current.finalize().completed
            settled = dict(session.manifest["finalization"])
        finally:
            release.set()
            thread.join(5)
    assert session.manifest["finalized"]
    assert session.manifest["finalization"] == settled
    assert session.sheets[1]["exam_no"] == "new"
