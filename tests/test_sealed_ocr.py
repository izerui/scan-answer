import pytest

from scan_answer.ocr.config import OcrConfig
from scan_answer.ocr.queue import OcrQueue, enable_ocr, reset_failed, retry_item
from scan_answer.output.local import write_json_atomic


@pytest.mark.parametrize("operation,status", [
    ("enqueue", "pending"), ("retry", "failed"), ("enable", "none"), ("reset", "failed"),
])
def test_sealed_result_cannot_be_rewritten_by_any_ocr_entrypoint(tmp_path, operation, status):
    path = tmp_path / "students/0001/result.json"
    write_json_atomic(tmp_path / "manifest.json", {"delivery": {"sealed": True, "revision": "frozen"}})
    write_json_atomic(path, {
        "subjective": [{"no": "1", "images": ["q.jpg"], "ocr_status": status}],
        "student": {"fields": {}}, "warnings": [],
    })
    (path.parent / "q.jpg").write_bytes(b"image")
    before = path.read_bytes()
    cfg = OcrConfig("http://127.0.0.1:1/v1", "local", "local", max_retries=0)
    if operation == "retry":
        assert retry_item(path, "subjective", "1", cfg) == (None, "failed")
    elif operation == "enqueue":
        q = OcrQueue(cfg)
        try:
            with pytest.raises(ValueError, match="交付|只读"):
                q.enqueue(path)
        finally:
            q.close(cancel=True)
    else:
        with pytest.raises(ValueError, match="交付|只读"):
            (enable_ocr if operation == "enable" else reset_failed)(path)
    assert path.read_bytes() == before
