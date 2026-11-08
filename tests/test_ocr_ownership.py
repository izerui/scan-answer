import copy
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError

import pytest

import scan_answer.ocr.queue as ocr
from scan_answer.ocr.client import OcrError, Transcription
from scan_answer.ocr.config import OcrConfig
from scan_answer.output.jsonfile import read_json, update_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.events import OcrProgress


CFG = OcrConfig("http://127.0.0.1:1/v1", "test", "fake", concurrency=1)


class GatedClient:
    def __init__(self, text="new text", fail=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.text = text
        self.fail = fail

    def transcribe(self, images, instruction):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(10), "test did not release OCR"
        if self.fail:
            raise OcrError("controlled failure")
        return Transcription(self.text, 0.9, "fake")


@pytest.fixture
def result_path(tmp_path):
    (tmp_path / "a.jpg").write_bytes(b"original image")
    path = tmp_path / "result.json"
    write_json_atomic(path, {
        "batch_id": "batch", "seq": 1, "sheet_id": "batch:0001",
        "subjective": [{"no": "1", "images": ["a.jpg"], "content": "zh",
                        "text": None, "ocr_status": "pending"}],
        "student": {"fields": {}}, "warnings": [],
    })
    return path


def item(path):
    return read_json(path)["subjective"][0]


def test_duplicate_enqueue_across_queues_makes_one_request(result_path):
    client = GatedClient()
    first, second = (ocr.OcrQueue(CFG, client=client) for _ in range(2))
    try:
        assert first.enqueue(result_path) == 1
        assert client.entered.wait(5)
        assert first.enqueue(result_path) == 0
        assert second.enqueue(result_path) == 0
    finally:
        client.release.set()
        first.close()
        second.close()
    assert client.calls == 1
    assert item(result_path)["text"] == "new text"


@pytest.mark.parametrize("fail", [False, True])
def test_cancel_invalidates_late_response_and_reopen_can_claim(result_path, fail):
    old = GatedClient("old text", fail)
    queue = ocr.OcrQueue(CFG, client=old)
    queue.enqueue(result_path)
    assert old.entered.wait(5)
    before = item(result_path).get("ocr_generation")
    queue.close(cancel=True)
    try:
        assert item(result_path)["ocr_status"] == "pending"
        assert item(result_path).get("ocr_generation") != before
        fresh = GatedClient("fresh text")
        fresh.release.set()
        reopened = ocr.OcrQueue(CFG, client=fresh)
        reopened.enqueue(result_path)
        reopened.close()
    finally:
        old.release.set()
        queue.wait()
    assert item(result_path)["text"] == "fresh text"
    assert not read_json(result_path)["warnings"]


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.parametrize("replacement", [
    "result", "image", "path", "instruction", "identity", "content_revision", "delete",
])
def test_late_response_cannot_modify_changed_input(result_path, fail, replacement):
    original = read_json(result_path)
    client = GatedClient(fail=fail)
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        queue.enqueue(result_path)
        assert client.entered.wait(5)
        if replacement == "result":
            # A newly published result at the same path, even with identical crops.
            write_json_atomic(result_path, original)
        elif replacement == "image":
            (result_path.parent / "a.jpg").write_bytes(b"replacement image")
        elif replacement == "path":
            (result_path.parent / "b.jpg").write_bytes(b"original image")
            update_json(result_path, lambda r: r["subjective"][0].update(images=["b.jpg"]))
        elif replacement == "instruction":
            update_json(result_path, lambda r: r["subjective"][0].update(content="en"))
        elif replacement == "identity":
            update_json(result_path, lambda r: r.update(sheet_id="batch:0002"))
        elif replacement == "content_revision":
            update_json(result_path, lambda r: r.update(content_revision="replacement"))
        else:
            result_path.unlink()
        expected = read_json(result_path) if result_path.exists() else None
    finally:
        client.release.set()
        queue.close()
    assert (read_json(result_path) if result_path.exists() else None) == expected


def test_retry_joins_active_queue_request(result_path, monkeypatch):
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    monkeypatch.setattr(ocr, "OcrClient", lambda cfg: client)
    with ThreadPoolExecutor(1) as pool:
        try:
            queue.enqueue(result_path)
            assert client.entered.wait(5)
            retry = pool.submit(ocr.retry_item, result_path, "subjective", "1", CFG)
            with pytest.raises(TimeoutError):
                retry.result(timeout=0.1)
        finally:
            client.release.set()
            queue.close()
        assert retry.result(timeout=5) == ("new text", "done")
    assert client.calls == 1


def test_global_cancel_includes_manual_retry_and_temporary_queues(result_path, monkeypatch):
    client = GatedClient()
    monkeypatch.setattr(ocr, "OcrClient", lambda cfg: client)
    with ThreadPoolExecutor(1) as pool:
        retry = pool.submit(ocr.retry_item, result_path, "subjective", "1", CFG)
        try:
            assert client.entered.wait(5)
            assert ocr.pending_ocr() == 1
            assert ocr.pending_ocr(result_path) == 1
            ocr.cancel_all_ocr()
            assert ocr.pending_ocr() == 0
        finally:
            client.release.set()
        assert retry.result(timeout=5) == (None, "pending")
    assert item(result_path)["ocr_status"] == "pending"
    assert item(result_path)["text"] is None

    temporary = ocr.OcrQueue(CFG, client=GatedClient())
    try:
        temporary.enqueue(result_path)
        assert temporary.client.entered.wait(5)
        assert ocr.pending_ocr() == 1
        ocr.cancel_all_ocr()
        assert temporary.pending == ocr.pending_ocr() == 0
    finally:
        temporary.client.release.set()
        temporary.wait()
        temporary.close()
    assert item(result_path)["ocr_status"] == "pending"


def test_graceful_close_delivers_final_progress(result_path):
    client = GatedClient()
    events = []
    queue = ocr.OcrQueue(CFG, events.append, client)
    queue.enqueue(result_path)
    assert client.entered.wait(5)
    with ThreadPoolExecutor(1) as pool:
        closing = pool.submit(queue.close)
        try:
            with pytest.raises(TimeoutError):
                closing.result(timeout=0.1)
        finally:
            client.release.set()
        closing.result(timeout=5)
    assert item(result_path)["ocr_status"] == "done"
    assert events[-1] == OcrProgress(done=1, failed=0, total=1)
    assert queue.idle and queue.pending == 0


def test_recovered_pending_generation_is_reclaimed(result_path):
    update_json(result_path, lambda r: r["subjective"][0].update(
        ocr_generation="previous-process", ocr_fingerprint="old-fingerprint"))
    client = GatedClient()
    client.release.set()
    queue = ocr.OcrQueue(CFG, client=client)
    queue.enqueue(result_path)
    queue.close()
    assert item(result_path)["ocr_status"] == "done"
    assert item(result_path)["ocr_generation"] != "previous-process"


def test_rebuild_preserving_metadata_allows_current_write(result_path):
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        queue.enqueue(result_path)
        assert client.entered.wait(5)
        current = read_json(result_path)
        rebuilt = copy.deepcopy(current)
        rebuilt["objective"] = [{"no": "2", "answer": "B"}]
        write_json_atomic(result_path, rebuilt)
    finally:
        client.release.set()
        queue.close()
    assert item(result_path)["text"] == "new text"
    assert read_json(result_path)["objective"] == [{"no": "2", "answer": "B"}]


def test_completed_future_bookkeeping_is_bounded(result_path):
    client = GatedClient()
    client.release.set()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        for _ in range(40):
            update_json(result_path, lambda r: r["subjective"][0].update(ocr_status="pending"))
            queue.enqueue(result_path)
            queue.wait()
        assert len(queue._futures) <= 1
    finally:
        queue.close()


def test_cancel_before_start_keeps_queued_items_resumable(result_path):
    update_json(result_path, lambda r: r["subjective"].append(
        {"no": "2", "images": ["a.jpg"], "content": "zh", "ocr_status": "pending"}))
    client = GatedClient()
    events = []
    queue = ocr.OcrQueue(CFG, events.append, client)
    queue.enqueue(result_path)
    assert client.entered.wait(5)
    futures = list(queue._futures)
    queue.close(cancel=True)
    notified = len(events)
    client.release.set()
    for future in futures:
        if not future.cancelled():
            future.result(timeout=5)
    assert client.calls == 1
    assert all(q["ocr_status"] == "pending" for q in read_json(result_path)["subjective"])
    assert queue.idle and queue.pending == ocr.pending_ocr() == 0
    assert len(events) == notified
    # wait() must also handle executor-cancelled futures without hanging.
    waiter = threading.Thread(target=queue.wait, daemon=True)
    waiter.start()
    waiter.join(timeout=1)
    assert not waiter.is_alive()


def test_scoped_cancel_leaves_other_result_running(result_path, tmp_path):
    other = tmp_path / "other" / "result.json"
    other.parent.mkdir()
    (other.parent / "a.jpg").write_bytes(b"other image")
    write_json_atomic(other, read_json(result_path))
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        queue.enqueue(result_path)
        assert client.entered.wait(5)
        queue.enqueue(other)
        assert ocr.pending_ocr(tmp_path) == 2
        ocr.cancel_ocr(result_path)
        assert ocr.pending_ocr(result_path) == 0
        assert ocr.pending_ocr(other) == 1
    finally:
        client.release.set()
        queue.close()
    assert item(result_path)["ocr_status"] == "pending"
    assert item(other)["ocr_status"] == "done"


def test_queued_changed_input_is_not_sent_to_client(result_path):
    update_json(result_path, lambda r: r["subjective"].append(
        {"no": "2", "images": ["a.jpg"], "content": "zh", "ocr_status": "pending"}))
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        queue.enqueue(result_path)
        assert client.entered.wait(5)
        update_json(result_path, lambda r: r["subjective"][1].update(content="en"))
    finally:
        client.release.set()
        queue.close()
    assert client.calls == 1
    assert read_json(result_path)["subjective"][1]["ocr_status"] == "pending"


def test_missing_image_marks_failure_without_network_and_retry_recovers(result_path, monkeypatch):
    (result_path.parent / "a.jpg").unlink()
    client = GatedClient()
    client.release.set()
    queue = ocr.OcrQueue(CFG, client=client)
    queue.enqueue(result_path)
    queue.close()
    assert client.calls == 0
    assert item(result_path)["ocr_status"] == "failed"
    assert read_json(result_path)["warnings"][0]["code"] == "OCR_FAILED"
    (result_path.parent / "a.jpg").write_bytes(b"recovered image")
    monkeypatch.setattr(ocr, "OcrClient", lambda cfg: client)
    assert ocr.retry_item(result_path, "subjective", "1", CFG) == ("new text", "done")
    assert not read_json(result_path)["warnings"]


@pytest.mark.parametrize("fail", [False, True])
def test_field_uses_same_ownership_protocol(result_path, fail):
    def field_only(result):
        result["subjective"] = []
        result["student"]["fields"]["name"] = {
            "label": "Name", "image": "a.jpg", "ocr_status": "pending", "text": None,
        }
    update_json(result_path, field_only)
    client = GatedClient(fail=fail)
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        assert queue.enqueue(result_path) == 1
        assert client.entered.wait(5)
        assert queue.enqueue(result_path) == 0
        update_json(result_path, lambda r: r["student"]["fields"]["name"].update(label="Surname"))
        expected = read_json(result_path)
    finally:
        client.release.set()
        queue.close()
    assert read_json(result_path) == expected


def test_recreated_directory_rejects_copied_old_generation(result_path):
    import shutil
    moved = result_path.parent.with_name(result_path.parent.name + "-old")
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        queue.enqueue(result_path)
        assert client.entered.wait(5)
        result_path.parent.rename(moved)
        shutil.copytree(moved, result_path.parent)
        expected = read_json(result_path)
    finally:
        client.release.set()
        queue.close()
        shutil.rmtree(moved)
    assert read_json(result_path) == expected


def test_enqueue_item_claims_before_background_worker_starts(result_path, monkeypatch):
    gate = threading.Event()
    prepare = ocr._prepare

    def blocked_prepare(task):
        assert gate.wait(5)
        return prepare(task)

    monkeypatch.setattr(ocr, "_prepare", blocked_prepare)
    client = GatedClient()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        assert queue.enqueue_item(result_path, "subjective", "1") == 1
        generation = item(result_path)["ocr_generation"]
        assert ocr.pending_ocr(result_path) == queue.pending == 1
        ocr.cancel_all_ocr()
        assert item(result_path)["ocr_generation"] != generation
        assert ocr.pending_ocr() == 0
        assert queue.enqueue_item(result_path, "subjective", "1") == 0
    finally:
        gate.set()
        client.release.set()
        queue.close()
    assert client.calls == 0
    assert item(result_path)["ocr_status"] == "pending"


@pytest.mark.parametrize("fail", [False, True])
def test_enqueue_item_joins_and_waits_for_other_queue(result_path, fail):
    client = GatedClient(fail=fail)
    owner = ocr.OcrQueue(CFG, client=client)
    events = []
    joiner = ocr.OcrQueue(CFG, events.append, client)
    with ThreadPoolExecutor(1) as pool:
        try:
            owner.enqueue(result_path)
            assert client.entered.wait(5)
            assert joiner.enqueue_item(result_path, "subjective", "1") == 0
            assert joiner.enqueue_item(result_path, "subjective", "1") == 0
            assert joiner.pending == joiner.total == ocr.pending_ocr() == 1
            waiting = pool.submit(joiner.wait)
            with pytest.raises(TimeoutError):
                waiting.result(timeout=0.1)
        finally:
            client.release.set()
            owner.close()
            joiner.close()
        waiting.result(timeout=5)
    assert client.calls == 1
    assert joiner.pending == 0 and joiner.idle
    assert events[-1] == OcrProgress(done=int(not fail), failed=int(fail), total=1)
    assert item(result_path)["ocr_status"] == ("failed" if fail else "done")


def test_enqueue_item_joins_manual_retry(result_path, monkeypatch):
    client = GatedClient()
    monkeypatch.setattr(ocr, "OcrClient", lambda cfg: client)
    queue = ocr.OcrQueue(CFG, client=client)
    with ThreadPoolExecutor(1) as pool:
        retry = pool.submit(ocr.retry_item, result_path, "subjective", "1", CFG)
        try:
            assert client.entered.wait(5)
            assert queue.enqueue_item(result_path, "subjective", "1") == 0
            assert queue.pending == ocr.pending_ocr() == 1
        finally:
            client.release.set()
            queue.close()
        assert retry.result(timeout=5) == ("new text", "done")
    assert client.calls == 1
    assert queue.done == queue.total == 1


def test_enqueue_item_retry_policy_and_missing_key(result_path):
    update_json(result_path, lambda r: r["subjective"][0].update(ocr_status="done", text="old"))
    client = GatedClient()
    client.release.set()
    queue = ocr.OcrQueue(CFG, client=client)
    try:
        assert queue.enqueue_item(result_path, "subjective", "missing") == 0
        assert queue.enqueue_item(result_path, "subjective", "1", retry=False) == 0
        assert queue.total == 0
        assert queue.enqueue_item(result_path, "subjective", "1", retry=True) == 1
    finally:
        queue.close()
    assert client.calls == 1
    assert item(result_path)["text"] == "new text"


def test_cancelling_joiner_does_not_cancel_shared_owner(result_path):
    client = GatedClient()
    owner = ocr.OcrQueue(CFG, client=client)
    joiner = ocr.OcrQueue(CFG, client=client)
    try:
        owner.enqueue(result_path)
        assert client.entered.wait(5)
        assert joiner.enqueue_item(result_path, "subjective", "1") == 0
        joiner.close(cancel=True)
        assert ocr.pending_ocr(result_path) == owner.pending == 1
        assert joiner.pending == 0
    finally:
        client.release.set()
        owner.close()
        joiner.close()
    assert item(result_path)["text"] == "new text"
    assert client.calls == 1
