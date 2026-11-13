"""Concurrency boundaries around durable input, rescan and Qt event delivery."""
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.ocr.config import OcrConfig
from scan_answer.output.jsonfile import read_json
from scan_answer.pipeline.events import DeviceNotice, SheetEvent, SheetQueued
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.pipeline.worker import SheetOutcome
from scan_answer.source.base import DeviceEvent, SheetImages
from scan_answer.source.control import ControlledSource
from scan_answer.ui.controller import ScanController
from conftest import TEMPLATE_DIR


def tiny_sheet(name="sample"):
    return SheetImages(name, [f"{name}.png"], [300], images=[np.full((16, 16), 255, np.uint8)])


def test_input_start_failure_is_reported_instead_of_hanging():
    from scan_answer.pipeline.runner import _InputPump

    class BrokenInput:
        def sheets(self):
            raise OSError("source unavailable")

    pump = _InputPump(BrokenInput())
    pump.thread.start()
    try:
        kind, error, _ack = pump.items.get(timeout=1)
        assert kind == "error" and "source unavailable" in str(error)
    finally:
        pump.close()


def test_paused_input_still_collects_completed_workers(tmp_path, monkeypatch):
    from scan_answer.pipeline import runner
    session = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    completed, submitted = threading.Event(), threading.Event()

    class Input:
        def sheets(self):
            yield tiny_sheet()
            yield tiny_sheet("second")

    source = ControlledSource(Input())
    monkeypatch.setattr(BatchPipeline, "_make_executor", lambda self: ThreadPoolExecutor(2))
    monkeypatch.setattr(runner, "process_sheet",
                        lambda job: SheetOutcome(job.seq, False, physical="MISSING_FACE"))

    def listen(event):
        if isinstance(event, SheetQueued):
            source.pause()
            submitted.set()
        if isinstance(event, SheetEvent):
            completed.set()

    with BatchPipeline(session, workers=2, listener=listen) as pipeline:
        thread = threading.Thread(target=pipeline.run, args=(source,))
        thread.start()
        try:
            assert submitted.wait(3)
            assert completed.wait(3), "finished recognition must be recorded while input is paused"
            assert session.sheets[1]["status"] == "physical"
        finally:
            source.stop()
            thread.join(5)
    assert not thread.is_alive()


def test_device_notice_is_handled_before_next_round(tmp_path):
    session = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    notice, advanced = threading.Event(), threading.Event()

    class Input:
        def sheets(self):
            yield DeviceEvent("no_paper", "empty")
            advanced.set()

    source = ControlledSource(Input())

    def listen(event):
        if isinstance(event, DeviceNotice):
            source.pause()
            notice.set()

    with BatchPipeline(session, listener=listen) as pipeline:
        thread = threading.Thread(target=pipeline.run, args=(source,))
        thread.start()
        try:
            assert notice.wait(3)
            assert not advanced.wait(0.1)
        finally:
            source.stop()
            thread.join(5)
    assert not advanced.is_set()


@pytest.fixture
def control(qtbot, tmp_path):
    controller = ScanController()
    exam = create_exam(tmp_path, ExamInfo("Capture"), TEMPLATE_DIR)
    controller.new_batch(exam)
    yield controller, exam
    controller.shutdown()


def test_rescan_is_busy_before_command_executes(control, qtbot, monkeypatch):
    controller, exam = control
    entered, finish = threading.Event(), threading.Event()

    def rescan(seq, sheet):
        entered.set()
        assert finish.wait(5)

    monkeypatch.setattr(controller._pipeline, "rescan", rescan)
    try:
        controller.rescan(1, ["front.jpg", "back.jpg"])
        assert controller.scanning
        assert entered.wait(3)
        with pytest.raises(RuntimeError):
            controller.rescan(1, ["front.jpg", "back.jpg"])
        with pytest.raises(RuntimeError):
            controller.new_batch(exam)
    finally:
        finish.set()
    qtbot.waitUntil(lambda: not controller.scanning, timeout=5000)
    assert controller.state == "idle"


def test_old_batch_events_are_dropped_at_ui_delivery(control, qtbot):
    controller, exam = control
    got = []
    controller.sheetDone.connect(got.append)
    event = SheetEvent(1, "old", False, "MISSING_FACE", "old", None, 0)
    listener = controller._pipeline.listener
    thread = threading.Thread(target=lambda: listener(event))
    thread.start()
    thread.join(3)
    controller.new_batch(exam)
    qtbot.wait(50)
    assert got == []


def test_device_remains_owned_until_actual_release(control, qtbot):
    controller, _ = control
    release = threading.Event()

    class DelayedRelease:
        drain_on_stop = True

        @property
        def released(self):
            return release.is_set()

        def wait_released(self, timeout=None):
            return release.wait(timeout)

        def sheets(self):
            yield from ()

        def stop(self):
            pass

    source = DelayedRelease()
    try:
        controller.start(source)
        assert controller.wait_idle(3)
        assert controller.scanning
        with pytest.raises(RuntimeError):
            controller.start(DelayedRelease())
        release.set()
        qtbot.waitUntil(lambda: not controller.scanning, timeout=5000)
        assert controller.state == "idle"
    finally:
        release.set()


def test_reopened_ocr_switch_matches_actual_queue(control):
    controller, _ = control
    directory = controller.session.batch_dir
    config = OcrConfig("http://127.0.0.1:1/v1", "test", "test")
    enabled = controller.open_batch(directory, config)
    assert enabled.manifest["ocr"] is True
    assert read_json(directory / "manifest.json")["ocr"] is True
    disabled = controller.open_batch(directory)
    assert disabled.manifest["ocr"] is False
    assert read_json(directory / "manifest.json")["ocr"] is False


def test_scanner_storage_is_bound_on_the_scan_thread(control, qtbot):
    """扫描仪的采集暂存目录（建目录、落盘）在扫描线程上、开始读取前建，不在界面线程等磁盘。"""
    controller, _ = control
    bound = []

    class Scanner:
        released = True

        def bind_storage(self, batch_dir, target_seq=None):
            bound.append((threading.current_thread() is threading.main_thread(), target_seq))

        def sheets(self):
            assert bound, "开始读取前要先建好暂存目录"
            yield from ()

        def stop(self):
            pass

    controller.start(Scanner())
    qtbot.waitUntil(lambda: not controller.scanning, timeout=5000)
    controller.rescan_scanner(7, Scanner())
    qtbot.waitUntil(lambda: not controller.scanning, timeout=5000)
    assert bound == [(False, None), (False, 7)]

