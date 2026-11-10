"""流水线：图像源 → 保存原图 → 识别进程池 → 记录结果 → 发事件；结束批次时按最终阈值重新判定。

扫描与识别并行：主线程从图像源取卡、提交给进程池，同时收集已完成的结果；
在途任务数不超过 workers*2，防止扫描远快于识别时内存堆积。workers=0 时在当前进程内顺序执行（测试和调试用）。
所有事件都在调用 run()/rescan()/finalize() 的线程里发出。
"""
from __future__ import annotations

import multiprocessing
import queue
import threading
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from typing import Callable

from ..source.base import DeviceEvent, ImageSource, SheetImages, SkippedSheet
from ..source.folder import read_dpi
from .events import (DeviceNotice, FinalizedEvent, SheetEvent, SheetQueued, SheetSkipped, ThresholdEvent, UnvoidEvent,
                     VoidEvent)
from .session import BatchSession, input_keys
from .worker import FinalizeJob, SheetJob, SheetOutcome, finalize_sheet, process_sheet

Listener = Callable[[object], None]


class _InputPump:
    """Bounded reads with acknowledgment, so device notices gate the next round."""

    def __init__(self, source: ImageSource):
        self.source = source
        self.items: queue.Queue = queue.Queue(maxsize=1)
        self.cancel = threading.Event()
        self.thread = threading.Thread(target=self._read, name="scan-input", daemon=True)

    def _offer(self, kind: str, value) -> bool:
        ack = threading.Event()
        while not self.cancel.is_set():
            try:
                self.items.put((kind, value, ack), timeout=0.05)
                while kind == "item" and not self.cancel.is_set():
                    if ack.wait(0.05):
                        break
                return not self.cancel.is_set()
            except queue.Full:
                continue
        return False

    def _read(self) -> None:
        iterator = None
        try:
            iterator = iter(self.source.sheets())
            for item in iterator:
                if not self._offer("item", item):
                    break
        except Exception as error:
            self._offer("error", error)
        finally:
            try:
                close = getattr(iterator, "close", None)
                if callable(close):
                    close()
            except Exception as error:
                self._offer("error", error)
            self._offer("end", None)

    def close(self) -> None:
        self.cancel.set()
        stop = getattr(self.source, "stop", None)
        if self.thread.is_alive() and callable(stop):
            stop()
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("输入读取尚未结束，请等待设备释放")


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
        self._running = threading.Event()
        self.consumed = 0  # 本轮 run() 已登记或跳过的答卷份数（统计“还剩多少没导入”用）
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

    def _job(self, seq: int, sheet: SheetImages, session: BatchSession | None = None) -> SheetJob:
        s = session or self.session
        raw = s.save_raw(seq, sheet)
        m = s.manifest
        return SheetJob(seq=seq, raw_paths=[str(p) for p in raw], dpis=list(sheet.dpis),
                        mark=s.thresholds.mark, uncertain=s.thresholds.uncertain,
                        batch_dir=str(s.batch_dir), template_dir=str(s.template_dir), batch_id=s.batch_id,
                        exam=m["exam"], scanner=m["scanner"], ocr=m["ocr"],
                        student_dir_pattern=m.get("student_dir_pattern", "{exam_no}"),
                        input_pages=s.sheets[seq].get("input_pages", []))

    def _collect(self, out: SheetOutcome, rescan: bool = False, *, record: bool = True) -> SheetEvent:
        refit = self.session.record(out) if record else None
        item = self.session.sheets[out.seq]
        ev = SheetEvent(out.seq, item["source"], out.ok, out.physical, out.message, out.exam_no,
                        out.warnings, rescan, str(self.session.batch_dir / out.result) if out.ok else None)
        self.listener(ev)
        if out.ok and self.on_result:
            self.on_result(str(self.session.batch_dir / out.result))
        if refit:
            self.listener(ThresholdEvent(refit.mark, refit.uncertain, len(self.session.fills),
                                         self.session.threshold_issues))
        return ev

    # ---------- 对外 ----------
    def run(self, source: ImageSource, stop: threading.Event | None = None) -> None:
        """处理图像源中的全部卡，直到取完或 stop 被置位。返回前等待所有在途任务完成。"""
        self.session.assert_writable()
        self._running.set()
        pending: dict[Future, tuple[int, SheetJob]] = {}
        window = max(1, self.workers * 2)
        # 已登记的输入：中途出错或结束本轮后再点“开始识别”时，已导入的答卷不重复登记。
        # 整份（各页及顺序）完全相同才跳过；页面部分重叠但配组不同（例如改正串卷后重新导入）照常登记并提示
        self.consumed = 0
        known = self.session.registered_inputs()
        known_sheets = self.session.registered_sheets()
        skip_registered = getattr(source, "skip_registered", None)
        if callable(skip_registered):
            skip_registered(set(known_sheets))  # PDF 中已登记的页不再渲染
        pump = _InputPump(source)
        pump.thread.start()
        try:
            while True:
                for future in [f for f in pending if f.done()]:
                    gen, job = pending.pop(future)
                    self._collect(self._outcome(future, gen, job))
                if len(pending) >= window:
                    wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                    continue
                try:
                    kind, item, ack = pump.items.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if kind == "end":
                        break
                    if kind == "error":
                        raise item
                    if stop is not None and stop.is_set():
                        request_stop = getattr(source, "stop", None)
                        if callable(request_stop):
                            request_stop()
                        if not getattr(source, "drain_on_stop", False):
                            break
                    if isinstance(item, DeviceEvent):
                        self.session.add_event(item.kind, item.message)
                        self.listener(DeviceNotice(item.kind, item.message, self.session.next_seq() - 1))
                        continue
                    if isinstance(item, SkippedSheet):
                        self.consumed += 1
                        self.listener(SheetSkipped(item.source, known_sheets.get(item.keys, 0)))
                        continue
                    keys = input_keys(item)
                    if keys and tuple(keys) in known_sheets:
                        self.consumed += 1
                        self.listener(SheetSkipped(item.source, known_sheets[tuple(keys)]))
                        continue
                    seq = self.session.next_seq()
                    self.session.register(seq, item, keys)
                    self.consumed += 1
                    if keys:
                        overlap = sorted({known[k] for k in keys if k in known})
                        known_sheets.setdefault(tuple(keys), seq)
                        for k in keys:
                            known.setdefault(k, seq)
                        if overlap:
                            message = (f"#{seq:04d} 的页面已出现在 " + "、".join(f"#{o:04d}" for o in overlap)
                                       + " 中（配组不同），已作为新答卷登记；请核对并作废配错的旧答卷")
                            self.session.add_event("input_overlap", message)
                            self.listener(DeviceNotice("input_overlap", message, seq))
                    self.listener(SheetQueued(seq, item.source))
                    job = self._job(seq, item)
                    f, gen = self._submit(process_sheet, job)
                    pending[f] = (gen, job)
                finally:
                    ack.set()
        finally:
            try:
                # 后续 PDF 读坏或用户停止时，已提交的卡也必须完成登记。
                for f, (gen, job) in list(pending.items()):
                    self._collect(self._outcome(f, gen, job))
            finally:
                try:
                    pump.close()
                finally:
                    try:
                        self.session.save(force=True)
                    finally:
                        self._running.clear()

    def _restore_event(self, seq: int, message: str) -> SheetEvent:
        """重扫类操作没换掉旧记录时，按旧记录重发一次完成事件（界面据此把“识别中”格子恢复原状）。"""
        old = self.session.sheets[seq]
        event = SheetEvent(seq, old["source"], old.get("status") == "ok", old.get("physical"), message,
                           old.get("exam_no"), old.get("warnings", 0), True)
        self.listener(event)
        return event

    def rescan(self, seq: int, sheet: SheetImages, manual: dict | None = None, *,
               action: str | None = None, confirm_identity: bool = False) -> SheetEvent:
        """用新图像替换某个序号的卡（物理异常重扫）；序号不变。manual 见 relocate。"""
        from .attempts import RescanAttempt

        action = action or ("手动定位" if manual else "重扫")
        kind = {"手动定位": "relocate_failed", "重新识别": "reprocess_failed"}.get(action, "rescan_failed")
        queued = False
        try:
            with RescanAttempt(self.session, seq) as attempt:
                attempt.session.register(seq, sheet)
                job = self._job(seq, sheet, attempt.session)
                job.manual = manual
                job.confirm_identity = confirm_identity
                self.listener(SheetQueued(seq, sheet.source))
                queued = True
                f, gen = self._submit(process_sheet, job)
                outcome = self._outcome(f, gen, job)
                if outcome.ok:
                    before = self.session.thresholds
                    attempt.commit(outcome)
                    event = self._collect(outcome, rescan=True, record=False)
                    after = self.session.thresholds
                    if before != after:
                        self.listener(ThresholdEvent(after.mark, after.uncertain, len(self.session.fills),
                                                     self.session.threshold_issues))
                    return event
                attempt.reject(outcome)
        except Exception as error:
            if queued and seq in self.session.sheets:
                # 提交失败（已回滚）：格子不能停在“识别中”
                self._restore_event(seq, f"{action}失败，旧证据已保留：{type(error).__name__}: {error}")
            raise
        self.session.add_event(kind, f"#{seq:04d} {action}失败，旧证据已保留：{outcome.message}")
        return self._restore_event(seq, f"{action}失败，旧证据已保留：{outcome.message}")

    def _sheet_from_raw(self, seq: int, action: str) -> SheetImages:
        """用这张卡已保存在 raw/ 的原图组装输入；原图记录不完整或文件缺失时抛出。"""
        s = self.session
        item = s.sheets[seq]
        pages = item.get("input_pages") or []
        if not pages or len(pages) != len(item.get("files", [])):
            raise ValueError(f"#{seq:04d} 的原图记录不完整，无法{action}，请重扫")
        paths = [s.batch_dir / o["raw"] for o in pages]
        missing = [p.name for p in paths if not p.is_file()]
        if missing:
            raise FileNotFoundError(f"#{seq:04d} 的原图缺失：{'、'.join(missing)}")
        documents = {o["document_id"]: s.batch_dir / o["document"] for o in pages
                     if o.get("document_id") and o.get("document") and (s.batch_dir / o["document"]).is_file()}
        return SheetImages(source=item["source"], names=list(item["files"]), dpis=[read_dpi(p) for p in paths],
                           paths=paths, origins=[{k: v for k, v in o.items() if k not in ("raw", "document")}
                                                 for o in pages], documents=documents)

    def relocate(self, seq: int, manual: dict, confirm_identity: bool = False) -> SheetEvent:
        """用这张卡已保存的原图、按操作员点击的参照点重新识别（自动定位失败时的人工补救）。

        与重扫走同一套暂存/提交流程：成功才替换旧结果，失败保留旧证据。
        """
        return self.rescan(seq, self._sheet_from_raw(seq, "手动定位"), manual=manual,
                           confirm_identity=confirm_identity)

    def reprocess(self, seq: int, confirm_identity: bool = False) -> SheetEvent:
        """用已保存的原图按自动定位重新识别（识别中断、内部错误等原图完好的卡）；失败保留原记录。

        confirm_identity：混卡（WRONG_CARD）经操作员核对原图确认无误后，跳过标识区检查重新识别。"""
        return self.rescan(seq, self._sheet_from_raw(seq, "重新识别"), action="重新识别",
                           confirm_identity=confirm_identity)

    def void(self, seq: int) -> None:
        self.session.void(seq)
        self.listener(VoidEvent(seq))

    def unvoid(self, seq: int) -> None:
        self.session.unvoid(seq)
        self.listener(UnvoidEvent(seq))

    def finalize(self) -> FinalizedEvent:
        """结束批次：用整批涂黑度拟合最终阈值，逐张重新判定；有变化的卡重写 result.json。"""
        s = self.session
        s.assert_writable()
        with s.lock:
            revision = s.manifest["revision"]
            s.manifest["finalized"] = False
            blocked = sorted(seq for seq, item in s.sheets.items() if item.get("status") not in ("ok", "voided"))
            eligible = [seq for seq, item in sorted(s.sheets.items()) if item.get("status") == "ok"]
            s.manifest["finalization"] = {"status": "running", "blocked_seqs": blocked, "failed_seqs": []}
            s.save(force=True)
        if self._running.is_set() or not s.sheets:
            message = "采集仍在进行，请先结束本轮" if self._running.is_set() else "空批次不能定稿"
            s.manifest["finalization"].update(status="blocked", reason=message)
            s.save(force=True)
            event = FinalizedEvent(s.thresholds.mark, s.thresholds.uncertain, 0,
                                   issues=[{"code": "NOT_SETTLED", "msg": message}],
                                   completed=False, blocked_seqs=blocked)
            self.listener(event)
            return event
        th = s.final_thresholds()
        jobs = [FinalizeJob(seq, th.mark, th.uncertain, str(s.batch_dir), str(s.template_dir))
                for seq in eligible]
        changed: list[int] = []
        failed: list[int] = []
        futures = [(j, *self._submit(finalize_sheet, j)) for j in jobs]
        for j, f, gen in futures:
            try:
                r = self._result(f, gen, finalize_sheet, j)
                if r.seq != j.seq:
                    raise ValueError("重判返回的答卷序号不一致")
            except Exception as e:  # noqa: BLE001 — 单张失败不中断整批结束
                failed.append(j.seq)
                with s.lock:
                    if s.manifest["revision"] == revision:
                        s.sheets[j.seq]["finalize_error"] = f"{type(e).__name__}: {e}"
                s.add_event("error", f"重新判定失败 #{j.seq}: {type(e).__name__}: {e}")
                continue
            with s.lock:
                if s.manifest["revision"] != revision:
                    continue
                item = s.sheets[r.seq]
                item.pop("finalize_error", None)
                item["warnings"] = r.warnings
                item["exam_no"] = r.exam_no
                result_ref = item.get("result")
            if r.changed:
                changed.append(r.seq)
                if self.on_result and result_ref:
                    try:
                        self.on_result(str(s.batch_dir / result_ref))
                    except Exception as error:
                        s.add_event("error", f"转写任务重新投递失败 #{r.seq}：{error}")
        with s.lock:
            stale = s.manifest["revision"] != revision
            completed = not blocked and not failed and not stale
            if stale:
                # A newer mutation or settlement owns the current metadata.
                s.save(force=True)
            else:
                s.manifest["finalization"] = {
                    "status": "complete" if completed else "failed" if failed else "blocked",
                    "blocked_seqs": blocked, "failed_seqs": failed, "revision": revision,
                }
                if completed:
                    s.mark_finalized(th, expected_revision=revision)
                else:
                    s.thresholds = th
                    s.save(force=True)
        issues = s.threshold_issues
        if stale:
            issues.append({"code": "BATCH_CHANGED", "msg": "批次内容已改变，请重新结算"})
        if blocked:
            issues.append({"code": "UNRESOLVED_SHEETS", "msg": f"还有 {len(blocked)} 份答卷未处置：{blocked}"})
        if failed:
            issues.append({"code": "REDECISION_FAILED", "msg": f"有 {len(failed)} 份重判失败，可重试：{failed}"})
        ev = FinalizedEvent(th.mark, th.uncertain, len(changed), changed, issues, completed, blocked, failed)
        self.listener(ev)
        return ev
