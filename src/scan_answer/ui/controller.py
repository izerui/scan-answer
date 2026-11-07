"""扫描控制器：把流水线放到后台线程，界面只发命令、收信号。

BatchSession / BatchPipeline 不是线程安全的，所以界面线程从不直接调用它们：
所有操作（开始、重扫、作废、结束批次）进入命令队列，由同一个后台线程串行执行；
暂停 / 继续 / 停止通过 ControlledSource 生效（可从任意线程调用）。
流水线事件和转写进度在后台线程发出，经 Qt 信号（跨线程自动排队）送到界面线程。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..app.library import Exam
from ..batch import ocr_progress_listener
from ..ocr.config import OcrConfig
from ..ocr.queue import OcrQueue
from ..pipeline.events import (DeviceNotice, FinalizedEvent, OcrProgress, SheetEvent, SheetQueued, ThresholdEvent,
                               VoidEvent)
from ..pipeline.runner import BatchPipeline
from ..pipeline.session import BatchSession
from ..source.base import ImageSource, SheetImages
from ..source.control import ControlledSource
from ..source.folder import read_dpi

IDLE, RUNNING, PAUSED, FINISHING, FINALIZED = "idle", "running", "paused", "finishing", "finalized"


class ScanController(QObject):
    sheetQueued = Signal(int, str)
    sheetDone = Signal(object)  # SheetEvent
    thresholdChanged = Signal(object)  # ThresholdEvent
    deviceNotice = Signal(object)  # DeviceNotice
    voided = Signal(int)
    finalized = Signal(object)  # FinalizedEvent
    ocrProgress = Signal(object)  # OcrProgress
    stateChanged = Signal(str)
    errorOccurred = Signal(str)

    def __init__(self, workers: int = 0, parent: QObject | None = None):
        super().__init__(parent)
        self.workers = workers
        self.session: BatchSession | None = None
        self._pipeline: BatchPipeline | None = None
        self._ocr: OcrQueue | None = None
        self._source: ControlledSource | None = None
        self._cmds: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._loop, name="scan-pipeline", daemon=True)
        self._thread.start()
        self.state = IDLE

    # ---------- 事件转信号（后台线程） ----------
    def _on_event(self, e: object) -> None:
        if isinstance(e, SheetQueued):
            self.sheetQueued.emit(e.seq, e.source)
        elif isinstance(e, SheetEvent):
            self.sheetDone.emit(e)
        elif isinstance(e, ThresholdEvent):
            self.thresholdChanged.emit(e)
        elif isinstance(e, DeviceNotice):
            self.deviceNotice.emit(e)
        elif isinstance(e, VoidEvent):
            self.voided.emit(e.seq)
        elif isinstance(e, FinalizedEvent):
            self.finalized.emit(e)
        elif isinstance(e, OcrProgress):
            self.ocrProgress.emit(e)

    def _set_state(self, s: str) -> None:
        self.state = s
        self.stateChanged.emit(s)

    # ---------- 后台线程 ----------
    def _loop(self) -> None:
        while True:
            cmd, args = self._cmds.get()
            if cmd == "quit":
                return
            try:
                getattr(self, "_do_" + cmd)(*args)
            except Exception as e:  # noqa: BLE001 — 报给界面，线程继续服务后续命令
                self.errorOccurred.emit(f"{type(e).__name__}: {e}")
                if cmd == "run":
                    self._set_state(IDLE)

    def _do_run(self, source: ControlledSource) -> None:
        # 界面可能在命令排队期间就点了暂停：以图像源的实际状态为准
        self._set_state(PAUSED if source.paused else RUNNING)
        self._pipeline.run(source)
        self._source = None
        self._set_state(IDLE if not self.session.manifest["finalized"] else FINALIZED)

    def _do_rescan(self, seq: int, sheet: SheetImages) -> None:
        self._pipeline.rescan(seq, sheet)

    def _do_void(self, seq: int) -> None:
        self._pipeline.void(seq)

    def _do_finalize(self) -> None:
        self._set_state(FINISHING)
        self._pipeline.finalize()
        self._set_state(FINALIZED)

    def _do_close_batch(self, done: threading.Event) -> None:
        try:
            if self._pipeline:
                self._pipeline.close()
            if self._ocr:
                self._ocr.close()
        finally:
            self._pipeline = self._ocr = self.session = None
            done.set()

    # ---------- 对外（界面线程） ----------
    def _open(self, session: BatchSession, ocr_cfg: OcrConfig | None) -> BatchSession:
        self.close_batch()
        self.session = session
        self._ocr = OcrQueue(ocr_cfg, ocr_progress_listener(session, self._on_event)) if ocr_cfg else None
        self._pipeline = BatchPipeline(session, workers=self.workers, listener=self._on_event,
                                       on_result=self._ocr.enqueue if self._ocr else None)
        self._set_state(FINALIZED if session.manifest["finalized"] else IDLE)
        return session

    def new_batch(self, exam: Exam, ocr_cfg: OcrConfig | None = None, scanner: str = "folder") -> BatchSession:
        session = BatchSession.create(exam.directory, exam.template_dir, exam=exam.info.meta(), scanner=scanner,
                                      ocr=ocr_cfg is not None)
        return self._open(session, ocr_cfg)

    def open_batch(self, batch_dir: str | Path, ocr_cfg: OcrConfig | None = None) -> BatchSession:
        """继续已有批次（含程序崩溃后重开）；待转写项重新投递。"""
        session = self._open(BatchSession.open(batch_dir), ocr_cfg)
        if self._ocr:
            for item in session.sheets.values():
                if item.get("status") == "ok" and item.get("result"):
                    self._ocr.enqueue(session.batch_dir / item["result"])
        return session

    def start(self, source: ImageSource) -> None:
        if self.session is None:
            raise RuntimeError("请先新建或打开批次")
        if self._source is not None:
            raise RuntimeError("当前批次正在扫描")
        self._source = ControlledSource(source)
        self._cmds.put(("run", (self._source,)))

    def pause(self) -> None:
        if self._source:
            self._source.pause()
            self._set_state(PAUSED)

    def resume(self) -> None:
        if self._source:
            self._source.resume()
            self._set_state(RUNNING)

    def stop(self) -> None:
        if self._source:
            self._source.stop()

    def rescan(self, seq: int, files: list[str | Path]) -> None:
        paths = [Path(f) for f in files]
        sheet = SheetImages(source=paths[0].stem if paths else "重扫", names=[p.name for p in paths],
                            dpis=[read_dpi(p) for p in paths], paths=paths)
        self._cmds.put(("rescan", (seq, sheet)))

    def void(self, seq: int) -> None:
        self._cmds.put(("void", (seq,)))

    def finalize(self) -> None:
        self.stop()
        self._cmds.put(("finalize", ()))

    def close_batch(self, timeout: float = 60.0) -> None:
        """停止当前批次并释放进程池、转写线程（等待在途任务完成）。"""
        if self.session is None:
            return
        self.stop()
        done = threading.Event()
        self._cmds.put(("close_batch", (done,)))
        if not done.wait(timeout):
            raise RuntimeError("关闭批次超时：后台仍在处理，请稍后再试")
        self._set_state(IDLE)

    def shutdown(self) -> None:
        try:
            self.close_batch()
        except RuntimeError:
            pass
        self._cmds.put(("quit", ()))
        self._thread.join(timeout=10)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """等待已提交的命令全部执行完（测试与"结束批次"后的收尾用）。"""
        done = threading.Event()
        self._cmds.put(("noop", (done,)))
        return done.wait(timeout)

    def _do_noop(self, done: threading.Event) -> None:
        done.set()

    def wait_ocr(self) -> None:
        if self._ocr:
            self._ocr.wait()
