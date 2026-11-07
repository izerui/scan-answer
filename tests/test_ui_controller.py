import shutil
import threading

import pytest

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.ocr.config import OcrConfig
from scan_answer.ocr.queue import enable_ocr
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


def test_closed_batch_ocr_finishes_in_background(ctrl, qtbot, small_dataset, tmp_path):
    """结束批次后旧批次的转写在后台继续转完：回写只进旧批次，不更新当前界面的转写计数。"""
    import json
    import time
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    gate = threading.Event()
    with MockOcrServer("ok", gate=gate) as srv:
        s = ctrl.new_batch(exam, OcrConfig(srv.url, "k", "m", concurrency=2))
        bd = s.batch_dir
        ctrl.start(FolderSource(small_dataset, s.faces))
        qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
        t0 = time.monotonic()
        ctrl.close_batch()  # gate 未放行：转写全部卡住，close_batch 不等待也不取消
        assert time.monotonic() - t0 < 10.0
        assert ctrl.session is None and ctrl.ocr_pending() == 15
        s2 = ctrl.new_batch(exam)  # 当前批次换成新批次（未开转写）
        qtbot.wait(200)  # 处理掉关闭前已排队的信号
        n_signals = len(got["ocr"])
        gate.set()
        qtbot.waitUntil(lambda: ctrl.ocr_pending() == 0, timeout=30000)

        def old_progress():
            return json.loads((bd / "manifest.json").read_text(encoding="utf-8")).get("ocr_progress")
        qtbot.waitUntil(lambda: old_progress() == {"done": 15, "failed": 0, "total": 15}, timeout=10000)
        qtbot.wait(200)
    statuses = []
    for rp in bd.glob("students/*/result.json"):
        r = json.loads(rp.read_text(encoding="utf-8"))
        statuses += [q["ocr_status"] for q in r["subjective"]]
        statuses += [f["ocr_status"] for f in r["student"].get("fields", {}).values()]
    assert len(statuses) == 15 and set(statuses) == {"done"}
    assert len(got["ocr"]) == n_signals, "旧批次的转写进度不应更新当前批次的界面"
    assert read_json(s2.batch_dir / "manifest.json")["ocr_progress"] is None
    assert len(list(s2.batch_dir.glob("students/*"))) == 0


def test_open_batch_skips_broken_result(ctrl, qtbot, small_dataset, tmp_path):
    """重新投递转写时 result.json 缺失：跳过该卡，批次照常打开（不留下“看不见的批次”）。"""
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    ctrl.start(FolderSource(small_dataset, s.faces))
    qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
    bd = s.batch_dir
    ok = [i for i in s.sheets.values() if i.get("status") == "ok"]
    ctrl.new_batch(exam)  # 当前批次是另一个
    for item in ok[1:]:
        per_card = enable_ocr(bd / item["result"])  # 识别时未开转写：事后置为待转写
    (bd / ok[0]["result"]).unlink()
    gate = threading.Event()
    with MockOcrServer("ok", gate=gate) as srv:
        s2 = ctrl.open_batch(bd, OcrConfig(srv.url, "k", "m"))
        assert ctrl.session is s2 and s2.batch_dir == bd
        assert ctrl.ocr_pending() == per_card * (len(ok) - 1) > 0
        gate.set()
        ctrl.wait_ocr()
    ctrl.close_batch()


def test_open_batch_failure_keeps_current(ctrl, qtbot, small_dataset, tmp_path):
    """打开别的批次失败：当前批次不变，仍可扫描；重开当前批次失败：变为无批次（不残留半打开状态）。"""
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    cur = ctrl.new_batch(exam)
    broken = exam.directory / "broken"
    broken.mkdir()
    with pytest.raises(FileNotFoundError):
        ctrl.open_batch(broken)
    assert ctrl.session is cur and ctrl._pipeline is not None and ctrl.state == IDLE
    ctrl.start(FolderSource(small_dataset, cur.faces))
    qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
    assert read_json(cur.batch_dir / "manifest.json")["counts"]["total"] == 4
    # 重开当前批次：先关闭当前批次再对账；manifest 坏了则打开失败、保持无批次
    (cur.batch_dir / "manifest.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(ValueError):
        ctrl.open_batch(cur.batch_dir)
    assert ctrl.session is None and ctrl._pipeline is None
    with pytest.raises(RuntimeError, match="请先新建或打开批次"):
        ctrl.start(FolderSource(small_dataset, 2))


def test_run_failure_allows_restart(ctrl, qtbot, small_dataset, tmp_path, monkeypatch):
    """一次运行抛异常后图像源被清掉，可以再次开始。"""
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)

    def boom(_src):
        raise OSError("磁盘已满")
    monkeypatch.setattr(ctrl._pipeline, "run", boom)
    ctrl.start(FolderSource(small_dataset, s.faces))
    qtbot.waitUntil(lambda: bool(got["errors"]), timeout=10000)
    assert "磁盘已满" in got["errors"][-1]
    assert ctrl.state == IDLE and ctrl._source is None and not ctrl.scanning
    monkeypatch.undo()
    ctrl.start(FolderSource(small_dataset, s.faces))
    qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)


def test_pause_racing_run_end_ends_idle(ctrl, qtbot, tmp_path, monkeypatch):
    """暂停与运行结束同时发生：最终状态以后台线程为准（就绪），界面不会卡在“已暂停”。"""
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    s = ctrl.new_batch(exam)
    entered, release = threading.Event(), threading.Event()

    def blocked(_src):
        entered.set()
        release.wait(10)
    monkeypatch.setattr(ctrl._pipeline, "run", blocked)
    ctrl.start(FolderSource(tmp_path, s.faces))
    assert entered.wait(10)
    ctrl.pause()  # 运行还在（后台阻塞中）：生效
    assert ctrl.state == "paused"
    release.set()  # 运行结束：后台把状态写回就绪
    qtbot.waitUntil(lambda: ctrl.state == IDLE and ctrl._source is None, timeout=10000)
    qtbot.wait(100)
    assert got["states"][-1] == IDLE
    ctrl.pause()  # 运行已结束：不再生效
    ctrl.resume()
    assert ctrl.state == IDLE and got["states"][-1] == IDLE


def test_late_ocr_writeback_does_not_clobber_manifest(ctrl, qtbot, small_dataset, tmp_path, monkeypatch):
    """关闭批次后在途 OCR 任务完成时不应写回旧 session 的 manifest，避免覆盖新数据。

    用 gate Event 阻住 OCR 请求，确保关闭批次时有在途任务；
    monkeypatch MANIFEST_INTERVAL=0 使旧 session 的 save 不被节流。
    """
    import json
    from concurrent.futures import wait as cf_wait
    import scan_answer.pipeline.session as sess_mod
    monkeypatch.setattr(sess_mod, "MANIFEST_INTERVAL", 0)
    got = _record(ctrl)
    exam = create_exam(tmp_path, ExamInfo("考试"), TEMPLATE_DIR)
    gate = threading.Event()
    with MockOcrServer("ok", gate=gate) as srv:
        cfg = OcrConfig(srv.url, "k", "m", concurrency=2)
        s = ctrl.new_batch(exam, cfg)
        bd = s.batch_dir
        ctrl.start(FolderSource(small_dataset, s.faces))
        qtbot.waitUntil(lambda: len(got["done"]) == 4 and ctrl.state == IDLE, timeout=120000)
        # OCR 已投递但被 gate 阻住——保存旧队列的 futures 引用
        old_ocr = ctrl._ocr
        assert old_ocr is not None
        with old_ocr._lock:
            old_futures = list(old_ocr._futures)
        assert len(old_futures) > 0, "应有被阻住的 OCR 任务"
        # 关闭批次（cancel=True，gate 仍未 set，任务被阻塞在 mock server）
        ctrl.close_batch()
        # 重新打开同一批次并扫描第二批
        s2 = ctrl.open_batch(bd)
        ctrl.start(FolderSource(small_dataset, s2.faces))
        qtbot.waitUntil(lambda: len([e for e in got["done"] if e.seq >= 5]) == 4 and ctrl.state == IDLE,
                        timeout=120000)
        n_after = len(json.loads((bd / "manifest.json").read_text())["sheets"])
        assert n_after == 8, f"重新扫描后应有 8 张，实际 {n_after}"
        # 释放 gate，让旧的在途 OCR 任务完成
        gate.set()
        cf_wait(old_futures, timeout=30)
        # 等待可能的 manifest 写盘（MANIFEST_INTERVAL=0 所以不被节流）
        qtbot.wait(500)
        n_later = len(json.loads((bd / "manifest.json").read_text())["sheets"])
        assert n_later == 8, f"旧 OCR 完成后 manifest 被覆盖为 {n_later} 张"
    ctrl.close_batch()
