from scan_answer.batch import run_batch
from scan_answer.ocr.config import OcrConfig
from scan_answer.ocr.queue import OcrQueue, enable_ocr, reset_failed
from scan_answer.output.jsonfile import read_json
from scan_answer.pipeline.events import OcrProgress
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR
from ocr_mock import MockOcrServer


def cfg(url, **kw):
    return OcrConfig(base_url=url, api_key="sk-test", model="mock-vl", **kw)


def test_run_batch_with_ocr_end_to_end(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 3, seed=91)
    events = []
    # 作文 57 两个区域：提示词相同；让信息栏（提示词含"栏"）全部失败
    with MockOcrServer("ok", fail_marker="栏的图片") as srv:
        bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b",
                       ocr_config=cfg(srv.url, max_retries=0), listener=events.append)
    m = read_json(bd / "manifest.json")
    assert m["ocr"] is True and m["finalized"] is True
    ok = [x for x in m["sheets"] if x["status"] == "ok"]
    for x in ok:
        r = read_json(bd / x["result"])
        for q in r["subjective"]:
            assert q["ocr_status"] == "done" and q["text"] == f"转写了 {len(q['images'])} 张图"
            assert q["ocr_model"] == "mock-vl" and q["ocr_confidence"] == 0.9
        for f in r["student"]["fields"].values():
            assert f["ocr_status"] == "failed" and f["text"] is None
        assert sum(w["code"] == "OCR_FAILED" for w in r["warnings"]) == len(r["student"]["fields"])
    prog = [e for e in events if isinstance(e, OcrProgress)]
    per_sheet = len(t.subjective) + len(t.fields)
    assert prog[-1] == OcrProgress(done=len(ok) * len(t.subjective), failed=len(ok) * len(t.fields),
                                   total=len(ok) * per_sheet)

    # 重试失败项：服务恢复后全部成功，OCR_FAILED 警告被清除
    with MockOcrServer("ok") as srv:
        q = OcrQueue(cfg(srv.url))
        for x in ok:
            assert reset_failed(bd / x["result"]) == len(t.fields)
            q.enqueue(bd / x["result"])
        q.wait()
        q.close()
    r = read_json(bd / ok[0]["result"])
    assert all(f["ocr_status"] == "done" for f in r["student"]["fields"].values())
    assert not any(w["code"] == "OCR_FAILED" for w in r["warnings"])


def test_enable_ocr_after_the_fact(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 1, seed=92)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    x = next(s for s in read_json(bd / "manifest.json")["sheets"] if s["status"] == "ok")
    r = read_json(bd / x["result"])
    assert all(q["ocr_status"] == "none" for q in r["subjective"])
    assert enable_ocr(bd / x["result"]) == len(t.subjective) + len(t.fields)
    with MockOcrServer("plain") as srv:
        q = OcrQueue(cfg(srv.url))
        q.enqueue(bd / x["result"])
        q.wait()
        q.close()
    r = read_json(bd / x["result"])
    assert r["subjective"][0]["text"].startswith("转写了") and r["subjective"][0]["ocr_confidence"] is None


def test_internal_error_does_not_crash_wait(tpl_bases, tmp_path, monkeypatch):
    """update_json 抛出非 OcrError/OSError 的异常时，wait() 不抛出，计数正确。"""
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 1, seed=93)
    # 不带 OCR 跑批次，items 全为 none
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    x = next(s for s in read_json(bd / "manifest.json")["sheets"] if s["status"] == "ok")
    rp = bd / x["result"]
    n_enabled = enable_ocr(rp)
    assert n_enabled > 0

    # 让 update_json 在转写回写时抛出 ValueError
    import scan_answer.ocr.queue as qmod
    real_update = qmod.update_json
    call_count = 0

    def bad_update(path, fn):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise ValueError("模拟损坏")
        return real_update(path, fn)

    monkeypatch.setattr(qmod, "update_json", bad_update)

    with MockOcrServer("ok") as srv:
        q = OcrQueue(cfg(srv.url))
        n = q.enqueue(rp)
        assert n > 0
        q.wait()  # 不应抛出
        q.close()
    assert q.failed >= 1
    assert q.done + q.failed == q.total
