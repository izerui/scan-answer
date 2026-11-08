import json
import threading
from types import SimpleNamespace

from scan_answer.ocr.config import OcrConfig
from scan_answer.ocr.queue import OcrQueue
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from test_delivery_export import batch as batch, delivery_seed as delivery_seed


def test_legacy_evidence_upgrade_requeues_pending_ocr_with_current_images(batch):
    path = next(batch.glob("students/*/result.json"))
    result = json.loads(path.read_text())
    question = result["subjective"][0]
    canonical = path.parent / question["images"][0]
    canonical.rename(path.parent / "legacy.jpg")
    question["images"][0] = "legacy.jpg"
    question["regions"][0]["image"] = "legacy.jpg"
    question["ocr_status"] = "pending"
    path.write_text(json.dumps(result))
    entered, release_old, new_call = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    count = 0

    class Client:
        def transcribe(self, images, instruction):
            nonlocal count
            with lock:
                count += 1
                own = count
            if own == 1:
                entered.set()
                assert release_old.wait(5)
            else:
                new_call.set()
            return SimpleNamespace(text="old" if own == 1 else "current", model="local", confidence=None)

    queue = OcrQueue(OcrConfig("http://127.0.0.1:1/v1", "k", "local", concurrency=2), client=Client())
    try:
        queue.enqueue(path)
        assert entered.wait(3)
        with BatchPipeline(BatchSession.open(batch), on_result=queue.enqueue) as pipeline:
            assert pipeline.finalize().completed
        assert new_call.wait(2), "upgraded pending evidence needs a current OCR task"
    finally:
        release_old.set()
        queue.wait()
        queue.close()
    current = json.loads(path.read_text())["subjective"][0]
    assert current["text"] == "current" and current["ocr_status"] == "done"
