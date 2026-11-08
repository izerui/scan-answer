import importlib
from pathlib import Path

import numpy as np
import pytest

from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.session import BatchSession
from scan_answer.pipeline.worker import SheetOutcome, state_path


@pytest.fixture
def session(tmp_path):
    template = tmp_path / "template"
    template.mkdir()
    (template / "front.jpg").write_bytes(b"template")
    write_json_atomic(template / "template.json", {
        "name": "transaction", "version": "1", "faces": {"front": {"image": "front.jpg"}},
    })
    return BatchSession.create(tmp_path, template, batch_id="batch")


def sheet(name="old.jpg", value=20, origins=None):
    from scan_answer.source.base import SheetImages
    return SheetImages(name, [name], [300], images=[np.full((8, 8), value, np.uint8)],
                       origins=origins or [])


def old_error(session, seq=1, status="physical"):
    old = sheet()
    session.register(seq, old)
    session.save_raw(seq, old)
    write_json_atomic(session.batch_dir / "errors" / f"{seq:04d}_BLANK_PAGE" / "error.json",
                      {"seq": seq, "code": "BLANK_PAGE", "msg": "old"})
    session.record(SheetOutcome(seq, False, physical="BLANK_PAGE", message="old"))
    if status == "voided":
        session.void(seq)
    session.save(force=True)


def active_bytes(session):
    result = {"manifest.json": (session.batch_dir / "manifest.json").read_bytes()}
    for directory in ("raw", "students", "errors", "state"):
        for path in (session.batch_dir / directory).rglob("*"):
            if path.is_file():
                result[str(path.relative_to(session.batch_dir))] = path.read_bytes()
    return result


def attempt_class():
    # A missing implementation is reported as an explicit behavior failure.
    import scan_answer.pipeline as pipeline
    assert (Path(pipeline.__file__).parent / "attempts.py").exists(), "rescan transaction is missing"
    return importlib.import_module("scan_answer.pipeline.attempts").RescanAttempt


def stage_success(attempt, seq=1):
    stage = attempt.session
    new = sheet("new.jpg", 220)
    stage.register(seq, new)
    raw = stage.save_raw(seq, new)
    result = f"students/{seq:04d}_new/result.json"
    write_json_atomic(stage.batch_dir / result, {
        "seq": seq, "student": {"exam_no": {"value": "new"}}, "warnings": [],
        "pages": {"raw_1": f"../../raw/{seq:04d}_new.jpg"},
        "input_pages": [{"raw": f"../../raw/{seq:04d}_new.jpg"}],
    })
    write_json_atomic(state_path(stage.batch_dir, seq), {
        "analysis": {}, "raw_paths": [str(p.resolve()) for p in raw],
        "input_pages": stage.sheets[seq]["input_pages"], "result": result,
    })
    return SheetOutcome(seq, True, result=result, exam_no="new", fills=[0.2])


@pytest.mark.parametrize("status", ["physical", "voided"])
def test_rejected_attempt_preserves_active_evidence_and_retains_input(session, status):
    old_error(session, status=status)
    before = active_bytes(session)
    with attempt_class()(session, 1) as attempt:
        new = sheet("failed.jpg")
        attempt.session.register(1, new)
        attempt.session.save_raw(1, new)
        attempt.reject(SheetOutcome(1, False, physical="MISSING_FACE", message="half"))
    assert active_bytes(session) == before
    assert list((session.batch_dir / "attempts").rglob("0001_failed.jpg"))
    assert any(j.get("phase") == "rejected" for j in
               map(read_json, (session.batch_dir / "attempts").rglob("journal.json")))


def test_input_save_failure_keeps_old_record_and_failed_attempt(session, monkeypatch):
    from PIL import Image
    old_error(session)
    before = active_bytes(session)
    with pytest.raises(OSError, match="disk full"):
        with attempt_class()(session, 1) as attempt:
            attempt.session.register(1, sheet("failed.jpg"))
            monkeypatch.setattr(Image.Image, "save", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
            attempt.session.save_raw(1, sheet("failed.jpg"))
    assert active_bytes(session) == before
    journals = list((session.batch_dir / "attempts").rglob("journal.json"))
    assert journals and "disk full" in str(read_json(journals[0]))
    assert read_json(attempt.session.batch_dir / "manifest.json")["sheets"][0]["files"] == ["failed.jpg"]


def test_success_promotes_normal_contract_and_archives_old_evidence(session):
    old_error(session)
    old_raw = (session.batch_dir / "raw/0001_old.jpg").read_bytes()
    with attempt_class()(session, 1) as attempt:
        outcome = stage_success(attempt)
        assert attempt.commit(outcome) == outcome
    assert session.sheets[1]["status"] == "ok"
    assert (session.batch_dir / outcome.result).is_file()
    assert not (session.batch_dir / "raw/0001_old.jpg").exists()
    assert not (session.batch_dir / "errors/0001_BLANK_PAGE").exists()
    assert read_json(state_path(session.batch_dir, 1))["raw_paths"] == ["raw/0001_new.jpg"]
    assert read_json(session.batch_dir / outcome.result)["content_revision"]
    assert any(p.read_bytes() == old_raw for p in
               (session.batch_dir / "attempts").rglob("0001_old.jpg"))


@pytest.mark.parametrize("failure", ["raw", "manifest"])
def test_publication_failure_restores_old_manifest_and_raw(session, monkeypatch, failure):
    import os
    old_error(session)
    before = active_bytes(session)
    replace = os.replace
    armed = True

    def fail_once(src, dst):
        nonlocal armed
        dst = Path(dst)
        target = session.batch_dir / ("raw/0001_new.jpg" if failure == "raw" else "manifest.json")
        if armed and dst == target:
            armed = False
            raise OSError("publication failed")
        return replace(src, dst)

    with attempt_class()(session, 1) as attempt:
        outcome = stage_success(attempt)
        monkeypatch.setattr(os, "replace", fail_once)
        with pytest.raises(OSError, match="publication failed"):
            attempt.commit(outcome)
    assert active_bytes(session) == before
    assert session.sheets[1]["status"] == "physical"
    assert (attempt.session.batch_dir / "raw/0001_new.jpg").exists()


@pytest.mark.parametrize("legacy_windows", [False, True])
def test_open_rolls_back_process_interruption_during_publication(session, monkeypatch, legacy_windows):
    import os
    old_error(session)
    before = active_bytes(session)
    attempt = attempt_class()(session, 1)
    attempt.__enter__()
    outcome = stage_success(attempt)
    source = attempt.session.batch_dir / "sources/new.pdf"
    source.parent.mkdir()
    source.write_bytes(b"archived document")
    replace = os.replace

    class PowerLoss(BaseException):
        pass

    def interrupt(src, dst):
        if Path(dst) == session.batch_dir / "manifest.json":
            raise PowerLoss()
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", interrupt)
    with pytest.raises(PowerLoss):
        attempt.commit(outcome)
    monkeypatch.setattr(os, "replace", replace)
    journal_path = attempt.folder / "journal.json"
    journal = read_json(journal_path)
    assert all("\\" not in rel for key in ("old_paths", "new_paths") for rel in journal[key])
    assert "sources/new.pdf" in journal["new_paths"]
    if legacy_windows:
        for key in ("old_paths", "new_paths"):
            journal[key] = [rel.replace("/", "\\") for rel in journal[key]]
        write_json_atomic(journal_path, journal)
    reopened = BatchSession.open(session.batch_dir)
    assert active_bytes(reopened) == before
    assert reopened.sheets[1]["status"] == "physical"
    assert not (session.batch_dir / "sources/new.pdf").exists()
    assert active_bytes(BatchSession.open(session.batch_dir)) == before


@pytest.mark.parametrize("relative", [
    r"..\raw\0001_old.jpg", r"raw\..\0001_old.jpg", r"C:\raw\0001_old.jpg",
    r"C:raw\0001_old.jpg", r"\\server\raw\0001_old.jpg", r"\raw\0001_old.jpg",
    r"raw\0002_other.jpg", r"raw\0001_old.jpg:stream",
    "raw\\0001_bad\x00.jpg",
])
def test_legacy_windows_journal_still_rejects_unsafe_paths(session, relative):
    from scan_answer.pipeline.attempts import recover_attempts

    old_error(session)
    before = active_bytes(session)
    folder = session.batch_dir / "attempts/0001/unsafe"
    write_json_atomic(folder / "journal.json", {
        "seq": 1, "phase": "prepared", "old_paths": [relative], "new_paths": [],
    })
    assert recover_attempts(session.batch_dir)
    assert active_bytes(session) == before


def test_only_physical_and_voided_are_eligible(session):
    session.register(1, sheet())
    session.sheets[1]["status"] = "ok"
    with pytest.raises(ValueError):
        attempt_class()(session, 1)
    with pytest.raises(KeyError):
        attempt_class()(session, 2)


def test_prepare_rescan_does_not_remove_active_evidence(session):
    old_error(session)
    before = active_bytes(session)
    session.prepare_rescan(1)
    assert active_bytes(session) == before


def test_open_recovers_full_sequence_and_filename(session):
    session.save_raw(10000, sheet("late.jpg"))
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {10000}
    assert reopened.sheets[10000]["files"] == ["late.jpg"]
    assert reopened.next_seq() == 10001


@pytest.mark.parametrize("state", [
    "{broken", '{"analysis": {}}', '[]',
    '{"analysis": {"placements": {"front": {"source": 1e400}}}}',
])
def test_broken_individual_state_does_not_block_batch_open(session, state):
    old_error(session)
    session.register(2, sheet("other.jpg"))
    session.save_raw(2, sheet("other.jpg"))
    session.sheets[1].update(status="ok", result="students/0001_old/result.json")
    session.save(force=True)
    path = state_path(session.batch_dir, 1)
    path.parent.mkdir(exist_ok=True)
    path.write_text(state)
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {1, 2}
    assert reopened.sheets[1]["status"] == "physical"
    assert (reopened.batch_dir / "raw/0001_old.jpg").exists()


def test_raw_save_failure_cannot_truncate_existing_file(session, monkeypatch):
    import scan_answer.pipeline.session as session_module
    old_error(session)
    before = (session.batch_dir / "raw/0001_old.jpg").read_bytes()
    source = session.batch_dir / "input.jpg"
    source.write_bytes(b"new")
    new = sheet()
    new.images = []
    new.paths = [source]

    def partial_copy(src, dst, *a, **k):
        Path(dst).write_bytes(b"partial")
        raise OSError("copy failed")

    monkeypatch.setattr(session_module.shutil, "copy", partial_copy)
    with pytest.raises(OSError, match="copy failed"):
        session.save_raw(1, new)
    assert (session.batch_dir / "raw/0001_old.jpg").read_bytes() == before


def spool_source(session, monkeypatch, *, target_seq=None):
    import sys
    from types import ModuleType
    from PIL import Image
    from scan_answer.source.base import SheetImages

    path = session.batch_dir / "acquisition" / "page.jpg"
    path.parent.mkdir(exist_ok=True)
    Image.fromarray(np.full((8, 8), 190, np.uint8)).save(path)
    origin = {"kind": "scanner", "acquisition_id": "durable-id", "page": 1}
    if target_seq is not None:
        origin["target_seq"] = target_seq
    recovered = SheetImages("scanner-round", ["page.jpg"], [300], paths=[path], origins=[origin])
    # The scanner agent owns its on-disk format; exercise the agreed SheetImages boundary.
    module = ModuleType("scan_answer.source.spool")
    module.recover_spooled = lambda batch_dir: [recovered]
    monkeypatch.setitem(sys.modules, "scan_answer.source.spool", module)
    return recovered


def test_unregistered_acquisition_recovers_once_as_physical_with_raw(session, monkeypatch):
    spool_source(session, monkeypatch)
    reopened = BatchSession.open(session.batch_dir)
    assert len(reopened.sheets) == 1
    item = reopened.sheets[1]
    assert item["status"] == "physical"
    assert item["input_pages"][0]["acquisition_id"] == "durable-id"
    assert (session.batch_dir / item["input_pages"][0]["raw"]).exists()
    assert len(BatchSession.open(session.batch_dir).sheets) == 1


@pytest.mark.parametrize("target_seq", [1, 999])
def test_rescan_acquisition_leftover_is_audit_not_new_student(session, monkeypatch, target_seq):
    old_error(session)
    before = active_bytes(session)
    spool_source(session, monkeypatch, target_seq=target_seq)
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {1}
    assert active_bytes(reopened) == before
    retained = list((session.batch_dir / "attempts").rglob("manifest.json"))
    assert retained and any("durable-id" in p.read_text() for p in retained)
    BatchSession.open(session.batch_dir)
    assert list((session.batch_dir / "attempts").rglob("manifest.json")) == retained


def test_known_acquisition_restores_missing_raw_without_duplicate(session, monkeypatch):
    recovered = spool_source(session, monkeypatch)
    session.register(8, recovered)
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {8}
    assert (session.batch_dir / "raw/0008_page.jpg").read_bytes() == recovered.paths[0].read_bytes()
    assert reopened.sheets[8]["status"] == "physical"


def test_worker_assigns_distinct_content_revision_on_replacement(ctx, tmp_path):
    from test_worker import _job, _raw
    from scan_answer.pipeline.worker import process_sheet
    paths, _ = _raw(ctx, tmp_path, 233)
    first = process_sheet(_job(tmp_path, 1, paths))
    old_revision = read_json(tmp_path / first.result).get("content_revision")
    second = process_sheet(_job(tmp_path, 1, paths))
    new_revision = read_json(tmp_path / second.result).get("content_revision")
    assert old_revision and new_revision and old_revision != new_revision


def test_finalization_preserves_result_and_item_ocr_ownership():
    from scan_answer.pipeline.worker import _carry_ocr
    old = {
        "content_revision": "content-1", "ocr_result_id": "result-1", "warnings": [],
        "student": {"fields": {"name": {
            "text": None, "ocr_status": "pending", "ocr_generation": "field-gen",
            "ocr_fingerprint": "field-fingerprint",
        }}},
        "subjective": [{"no": "56", "text": None, "ocr_status": "pending",
                        "ocr_generation": "question-gen", "ocr_fingerprint": "question-fingerprint"}],
    }
    new = {"warnings": [], "student": {"fields": {"name": {}}}, "subjective": [{"no": "56"}]}
    result = _carry_ocr(new, old)
    assert result["content_revision"] == "content-1"
    assert result["ocr_result_id"] == "result-1"
    assert result["subjective"][0]["ocr_generation"] == "question-gen"
    assert result["subjective"][0]["ocr_fingerprint"] == "question-fingerprint"
    assert result["student"]["fields"]["name"]["ocr_generation"] == "field-gen"
    assert result["student"]["fields"]["name"]["ocr_fingerprint"] == "field-fingerprint"


def test_real_spool_recovers_rounds_and_rescan_history_once(session):
    from scan_answer.source.spool import AcquisitionSpool
    old_error(session)
    pixels = np.full((8, 8), 70, np.uint8)
    normal = AcquisitionSpool(session.batch_dir, faces=2)
    normal.append(1, pixels, 300)
    normal.append(2, pixels, 300)
    normal.append(2, pixels, 300)
    rescan = AcquisitionSpool(session.batch_dir, faces=2, target_seq=1)
    rescan.append(1, pixels, 300)
    recovered = BatchSession.open(session.batch_dir)
    assert set(recovered.sheets) == {1, 2, 3}
    assert sorted(len(s["input_pages"]) for seq, s in recovered.sheets.items() if seq != 1) == [1, 2]
    for item in recovered.sheets.values():
        assert item["status"] == "physical"
        assert all((session.batch_dir / p["raw"]).exists() for p in item["input_pages"])
    assert len(list((session.batch_dir / "attempts").rglob("journal.json"))) == 1
    again = BatchSession.open(session.batch_dir)
    assert again.sheets == recovered.sheets
    assert len(list((session.batch_dir / "attempts").rglob("journal.json"))) == 1


def test_interrupted_rollback_can_resume_with_overlapping_output_names(session, monkeypatch):
    import os
    import scan_answer.pipeline.attempts as attempts
    old_error(session, status="voided")
    session.save_raw(1, sheet("new.jpg", 40))
    write_json_atomic(session.batch_dir / "students/0001_new/result.json", {"old": "result"})
    before = active_bytes(session)
    attempt = attempt_class()(session, 1)
    outcome = stage_success(attempt)
    replace = os.replace

    class PowerLoss(BaseException):
        pass

    def interrupt_publication(src, dst):
        if Path(dst) == session.batch_dir / "manifest.json":
            raise PowerLoss()
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", interrupt_publication)
    with pytest.raises(PowerLoss):
        attempt.commit(outcome)

    def interrupt_rollback(src, dst):
        if Path(dst) == session.batch_dir / "students/0001_new":
            raise PowerLoss()
        return replace(src, dst)

    monkeypatch.setattr(os, "replace", interrupt_rollback)
    with pytest.raises(PowerLoss):
        attempts.recover_attempts(session.batch_dir)
    monkeypatch.setattr(os, "replace", replace)
    assert active_bytes(BatchSession.open(session.batch_dir)) == before


def test_valid_commit_survives_reopen_with_archived_prior_evidence(session):
    old_error(session)
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
    assert reopened.sheets[1]["status"] == "ok"
    assert list((session.batch_dir / "attempts").rglob("0001_old.jpg"))
    assert (session.batch_dir / outcome.result).exists()


def test_broken_error_json_does_not_block_recovery(session):
    old_error(session, 10001)
    session.sheets[10001]["status"] = "processing"
    session.save(force=True)
    (session.batch_dir / "errors/10001_BLANK_PAGE/error.json").write_text("{bad")
    reopened = BatchSession.open(session.batch_dir)
    assert reopened.sheets[10001]["status"] == "physical"
    assert reopened.next_seq() == 10002


def test_persistent_raw_publication_failure_leaves_old_file_in_place(session, monkeypatch):
    import os
    old_error(session)
    session.save_raw(1, sheet("new.jpg", 40))
    before = active_bytes(session)
    replace = os.replace
    with attempt_class()(session, 1) as attempt:
        outcome = stage_success(attempt)

        def no_publish(src, dst):
            if Path(dst).parent == session.batch_dir / "raw":
                raise OSError("raw directory publication blocked")
            return replace(src, dst)

        monkeypatch.setattr(os, "replace", no_publish)
        with pytest.raises(OSError):
            attempt.commit(outcome)
    assert active_bytes(session) == before


def test_bare_numeric_student_directory_reserves_full_sequence(session):
    write_json_atomic(session.batch_dir / "students/10002/result.json", {"seq": 10002})
    reopened = BatchSession.open(session.batch_dir)
    assert reopened.next_seq() == 10003
    assert reopened.sheets[10002]["status"] == "physical"


@pytest.mark.parametrize("target_seq", [None, 8])
def test_partly_registered_spool_group_retains_only_new_page_as_attempt(session, target_seq):
    from scan_answer.source.spool import AcquisitionSpool, sheet_from_pages
    pixels = np.full((8, 8), 70, np.uint8)
    spool = AcquisitionSpool(session.batch_dir, faces=2, target_seq=target_seq)
    first = spool.append(1, pixels, 300)
    received = sheet_from_pages([first])
    session.register(8, received)
    session.record(SheetOutcome(8, False, physical="MISSING_FACE", message="old half"))
    session.save(force=True)
    second = spool.append(1, pixels + 20, 300)
    # The known page needs restoration too; the new page must not be silently lost.
    reopened = BatchSession.open(session.batch_dir)
    assert set(reopened.sheets) == {8}
    assert len(reopened.sheets[8]["input_pages"]) == 1
    assert (session.batch_dir / "raw/0008_page-000001.png").read_bytes() == first.path.read_bytes()
    histories = list((session.batch_dir / "attempts").glob("*/*/stage/manifest.json"))
    assert len(histories) == 1
    retained = read_json(histories[0])["sheets"]
    assert len(retained) == 1
    assert [o["acquisition_id"] for o in retained[0]["input_pages"]] == [second.origin["acquisition_id"]]
    assert (histories[0].parent / retained[0]["input_pages"][0]["raw"]).read_bytes() == second.path.read_bytes()
    BatchSession.open(session.batch_dir)
    assert list((session.batch_dir / "attempts").glob("*/*/stage/manifest.json")) == histories


def test_partly_registered_spool_does_not_assign_ambiguous_pages_to_student(session):
    from scan_answer.source.spool import AcquisitionSpool, sheet_from_pages
    spool = AcquisitionSpool(session.batch_dir, faces=2)
    first = spool.append(1, np.zeros((8, 8), np.uint8), 300)
    session.register(8, sheet_from_pages([first]))
    session.record(SheetOutcome(8, False, physical="MISSING_FACE"))
    session.save(force=True)
    second = spool.append(1, np.ones((8, 8), np.uint8), 300)
    reopened = BatchSession.open(session.batch_dir)
    assert reopened.sheets[8]["files"] == ["page-000001.png"]
    assert not (session.batch_dir / "raw/0008_page-000002.png").exists()
    paths = list((session.batch_dir / "attempts").rglob("0008_page-000002.png"))
    assert paths and paths[0].read_bytes() == second.path.read_bytes()


def test_context_interruption_restores_memory_and_disk(session, monkeypatch):
    import os
    old_error(session)
    before = active_bytes(session)
    replace = os.replace

    def interrupt(src, dst):
        if Path(dst) == session.batch_dir / "manifest.json":
            raise KeyboardInterrupt()
        return replace(src, dst)

    with pytest.raises(KeyboardInterrupt):
        with attempt_class()(session, 1) as attempt:
            outcome = stage_success(attempt)
            monkeypatch.setattr(os, "replace", interrupt)
            attempt.commit(outcome)
    assert active_bytes(session) == before
    assert session.sheets[1]["status"] == "physical"
    assert 1 not in session.fills
