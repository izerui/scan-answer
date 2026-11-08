"""扫描控制器：把流水线放到后台线程，界面只发命令、收信号。

BatchSession / BatchPipeline 不是线程安全的，所以界面线程从不直接调用它们：
所有操作（开始、重扫、作废、结束批次）进入命令队列，由同一个后台线程串行执行；
暂停 / 继续 / 停止通过 ControlledSource 生效（可从任意线程调用）。
流水线事件和转写进度在后台线程发出，经 Qt 信号（跨线程自动排队）送到界面线程。

手写转写的生命周期：关闭批次（结束批次后新建、切换考试、继续另一个批次）时，
该批次的转写队列转入后台继续转完，进度只写回它自己的 manifest，不再更新界面计数；
只有重新打开同一个批次时，才取消它残留的后台队列（防止迟到的进度覆盖新会话的 manifest），
未转完的项保留为 pending，由新会话重新投递。退出程序时取消全部队列。

锁顺序（不得反向获取）：OcrQueue._lock → OcrQueue._emit_lock → BatchSession.lock；
控制器的 _state_lock、_bg_lock 持有期间不调用会获取上述锁的方法（_bg_lock 内只做字典读写）。
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..app.library import Exam
from ..batch import ocr_progress_listener
from ..ocr.config import OcrConfig
from ..ocr.queue import OcrQueue
from ..pipeline.events import DeviceNotice, FinalizedEvent, SheetEvent, SheetQueued, ThresholdEvent, VoidEvent
from ..pipeline.runner import BatchPipeline
from ..pipeline.session import BatchSession
from ..source.base import DeviceEvent, ImageSource, SheetImages
from ..source.control import ControlledSource
from ..source.folder import read_dpi

IDLE, RUNNING, PAUSED, FINISHING, FINALIZED = "idle", "running", "paused", "finishing", "finalized"
PAUSE_ON_DEVICE = {"no_paper", "double_feed", "paper_jam", "error"}


def _key(batch_dir: str | Path) -> Path:
    return Path(batch_dir).resolve()


class ScanController(QObject):
    sheetQueued = Signal(int, str)
    sheetDone = Signal(object)  # SheetEvent
    thresholdChanged = Signal(object)  # ThresholdEvent
    deviceNotice = Signal(object)  # DeviceNotice
    voided = Signal(int)
    finalized = Signal(object)  # FinalizedEvent
    ocrProgress = Signal(object)  # OcrProgress（只反映当前批次）
    stateChanged = Signal(str)
    errorOccurred = Signal(str)
    # 内部中转：后台线程 → 界面线程，在界面线程里校验后再发出对外信号
    _ocrRelay = Signal(object, object)  # (OcrQueue, OcrProgress)
    _stateRelay = Signal(int, str)  # (状态版本号, 状态)

    def __init__(self, workers: int = 0, parent: QObject | None = None):
        super().__init__(parent)
        self.workers = workers
        self.session: BatchSession | None = None
        self._pipeline: BatchPipeline | None = None
        self._ocr: OcrQueue | None = None
        self._source: ControlledSource | None = None
        # 保护 _source 与 state 的配对更新：运行结束（后台线程）与暂停/继续（界面线程）可能同时发生
        self._state_lock = threading.RLock()
        self._state_ver = 0  # 每次改状态加 1
        self._emitted_ver = 0  # 已发出 stateChanged 的最大版本号（只在界面线程读写）
        self._bg: dict[Path, OcrQueue] = {}  # 已关闭批次仍在后台转写的队列，键为批次目录
        self._bg_lock = threading.Lock()
        self._ocrRelay.connect(self._relay_ocr)
        self._stateRelay.connect(self._emit_state)
        self._cmds: queue.Queue = queue.Queue()
        self.state = IDLE
        self._thread = threading.Thread(target=self._loop, name="scan-pipeline", daemon=True)
        self._thread.start()

    # ---------- 事件转信号（后台线程） ----------
    def _on_event(self, e: object) -> None:
        if isinstance(e, SheetQueued):
            self.sheetQueued.emit(e.seq, e.source)
        elif isinstance(e, SheetEvent):
            self.sheetDone.emit(e)
        elif isinstance(e, ThresholdEvent):
            self.thresholdChanged.emit(e)
        elif isinstance(e, DeviceNotice):
            if e.kind in PAUSE_ON_DEVICE:
                # 卡纸、双张、进纸器空：自动暂停，等扫描员处理好纸张后点“继续”
                self.pause()
            self.deviceNotice.emit(e)
        elif isinstance(e, VoidEvent):
            self.voided.emit(e.seq)
        elif isinstance(e, FinalizedEvent):
            self.finalized.emit(e)

    @Slot(object, object)
    def _relay_ocr(self, q: OcrQueue, e: object) -> None:
        """界面线程：只转发当前批次队列的进度（旧批次的迟到事件丢弃）。"""
        if q is self._ocr:
            self.ocrProgress.emit(e)

    def _set_state(self, s: str) -> None:
        """任意线程可调用；界面线程直接发信号，后台线程经中转按版本号发出（过期的丢弃）。"""
        with self._state_lock:
            self._state_ver += 1
            self.state = s
            ver = self._state_ver
        if QThread.currentThread() == self.thread():
            self._emit_state(ver, s)
        else:
            self._stateRelay.emit(ver, s)

    @Slot(int, str)
    def _emit_state(self, ver: int, s: str) -> None:
        if ver <= self._emitted_ver:
            return  # 界面线程已经发出过更新的状态
        self._emitted_ver = ver
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

    def _do_run(self, source: ControlledSource) -> None:
        with self._state_lock:
            # 界面可能在命令排队期间就点了暂停：以图像源的实际状态为准
            self._set_state(PAUSED if source.paused else RUNNING)
        try:
            self._pipeline.run(source)
        finally:
            # 无论成功还是失败都清掉图像源（否则之后再也无法开始），并以后台线程为准写最终状态
            with self._state_lock:
                if self._source is source:
                    self._source = None
                done = self.session is not None and self.session.manifest["finalized"]
                self._set_state(FINALIZED if done else IDLE)

    def _do_rescan(self, seq: int, sheet: SheetImages) -> None:
        self._pipeline.rescan(seq, sheet)

    def _do_rescan_scanner(self, seq: int, source: ImageSource) -> None:
        """用扫描仪扫一张卡替换某个序号（单张模式的图像源，扫到第一张即结束）。"""
        for item in source.sheets():
            if isinstance(item, SheetImages):
                self._pipeline.rescan(seq, item)
                return
            if isinstance(item, DeviceEvent) and item.kind != "notice":
                raise RuntimeError(f"重扫 #{seq:04d} 失败：{item.message}")
        raise RuntimeError(f"重扫 #{seq:04d} 失败：未扫描到纸张，请把答题卡放进进纸器后重试")

    def _do_void(self, seq: int) -> None:
        self._pipeline.void(seq)

    def _do_finalize(self) -> None:
        self._set_state(FINISHING)
        self._pipeline.finalize()
        self._set_state(FINALIZED)

    def _do_close_batch(self, done: threading.Event) -> None:
        ocr, session = self._ocr, self.session
        try:
            if self._pipeline:
                self._pipeline.close()
        finally:
            with self._state_lock:
                self._pipeline = self._ocr = self.session = None
                self._source = None
            try:
                if ocr is not None and session is not None:
                    self._detach_ocr(ocr, session)
            finally:
                done.set()

    # ---------- 后台转写队列 ----------
    def _detach_ocr(self, ocr: OcrQueue, session: BatchSession) -> None:
        """把关闭批次的转写队列转入后台：继续转完，进度只写该批次的 manifest，不发界面信号。"""
        ocr.detach(ocr_progress_listener(session, None))
        k = _key(session.batch_dir)
        with self._bg_lock:
            old = self._bg.get(k)
            self._bg[k] = ocr
        if old is not None and old is not ocr:
            old.close(cancel=True)  # 正常不会发生（重开时已取消）；防御：同一批次只保留一个写 manifest 的队列
        self._prune_bg()

    def _prune_bg(self) -> None:
        """移除已全部完成的后台队列。"""
        with self._bg_lock:
            items = list(self._bg.items())
        finished = [(k, q) for k, q in items if q.idle]
        with self._bg_lock:
            for k, q in finished:
                if self._bg.get(k) is q:
                    del self._bg[k]

    def _cancel_bg(self, batch_dir: str | Path) -> None:
        """取消某批次残留的后台转写：置关闭标志并排空 emit 锁，返回后旧会话不会再写 manifest。"""
        with self._bg_lock:
            q = self._bg.pop(_key(batch_dir), None)
        if q is not None:
            q.close(cancel=True)

    def cancel_all_ocr(self) -> None:
        """取消全部转写（当前与后台）；未完成的项在 result.json 中保持“等待转写”。"""
        with self._bg_lock:
            queues = list(self._bg.values())
            self._bg.clear()
        if self._ocr is not None:
            queues.append(self._ocr)
        for q in queues:
            q.close(cancel=True)

    def ocr_pending(self) -> int:
        """当前批次与后台批次尚未完成的转写项总数。"""
        with self._bg_lock:
            queues = list(self._bg.values())
        cur = self._ocr
        if cur is not None:
            queues.append(cur)
        return sum(q.pending for q in queues)

    # ---------- 对外（界面线程） ----------
    def _install(self, session: BatchSession, ocr_cfg: OcrConfig | None) -> BatchSession:
        """为 session 建好转写队列与流水线，再关闭当前批次并切换；任何一步失败都不切换。"""
        ocr = OcrQueue(ocr_cfg) if ocr_cfg else None
        pipeline = None
        try:
            if ocr is not None:
                ocr.listener = ocr_progress_listener(session, lambda e, q=ocr: self._ocrRelay.emit(q, e))
            pipeline = BatchPipeline(session, workers=self.workers, listener=self._on_event,
                                     on_result=ocr.enqueue if ocr else None)
            self.close_batch()
        except BaseException:
            if pipeline is not None:
                pipeline.close()
            if ocr is not None:
                ocr.close(cancel=True)
            raise
        with self._state_lock:
            self.session, self._ocr, self._pipeline = session, ocr, pipeline
            self._set_state(FINALIZED if session.manifest["finalized"] else IDLE)
        return session

    def new_batch(self, exam: Exam, ocr_cfg: OcrConfig | None = None, scanner: str = "folder",
                  student_dir_pattern: str = "{exam_no}") -> BatchSession:
        session = BatchSession.create(exam.directory, exam.template_dir, exam=exam.info.meta(), scanner=scanner,
                                      student_dir_pattern=student_dir_pattern,
                                      ocr=ocr_cfg is not None)
        return self._install(session, ocr_cfg)

    def open_batch(self, batch_dir: str | Path, ocr_cfg: OcrConfig | None = None) -> BatchSession:
        """继续已有批次（含程序崩溃后重开）；待转写项重新投递。

        打开或对账失败时不切换：目标是别的批次时当前批次保持不变；
        目标就是当前批次时，会先关闭它再对账（避免把正在处理的卡对账成“识别中断”），失败后为无批次状态。"""
        if self.session is not None and _key(self.session.batch_dir) == _key(batch_dir):
            self.close_batch()
        # 该批次若还有后台转写，先取消并排空，再对账（否则迟到的进度会用旧 manifest 覆盖新会话的写入）
        self._cancel_bg(batch_dir)
        session = self._install(BatchSession.open(batch_dir), ocr_cfg)
        if self._ocr:
            for item in session.sheets.values():
                if item.get("status") == "ok" and item.get("result"):
                    try:
                        self._ocr.enqueue(session.batch_dir / item["result"])
                    except (OSError, ValueError, KeyError, TypeError, AttributeError):
                        continue  # result.json 缺失或损坏：跳过该卡的转写
        return session

    @property
    def scanning(self) -> bool:
        """已提交运行且未停止（含命令已排队、尚未开始）。"""
        src = self._source
        return src is not None and not src.stopped

    def start(self, source: ImageSource) -> None:
        with self._state_lock:
            if self.session is None:
                raise RuntimeError("请先新建或打开批次")
            if self._source is not None:
                raise RuntimeError("当前批次正在扫描")
            self._source = ControlledSource(source)
            self._cmds.put(("run", (self._source,)))

    def pause(self) -> None:
        # 只在有运行（排队中或进行中）时生效；运行结束与暂停同时发生时，锁保证不会留下“已暂停”
        with self._state_lock:
            if self._source is None or self.state in (FINISHING, FINALIZED):
                return
            self._source.pause()
            self._set_state(PAUSED)

    def resume(self) -> None:
        with self._state_lock:
            if self._source is None or self.state in (FINISHING, FINALIZED):
                return
            self._source.resume()
            self._set_state(RUNNING)

    def stop(self) -> None:
        src = self._source
        if src:
            src.stop()

    def rescan(self, seq: int, files: list[str | Path]) -> None:
        paths = [Path(f) for f in files]
        sheet = SheetImages(source=paths[0].stem if paths else "重扫", names=[p.name for p in paths],
                            dpis=[read_dpi(p) for p in paths], paths=paths)
        self._cmds.put(("rescan", (seq, sheet)))

    def rescan_scanner(self, seq: int, source: ImageSource) -> None:
        """用扫描仪重扫（source 为单张模式的 TwainSource）。正在扫描时扫描仪被占用，调用方应先结束本轮。"""
        if self.scanning:
            raise RuntimeError("扫描仪正在使用：请先点“结束本轮”，再重扫")
        self._cmds.put(("rescan_scanner", (seq, source)))

    def void(self, seq: int) -> None:
        self._cmds.put(("void", (seq,)))

    def finalize(self) -> None:
        self.stop()
        self._cmds.put(("finalize", ()))

    def close_batch(self, timeout: float = 60.0) -> None:
        """停止当前批次并释放进程池；转写队列不取消，转入后台继续转完（见模块说明）。"""
        if self.session is None:
            return
        self.stop()
        done = threading.Event()
        self._cmds.put(("close_batch", (done,)))
        if not done.wait(timeout):
            raise RuntimeError("关闭批次超时：后台仍在处理，请稍后再试")
        self._set_state(IDLE)

    def shutdown(self) -> None:
        """退出时调用：关闭批次并取消全部转写（当前与后台），不等待在途请求。"""
        if not self._thread.is_alive():
            return
        try:
            self.close_batch(timeout=5)
        except RuntimeError:
            pass
        self.cancel_all_ocr()
        self._cmds.put(("quit", ()))
        self._thread.join(timeout=10)

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """等待已提交的命令全部执行完（测试与“结束批次”后的收尾用）。"""
        done = threading.Event()
        self._cmds.put(("noop", (done,)))
        return done.wait(timeout)

    def _do_noop(self, done: threading.Event) -> None:
        done.set()

    def wait_ocr(self) -> None:
        if self._ocr:
            self._ocr.wait()
