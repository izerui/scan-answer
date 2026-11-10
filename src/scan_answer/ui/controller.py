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
from contextlib import closing
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from ..app.library import Exam, ensure_exam_snapshot
from ..batch import ocr_progress_listener, result_ocr_progress
from ..output.jsonfile import update_json
from ..ocr.config import OcrConfig
from ..ocr.queue import OcrQueue
from ..pipeline.events import (DeviceNotice, FinalizedEvent, SheetEvent, SheetQueued, SheetSkipped, ThresholdEvent,
                               UnvoidEvent, VoidEvent)
from ..pipeline.runner import BatchPipeline
from ..pipeline.session import BatchSession
from ..source.base import DeviceEvent, ImageSource, SheetImages
from ..source.control import ControlledSource
from ..source.folder import read_dpi

IDLE, RUNNING, PAUSED, FINISHING, FINALIZED = "idle", "running", "paused", "finishing", "finalized"
RESCANNING, STOPPING = "rescanning", "stopping"
RELOCATING, REPROCESSING = "relocating", "reprocessing"  # 手动定位 / 用原图重新识别（都不占用图像源）
BUSY_STATES = (RUNNING, PAUSED, FINISHING, RESCANNING, STOPPING, RELOCATING, REPROCESSING)
PAUSE_ON_DEVICE = {"no_paper", "double_feed", "paper_jam", "error"}


def _key(batch_dir: str | Path) -> Path:
    return Path(batch_dir).resolve()


class ScanController(QObject):
    sheetQueued = Signal(int, str)
    sheetDone = Signal(object)  # SheetEvent
    thresholdChanged = Signal(object)  # ThresholdEvent
    deviceNotice = Signal(object)  # DeviceNotice
    voided = Signal(int)
    unvoided = Signal(int)
    sheetSkipped = Signal(object)  # SheetSkipped：重复导入、未再登记的答卷
    finalized = Signal(object)  # FinalizedEvent
    ocrProgress = Signal(object)  # OcrProgress（只反映当前批次）
    stateChanged = Signal(str)
    errorOccurred = Signal(str)
    # 内部中转：后台线程 → 界面线程，在界面线程里校验后再发出对外信号
    _ocrRelay = Signal(object, object)  # (OcrQueue, OcrProgress)
    _stateRelay = Signal(int, str)  # (状态版本号, 状态)
    _eventRelay = Signal(object, object)  # (owning session, event)
    _errorRelay = Signal(object, str)

    def __init__(self, workers: int = 0, parent: QObject | None = None):
        super().__init__(parent)
        self.workers = workers
        self.session: BatchSession | None = None
        self._pipeline: BatchPipeline | None = None
        self._ocr: OcrQueue | None = None
        self._source: ControlledSource | None = None
        self._operation: str | None = None
        self._finalizing = False
        # 保护 _source 与 state 的配对更新：运行结束（后台线程）与暂停/继续（界面线程）可能同时发生
        self._state_lock = threading.RLock()
        self._state_ver = 0  # 每次改状态加 1
        self._emitted_ver = 0  # 已发出 stateChanged 的最大版本号（只在界面线程读写）
        self._bg: dict[Path, OcrQueue] = {}  # 已关闭批次仍在后台转写的队列，键为批次目录
        self._bg_lock = threading.Lock()
        self._progress_refreshes: set[Path] = set()
        self._progress_timer = QTimer(self)
        self._progress_timer.setInterval(50)
        self._progress_timer.timeout.connect(self._refresh_waiting_progress)
        self._ocrRelay.connect(self._relay_ocr)
        self._stateRelay.connect(self._emit_state)
        self._eventRelay.connect(self._relay_event)
        self._errorRelay.connect(self._relay_error)
        self._cmds: queue.Queue = queue.Queue()
        self.state = IDLE
        self._thread = threading.Thread(target=self._loop, name="scan-pipeline", daemon=True)
        self._thread.start()

    # ---------- 事件转信号（后台线程） ----------
    def _receive_event(self, session: BatchSession, event: object) -> None:
        if session is not self.session:
            return
        if isinstance(event, DeviceNotice) and event.kind in PAUSE_ON_DEVICE:
            self.pause()
        self._eventRelay.emit(session, event)

    @Slot(object, object)
    def _relay_event(self, session, event) -> None:
        if session is self.session:
            self._on_event(event)

    @Slot(object, str)
    def _relay_error(self, session, message: str) -> None:
        if session is self.session:
            self.errorOccurred.emit(message)

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
        elif isinstance(e, UnvoidEvent):
            self.unvoided.emit(e.seq)
        elif isinstance(e, SheetSkipped):
            self.sheetSkipped.emit(e)
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
            owner = self.session
            try:
                getattr(self, "_do_" + cmd)(*args)
            except Exception as e:  # noqa: BLE001 — 报给界面，线程继续服务后续命令
                self._errorRelay.emit(owner, f"{type(e).__name__}: {e}")

    def _do_run(self, source: ControlledSource) -> None:
        with self._state_lock:
            # 界面可能在命令排队期间就点了暂停：以图像源的实际状态为准
            if not self._finalizing:
                self._set_state(STOPPING if source.stopped else PAUSED if source.paused else RUNNING)
        try:
            self._pipeline.run(source)
        finally:
            self._finish_operation(source)

    @staticmethod
    def _released(source: ControlledSource | None) -> bool:
        return source is None or bool(getattr(source.inner, "released", True))

    def _finish_operation(self, source: ControlledSource | None = None) -> None:
        with self._state_lock:
            if source is not None and not self._released(source):
                self._set_state(STOPPING)
                threading.Thread(target=self._await_release, args=(source,),
                                 name="scan-release", daemon=True).start()
                return
            if source is None or self._source is source:
                self._source = None
                self._operation = None
            done = self.session is not None and self.session.manifest["finalized"]
            self._set_state(FINISHING if self._finalizing else FINALIZED if done else IDLE)

    def _await_release(self, source: ControlledSource) -> None:
        wait = getattr(source.inner, "wait_released", None)
        if callable(wait):
            wait(None)
        else:
            while not self._released(source):
                threading.Event().wait(0.1)
        with self._state_lock:
            if self._source is source and self._released(source):
                self._finish_operation(source)

    def _do_rescan(self, seq: int, sheet) -> None:
        try:
            # PDF 重扫在后台渲染：sheet 是返回 SheetImages 的函数
            self._pipeline.rescan(seq, sheet() if callable(sheet) else sheet)
        finally:
            self._finish_operation()

    def _do_relocate(self, seq: int, manual: dict, confirm_identity: bool = False) -> None:
        try:
            self._pipeline.relocate(seq, manual, confirm_identity)
        finally:
            self._finish_operation()

    def _do_reprocess(self, seq: int, confirm_identity: bool = False) -> None:
        try:
            self._pipeline.reprocess(seq, confirm_identity)
        finally:
            self._finish_operation()

    def _do_rescan_scanner(self, seq: int, source: ControlledSource) -> None:
        """Drain and release the device before accepting a single replacement."""
        sheets = []
        try:
            with closing(source.sheets()) as items:
                for item in items:
                    if isinstance(item, SheetImages):
                        sheets.append(item)
                    elif isinstance(item, DeviceEvent) and item.kind != "notice":
                        raise RuntimeError(f"重扫 #{seq:04d} 失败：{item.message}")
            if len(sheets) != 1:
                raise RuntimeError(f"重扫 #{seq:04d} 需要一份完整答卷，实际收到 {len(sheets)} 份；旧证据已保留")
            self._pipeline.rescan(seq, sheets[0])
        finally:
            self._finish_operation(source)

    def _do_void(self, seq: int) -> None:
        self._pipeline.void(seq)

    def _do_unvoid(self, seq: int) -> None:
        self._pipeline.unvoid(seq)

    def _do_finalize(self) -> None:
        try:
            if not self._released(self._source):
                raise RuntimeError("设备尚未释放，暂不能结束批次")
            self._set_state(FINISHING)
            self._pipeline.finalize()
        finally:
            self._finalizing = False
            self._finish_operation(self._source)

    def _do_close_batch(self, done: threading.Event) -> None:
        ocr, session = self._ocr, self.session
        try:
            if not self._released(self._source):
                raise RuntimeError("设备尚未释放，不能切换批次")
            if self._pipeline:
                self._pipeline.close()
            with self._state_lock:
                self._pipeline = self._ocr = self.session = None
                self._source = None
                self._operation = None
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
        from ..ocr.queue import cancel_all_ocr
        cancel_all_ocr()

    def ocr_pending(self) -> int:
        """当前批次与后台批次尚未完成的转写项总数。"""
        from ..ocr.queue import pending_ocr
        return pending_ocr()

    @Slot(str)
    def refresh_ocr_progress(self, batch_dir: str) -> None:
        """Retry completion updates its owning batch, even after navigation."""
        try:
            current = self.session
            if current is not None and _key(current.batch_dir) == _key(batch_dir):
                with current.lock:
                    progress = result_ocr_progress(current.batch_dir, list(current.sheets.values()))
                    current.set_ocr_progress(progress.done, progress.failed, progress.total, force=True)
                self.ocrProgress.emit(progress)
                return
            with self._bg_lock:
                background = self._bg.get(_key(batch_dir))
            if background is not None and not background.idle:
                self._progress_refreshes.add(_key(batch_dir))
                self._progress_timer.start()
                return
            progress = result_ocr_progress(batch_dir)
            update_json(Path(batch_dir) / "manifest.json", lambda record: record.update(
                ocr_progress={"done": progress.done, "failed": progress.failed, "total": progress.total}))
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def _refresh_waiting_progress(self) -> None:
        waiting, self._progress_refreshes = self._progress_refreshes, set()
        for directory in waiting:
            self.refresh_ocr_progress(str(directory))
        if not self._progress_refreshes:
            self._progress_timer.stop()

    # ---------- 对外（界面线程） ----------
    def _install(self, session: BatchSession, ocr_cfg: OcrConfig | None) -> BatchSession:
        """为 session 建好转写队列与流水线，再关闭当前批次并切换；任何一步失败都不切换。"""
        ocr = OcrQueue(ocr_cfg) if ocr_cfg else None
        pipeline = None
        try:
            if ocr is not None:
                ocr.listener = ocr_progress_listener(session, lambda e, q=ocr: self._ocrRelay.emit(q, e))
            pipeline = BatchPipeline(session, workers=self.workers,
                                     listener=lambda e, s=session: self._receive_event(s, e),
                                     on_result=ocr.enqueue if ocr else None)
            self.close_batch()
            session.manifest["ocr"] = ocr is not None
            session.save(force=True)
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
        self._assert_idle()
        template_dir = ensure_exam_snapshot(exam)
        session = BatchSession.create(exam.directory, template_dir, exam=exam.info.meta(), scanner=scanner,
                                      student_dir_pattern=student_dir_pattern,
                                      ocr=ocr_cfg is not None)
        return self._install(session, ocr_cfg)

    def open_batch(self, batch_dir: str | Path, ocr_cfg: OcrConfig | None = None) -> BatchSession:
        """继续已有批次（含程序崩溃后重开）；待转写项重新投递。

        打开或对账失败时不切换：目标是别的批次时当前批次保持不变；
        目标就是当前批次时，会先关闭它再对账（避免把正在处理的卡对账成“识别中断”），失败后为无批次状态。"""
        self._assert_idle()
        if self.session is not None and _key(self.session.batch_dir) == _key(batch_dir):
            self.close_batch()
        # 该批次若还有后台转写，先取消并排空，再对账（否则迟到的进度会用旧 manifest 覆盖新会话的写入）
        self._cancel_bg(batch_dir)
        from ..ocr.queue import cancel_ocr
        cancel_ocr(batch_dir)
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
        """Input/replacement remains owned through drain and device release."""
        return self._source is not None or self._operation is not None or self._finalizing

    def _assert_idle(self) -> None:
        if self.scanning:
            raise RuntimeError("当前批次正在处理或等待设备释放，请稍后再试")

    def _bind_source(self, source: ImageSource, target_seq: int | None = None) -> ControlledSource:
        if self.session is None:
            raise RuntimeError("请先新建或打开批次")
        bind = getattr(source, "bind_storage", None)
        if callable(bind):
            bind(self.session.batch_dir, target_seq=target_seq)
        return ControlledSource(source)

    def start(self, source: ImageSource) -> None:
        with self._state_lock:
            if self.session is None:
                raise RuntimeError("请先新建或打开批次")
            self._assert_idle()
            self._source = self._bind_source(source)
            self._operation = "scan"
            self._set_state(RUNNING)
            self._cmds.put(("run", (self._source,)))

    def pause(self) -> None:
        # 只在有运行（排队中或进行中）时生效；运行结束与暂停同时发生时，锁保证不会留下“已暂停”
        with self._state_lock:
            if self._source is None or self._operation != "scan" or self.state in (STOPPING, FINISHING, FINALIZED):
                return
            self._source.pause()
            self._set_state(PAUSED)

    def resume(self) -> None:
        with self._state_lock:
            if self._source is None or self._operation != "scan" or self.state in (STOPPING, FINISHING, FINALIZED):
                return
            self._source.resume()
            self._set_state(RUNNING)

    def stop(self) -> None:
        # 与后台的 _finish_operation 互斥：运行恰好结束时不能在它之后把状态改回“停止收尾中”
        with self._state_lock:
            src = self._source
            if src is None:
                return
            src.stop()
            if not self._finalizing:
                self._set_state(STOPPING)

    def remaining_inputs(self) -> int | None:
        """文件导入进行中（含暂停）时还没读入的答卷份数；不是文件导入或无法统计时返回 None。"""
        with self._state_lock:
            src = self._source
            if src is None or self._operation != "scan" or src.mode != "file":
                return None
            count = getattr(src.inner, "sheet_count", None)
            # 按流水线实际登记 / 跳过的份数算：图像源已交出、但暂停或停止时还没登记的那份仍算“没导入”
            done = self._pipeline.consumed if self._pipeline is not None else src.taken
        total = count() if callable(count) else None
        return None if total is None else max(0, total - done)

    def rescan(self, seq: int, files: list[str | Path]) -> None:
        """用文件重扫：若干图片（各面），或一个页数等于模板面数的 PDF。"""
        paths = [Path(f) for f in files]
        if not paths:
            raise RuntimeError("请选择重扫后的图片或 PDF")
        pdfs = [p for p in paths if p.suffix.lower() == ".pdf"]
        with self._state_lock:
            self._assert_idle()
            if self.session is None:
                raise RuntimeError("请先新建或打开批次")
            faces = self.session.faces
        if pdfs:
            if len(paths) != 1:
                raise RuntimeError("重扫只能选一个 PDF，或者只选图片")
            from ..source.pdf import pdf_page_count
            try:
                pages = pdf_page_count(pdfs[0])
            except ValueError as e:
                raise RuntimeError(f"无法读取 PDF {pdfs[0].name}：{e}") from e
            if pages != faces:
                raise RuntimeError(f"重扫 PDF 需要正好 {faces} 页（与模板面数一致），{pdfs[0].name} 有 {pages} 页")

            def sheet(path=pdfs[0]) -> SheetImages:
                from ..source.documents import DocumentSource
                return next(iter(DocumentSource(path, faces).sheets()))
        else:
            sheet = SheetImages(source=paths[0].stem, names=[p.name for p in paths],
                                dpis=[read_dpi(p) for p in paths], paths=paths)
        with self._state_lock:
            self._assert_idle()
            self._operation = "rescan"
            self._set_state(RESCANNING)
            self._cmds.put(("rescan", (seq, sheet)))

    def relocate(self, seq: int, manual: dict, confirm_identity: bool = False) -> None:
        """按操作员点击的参照点重新识别这张卡（不需要重扫）。confirm_identity 见 reprocess。"""
        self._start_rework("relocate", RELOCATING, seq, manual, confirm_identity)

    def reprocess(self, seq: int, confirm_identity: bool = False) -> None:
        """用已保存的原图按自动定位重新识别这张卡（识别中断、内部错误等，原图完好）。

        confirm_identity：混卡经操作员确认无误，跳过标识区检查。"""
        self._start_rework("reprocess", REPROCESSING, seq, confirm_identity)

    def _start_rework(self, cmd: str, state: str, seq: int, *args) -> None:
        with self._state_lock:
            self._assert_idle()
            if self.session is None:
                raise RuntimeError("请先新建或打开批次")
            self._operation = "rescan"
            self._set_state(state)
            self._cmds.put((cmd, (seq, *args)))

    def rescan_scanner(self, seq: int, source: ImageSource) -> None:
        """用扫描仪重扫（source 为单张模式的 TwainSource）。正在扫描时扫描仪被占用，调用方应先结束本轮。"""
        with self._state_lock:
            self._assert_idle()
            self._source = self._bind_source(source, target_seq=seq)
            self._operation = "rescan"
            self._set_state(RESCANNING)
            self._cmds.put(("rescan_scanner", (seq, self._source)))

    def void(self, seq: int) -> None:
        """作废物理异常的卡。识别进行中（含暂停）、重扫或结束批次期间不接受：命令会排在本轮之后，界面看起来没反应。"""
        self._queue_sheet_command("void", seq)

    def unvoid(self, seq: int) -> None:
        """撤销作废：恢复为物理异常。"""
        self._queue_sheet_command("unvoid", seq)

    def _queue_sheet_command(self, cmd: str, seq: int) -> None:
        with self._state_lock:
            if self.session is None:
                raise RuntimeError("请先新建或打开批次")
            self._assert_idle()
            self._cmds.put((cmd, (seq,)))

    def finalize(self) -> None:
        if self._finalizing:
            return
        if self._operation == "rescan" or self.state == STOPPING:
            raise RuntimeError("当前操作尚未结束，请等待设备释放")
        self._finalizing = True
        self.stop()
        self._set_state(FINISHING)
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
        if self.session is not None:
            raise RuntimeError("设备尚未释放或关闭失败，当前批次仍保持占用")
        self._set_state(IDLE)

    def shutdown(self) -> None:
        """退出时调用：关闭批次并取消全部转写（当前与后台），不等待在途请求。"""
        self._progress_timer.stop()
        self._progress_refreshes.clear()
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
