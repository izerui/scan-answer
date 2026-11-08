from types import SimpleNamespace

from scan_answer.cli import _run_ocr
from scan_answer.ocr.config import OcrConfig
from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_json_atomic
from scan_answer.pipeline.session import BatchSession
from conftest import TEMPLATE_DIR


def test_cli_retry_summary_includes_previously_completed_items(tmp_path, monkeypatch):
    from scan_answer.ocr import queue
    session = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    relative = "students/0001/result.json"
    path = session.batch_dir / relative
    path.parent.mkdir(parents=True)
    (path.parent / "q.jpg").write_bytes(b"image")
    write_json_atomic(path, {
        "subjective": [
            {"no": "1", "images": ["q.jpg"], "ocr_status": "done", "text": "old"},
            {"no": "2", "images": ["q.jpg"], "ocr_status": "failed", "text": None},
        ],
        "student": {"fields": {}}, "warnings": [],
    })
    session.sheets[1] = {"seq": 1, "status": "ok", "result": relative}
    write_json_atomic(session.batch_dir / "state/0001.json", {
        "analysis": {"placements": {}, "swapped": False, "exam_fills": None,
                     "absent_fill": None, "objective_fills": [[0.2]], "barcodes": {}},
        "result": relative, "raw_paths": [],
    })
    session.save(force=True)

    class Client:
        def transcribe(self, images, instruction):
            return SimpleNamespace(text="new", model="local", confidence=None)

    monkeypatch.setattr(queue, "OcrClient", lambda cfg: Client())
    assert _run_ocr(session.batch_dir, OcrConfig("http://localhost:1", "k", "m"), True, False) == 0
    assert read_json(path)["subjective"][1]["text"] == "new"
    assert read_json(session.batch_dir / "manifest.json")["ocr_progress"] == {
        "done": 2, "failed": 0, "total": 2,
    }
