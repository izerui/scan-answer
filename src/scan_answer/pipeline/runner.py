"""流水线：图像源 → 保存原图 → 识别进程池 → 记录结果 → 发事件；结束批次时按最终阈值重新判定。

扫描与识别并行：主线程从图像源取卡、提交给进程池，同时收集已完成的结果；
在途任务数不超过 workers*2，防止扫描远快于识别时内存堆积。workers=0 时在当前进程内顺序执行（测试和调试用）。
所有事件都在调用 run()/rescan()/finalize() 的线程里发出。
"""
from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Callable

from ..source.base import DeviceEvent, ImageSource, SheetImages
from .events import DeviceNotice, FinalizedEvent, SheetEvent, ThresholdEvent, VoidEvent
from .session import BatchSession
from .worker import FinalizeJob, SheetJob, SheetOutcome, finalize_sheet, process_sheet

Listener = Callable[[object], None]


class _InlineExecutor(Executor):
    """在当前线程立即执行的执行器，接口与进程池一致。"""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 — 交给调用方的 result() 抛出
            f.set_exception(e)
        return f


class BatchPipeline:
    def __init__(self, session: BatchSession, *, workers: int = 0, listener: Listener | None = None,
                 on_result: Callable[[str], None] | None = None):
        """on_result：每张卡成功写出 result.json 后回调其绝对路径（用于投递手写转写）。"""
        self.session = session
        self.workers = workers
        self.listener = listener or (lambda e: None)
        self.on_result = on_result
        self._gen = 0  # 执行器代数：每重建一次加 1
        self._executor: Executor = self._make_executor()

    # ---------- 生命周期 ----------
    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def __enter__(self) -> "BatchPipeline":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 内部 ----------
    def _make_executor(self) -> Executor:
        if self.workers > 0:
            return ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"))
        return _InlineExecutor()

    def _rebuild(self, gen: int) -> None:
        """进程池损坏（某个识别进程异常退出）时重建；同一代只重建一次。"""
        if gen != self._gen:
            return
        old = self._executor
        self._executor = self._make_executor()
        self._gen += 1
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 — 已损坏的池关闭失败不影响后续
            pass

    def _submit(self, fn, job) -> tuple[Future, int]:
        """提交任务，返回 (future, 提交时的执行器代数)；进程池已损坏时重建后再提交一次。"""
        err: BaseException | None = None
        for _ in range(2):
            gen = self._gen
            try:
                return self._executor.submit(fn, job), gen
            except BrokenProcessPool as e:
                err = e
                self._rebuild(gen)
        f: Future = Future()
        f.set_exception(err)
        return f, self._gen

    def _result(self, f: Future, gen: int, fn, job):
        """取结果；进程池损坏导致失败时重建并重提交一次，再失败则抛出。"""
        try:
            return f.result()
        except BrokenProcessPool:
            self._rebuild(gen)
        f2, gen2 = self._submit(fn, job)
        try:
            return f2.result()
        except BrokenProcessPool:
            self._rebuild(gen2)  # 保证后续任务有可用的进程池
            raise

    def _outcome(self, f: Future, gen: int, job: SheetJob) -> SheetOutcome:
        """安全地取结果；worker 异常时转为物理异常记录，不中断批次。"""
        try:
            return self._result(f, gen, process_sheet, job)
        except Exception as e:  # noqa: BLE001 — worker 崩溃不应中断整批
            return SheetOutcome(job.seq, False, physical="ALIGN_FAILED",
                                message=f"内部错误：{type(e).__name__}: {e}")

    def _job(self, seq: int, sheet: SheetImages) -> SheetJob:
        s = self.session
        raw = s.save_raw(seq, sheet)
        m = s.manifest
        return SheetJob(seq=seq, raw_paths=[str(p) for p in raw], dpis=list(sheet.dpis),
                        mark=s.thresholds.mark, uncertain=s.thresholds.uncertain,
                        batch_dir=str(s.batch_dir), template_dir=str(s.template_dir), batch_id=s.batch_id,
                        exam=m["exam"], scanner=m["scanner"], ocr=m["ocr"])

    def _collect(self, out: SheetOutcome, rescan: bool = False) -> SheetEvent:
        refit = self.session.record(out)
        item = self.session.sheets[out.seq]
        ev = SheetEvent(out.seq, item["source"], out.ok, out.physical, out.message, out.exam_no,
                        out.warnings, rescan)
        self.listener(ev)
        if out.ok and self.on_result:
            self.on_result(str(self.session.batch_dir / out.result))
        if refit:
            self.listener(ThresholdEvent(refit.mark, refit.uncertain, len(self.session.fills)))
        return ev

    # ---------- 对外 ----------
    def run(self, source: ImageSource, stop: threading.Event | None = None) -> None:
        """处理图像源中的全部卡，直到取完或 stop 被置位。返回前等待所有在途任务完成。"""
        pending: dict[Future, tuple[int, SheetJob]] = {}
        window = max(1, self.workers * 2)
        try:
            for item in source.sheets():
                if stop is not None and stop.is_set():
                    break
                if isinstance(item, DeviceEvent):
                    self.session.add_event(item.kind, item.message)
                    self.listener(DeviceNotice(item.kind, item.message, self.session.next_seq() - 1))
                    continue
                seq = self.session.next_seq()
                self.session.register(seq, item)
                job = self._job(seq, item)
                f, gen = self._submit(process_sheet, job)
                pending[f] = (gen, job)
                if len(pending) >= window:
                    done, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for f in done:
                        gen, job = pending.pop(f)
                        self._collect(self._outcome(f, gen, job))
            for f, (gen, job) in list(pending.items()):
                self._collect(self._outcome(f, gen, job))
        finally:
            self.session.save(force=True)

    def rescan(self, seq: int, sheet: SheetImages) -> SheetEvent:
        """用新图像替换某个序号的卡（物理异常重扫）；序号不变。"""
        self.session.prepare_rescan(seq)
        self.session.register(seq, sheet)
        job = self._job(seq, sheet)
        f, gen = self._submit(process_sheet, job)
        ev = self._collect(self._outcome(f, gen, job), rescan=True)
        self.session.save(force=True)
        return ev

    def void(self, seq: int) -> None:
        self.session.void(seq)
        self.listener(VoidEvent(seq))

    def finalize(self) -> FinalizedEvent:
        """结束批次：用整批涂黑度拟合最终阈值，逐张重新判定；有变化的卡重写 result.json。"""
        s = self.session
        th = s.final_thresholds()
        jobs = [FinalizeJob(seq, th.mark, th.uncertain, str(s.batch_dir), str(s.template_dir))
                for seq, item in sorted(s.sheets.items()) if item.get("status") == "ok"]
        changed = 0
        futures = [(j, *self._submit(finalize_sheet, j)) for j in jobs]
        for j, f, gen in futures:
            try:
                r = self._result(f, gen, finalize_sheet, j)
            except Exception as e:  # noqa: BLE001 — 单张失败不中断整批结束
                s.add_event("error", f"重新判定失败 #{j.seq}: {type(e).__name__}: {e}")
                continue
            item = s.sheets[r.seq]
            item["warnings"] = r.warnings
            item["exam_no"] = r.exam_no
            changed += int(r.changed)
        s.mark_finalized(th)
        ev = FinalizedEvent(th.mark, th.uncertain, changed)
        self.listener(ev)
        return ev
