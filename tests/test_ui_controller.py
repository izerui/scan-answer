import shutil
import threading

import pytest

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.ocr.config import OcrConfig
from scan_answer.output.jsonfile import read_json
from scan_answer.source.folder import FolderSource
from scan_answer.ui.controller import FINALIZED, IDLE, ScanController

from conftest import TEMPLATE_DIR
from ocr_mock import MockOcrServer


@pytest.fixture
def ctrl(qtbot):
    c = ScanController(workers=0)
    yield c
    c.shutdown()


def _record(c):
    got = {"queued": [], "done": [], "states": [], "voided": [], "finalized": [], "ocr": [], "errors": []}
    c.sheetQueued.connect(lambda seq, _s: got["queued"].append(seq))
    c.sheetDone.connect(lambda e: got["done"].append(e))
    c.stateChanged.connect(got["states"].append)
    c.voided.connect(got["voided"].append)
    c.finalized.connect(got["finalized"].append)
    c.ocrProgress.connect(got["ocr"].append)
    c.errorOccurred.connect(got["errors"].append)
    return got


def test_run_rescan_void_finalize(ctrl, qtbot, small_dataset, tmp_path):
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试", "管综"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    assert s.batch_dir.parent == exam.directory and s.manifest["exam"] == {"name": "考试", "subject": "管综"}
    ctrl.start(FolderSource(small_dataset, s.faces))
    qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
    assert got["queued"] == [1, 2, 3, 4] and "running" in got["states"]
    bad = next(e for e in got["done"] if not e.ok)
    assert bad.seq == 3 and bad.physical == "BLANK_PAGE"
    # 重扫：用第 1 张卡的图替换 #3
    fix = tmp_path / "fix"
    fix.mkdir()
    for f in ("0001_1.jpg", "0001_2.jpg"):
        shutil.copy(small_dataset / f, fix / f)
    ctrl.rescan(3, sorted(fix.iterdir()))
    qtbot.waitUntil(lambda: len(got["done"]) == 5, timeout=60000)
    assert got["done"][-1].seq == 3 and got["done"][-1].ok and got["done"][-1].rescan
    ctrl.void(3)  # #3 重扫后已成功，不能作废 → 后台报错并通过信号送达
    qtbot.waitUntil(lambda: bool(got["errors"]), timeout=10000)
    assert "只能作废物理异常" in got["errors"][-1] and got["voided"] == []
    ctrl.finalize()
    qtbot.waitUntil(lambda: ctrl.state == FINALIZED, timeout=60000)
    assert got["finalized"] and read_json(s.batch_dir / "manifest.json")["finalized"] is True


def test_pause_resume_and_reopen(ctrl, qtbot, small_dataset, tmp_path):
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    ctrl.start(FolderSource(small_dataset, s.faces))
    ctrl.pause()
    qtbot.wait(300)
    n_paused = len(got["queued"])
    assert n_paused <= 1 and ctrl.state == "paused"
    ctrl.resume()
    qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
    with pytest.raises(RuntimeError):
        ctrl._source = object()  # 模拟正在扫描
        ctrl.start(FolderSource(small_dataset, s.faces))
    ctrl._source = None
    batch_dir = s.batch_dir
    ctrl.close_batch()
    assert ctrl.session is None
    s2 = ctrl.open_batch(batch_dir)
    assert s2.next_seq() == 5 and ctrl.state == IDLE


def test_ocr_progress_signal(ctrl, qtbot, small_dataset, tmp_path):
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    with MockOcrServer("ok") as srv:
        s = ctrl.new_batch(exam, OcrConfig(srv.url, "k", "m"))
        ctrl.start(FolderSource(small_dataset, s.faces))
        qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
        ctrl.wait_ocr()
        qtbot.waitUntil(lambda: bool(got["ocr"]) and got["ocr"][-1].done == got["ocr"][-1].total == 3 * 5,
                        timeout=30000)
    assert s.manifest["ocr"] is True


def test_start_without_batch_raises(ctrl):
    with pytest.raises(RuntimeError):
        ctrl.start(FolderSource(".", 2))


def test_close_batch_timeout_raises(ctrl, qtbot, tmp_path, monkeypatch):
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    ctrl.new_batch(exam)
    # 用一个阻塞命令占住后台线程
    blocker = threading.Event()
    monkeypatch.setattr(ctrl, "_do_noop", lambda done: (blocker.wait(), done.set()))
    hold = threading.Event()
    ctrl._cmds.put(("noop", (hold,)))
    # 后台线程被 blocker 阻塞，close_batch 应该超时
    with pytest.raises(RuntimeError, match="关闭批次超时"):
        ctrl.close_batch(timeout=0.2)
    assert ctrl.session is not None  # 超时时不应清理 session
    # 释放阻塞，正常关闭应该成功
    blocker.set()
    hold.wait(5)
    ctrl.close_batch()
    assert ctrl.session is None


def test_close_batch_cancels_slow_ocr(ctrl, qtbot, small_dataset, tmp_path):
    """close_batch 使用 cancel=True 快速关闭 OCR 队列，不等待慢转写完成。"""
    import time
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    with MockOcrServer("slow", delay=2) as srv:
        cfg = OcrConfig(srv.url, "k", "m", concurrency=1)
        s = ctrl.new_batch(exam, cfg)
        ctrl.start(FolderSource(small_dataset, s.faces))
        qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
        # 等一小段让 OCR 队列投递但还未全部完成
        qtbot.wait(500)
        batch_dir = s.batch_dir
        t0 = time.monotonic()
        ctrl.close_batch()
        elapsed = time.monotonic() - t0
        assert elapsed < 10.0, f"close_batch 耗时 {elapsed:.1f}s，应该因 cancel 而快速返回"
        assert ctrl.session is None
        # 检查至少有一些题目仍为 pending（被取消的转写项）
        import json
        results = list(batch_dir.glob("students/*/result.json"))
        pending = 0
        for rp in results:
            r = json.loads(rp.read_text(encoding="utf-8"))
            pending += sum(1 for q in r.get("subjective", []) if q.get("ocr_status") == "pending")
        # 3 张成功卡各 5 道主观题 = 15；slow delay=2s，concurrency=1，最多完成几项
        assert pending > 0, "应该有未完成的 pending 转写项"


def test_late_ocr_writeback_does_not_clobber_manifest(ctrl, qtbot, small_dataset, tmp_path):
    """关闭批次后在途 OCR 任务完成时不应写回旧 session 的 manifest，避免覆盖新数据。"""
    import json
    import time
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    with MockOcrServer("slow", delay=1.5) as srv:
        cfg = OcrConfig(srv.url, "k", "m", concurrency=1)
        s = ctrl.new_batch(exam, cfg)
        bd = s.batch_dir
        ctrl.start(FolderSource(small_dataset, s.faces))
        qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
        # 短暂等待让 OCR 任务投递
        qtbot.wait(300)
        # 关闭批次（cancel=True，在途 OCR 任务可能还在运行）
        ctrl.close_batch()
        # 重新打开同一批次并扫描
        s2 = ctrl.open_batch(bd)
        ctrl.start(FolderSource(small_dataset, s2.faces))
        qtbot.waitUntil(lambda: len([e for e in got["done"] if e.seq >= 5]) == 4 and ctrl.state == IDLE,
                        timeout=120000)
        n_after = len(json.loads((bd / "manifest.json").read_text())["sheets"])
        # 等待足够时间让旧的在途 OCR 任务完成
        time.sleep(3)
        n_later = len(json.loads((bd / "manifest.json").read_text())["sheets"])
        # manifest 应该保持 8 张（两次各 4 张），不应被旧 OCR 回写覆盖为 4
        assert n_after == 8, f"重新扫描后应有 8 张，实际 {n_after}"
        assert n_later == 8, f"旧 OCR 完成后 manifest 被覆盖为 {n_later} 张"
    ctrl.close_batch()
