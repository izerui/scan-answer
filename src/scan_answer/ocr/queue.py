"""后台手写转写队列：对 result.json 中 ocr_status 为 pending 的主观题和信息栏逐项转写并回写。

每项转写完成后单独加锁回写（与"结束批次重新判定"互不覆盖）；扫描与识别不等待转写。
"""
from __future__ import annotations

import hashlib
import json
import threading
import uuid
import weakref
from datetime import datetime
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..output.jsonfile import file_lock, read_json, update_json
from ..output.protection import ensure_writable_result
from ..pipeline.events import OcrProgress
from .client import OcrClient, OcrError
from ..template.model import subjective_item_key
from .config import OcrConfig
from .prompts import field_instruction, question_instruction


@dataclass
class _Task:
    path: Path
    kind: str
    key: str
    generation: str
    fingerprint: str
    result_id: str
    owner: OcrQueue | None
    result: Future = field(default_factory=Future)

    @property
    def identity(self) -> tuple[Path, str, str]:
        return self.path, self.kind, self.key


# Lock ordering: queue counters -> registry -> result file. Never notify listeners
# while holding the registry or result lock.
_registry_lock = threading.RLock()
_active: dict[tuple[Path, str, str], _Task] = {}
_queues: weakref.WeakSet = weakref.WeakSet()


class _Unchanged(Exception):
    """Abort update_json without rewriting a stale or already claimed result."""


def _item(result: dict, kind: str, key: str) -> dict | None:
    if kind == "subjective":
        return next((q for q in result.get("subjective", []) if subjective_item_key(q) == key), None)
    if kind == "field":
        return result.get("student", {}).get("fields", {}).get(key)
    return None


def _input(result: dict, path: Path, kind: str, key: str) -> tuple[str, list[bytes], str, OSError | None]:
    entry = _item(result, kind, key)
    if entry is None:
        raise _Unchanged
    if kind == "subjective":
        images = entry["images"]
        instruction = question_instruction(entry.get("content", "zh"))
    else:
        images = [entry["image"]]
        instruction = field_instruction(entry.get("label", key))
    jpegs, digests = [], []
    error = None
    for relative in images:
        try:
            image = (path.parent / relative).read_bytes()
            jpegs.append(image)
            digests.append(hashlib.sha256(image).hexdigest())
        except OSError as exc:
            error = exc
            digests.append(["unreadable", type(exc).__name__, exc.errno])
    directory = path.parent.stat()
    identity = {k: result.get(k) for k in
                ("ocr_result_id", "content_revision", "batch_id", "seq", "sheet_id",
                 "template", "input_pages")}
    identity["scan_time"] = result.get("scan", {}).get("time")
    payload = [str(path), directory.st_dev, directory.st_ino, identity, kind, key,
               images, instruction, digests]
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return fingerprint, jpegs, instruction, error


def _matches(task: _Task, result: dict) -> bool:
    entry = _item(result, task.kind, task.key)
    return bool(entry is not None and result.get("ocr_result_id") == task.result_id
                and entry.get("ocr_generation") == task.generation
                and entry.get("ocr_fingerprint") == task.fingerprint
                and entry.get("ocr_status") == "pending")


def _finish(task: _Task, outcome: tuple[str | None, str]) -> None:
    with _registry_lock:
        if _active.get(task.identity) is task:
            del _active[task.identity]
        if not task.result.done():
            task.result.set_result(outcome)


def _claim(path: Path, kind: str, key: str, owner: OcrQueue | None = None,
           retry: bool = False) -> tuple[_Task | None, bool]:
    ensure_writable_result(path)
    task = None
    created = False

    def claim(result: dict) -> None:
        nonlocal task, created
        entry = _item(result, kind, key)
        if entry is None or (not retry and entry.get("ocr_status") != "pending"):
            raise _Unchanged
        result.setdefault("ocr_result_id", uuid.uuid4().hex)
        fingerprint, _, _, _ = _input(result, path, kind, key)
        previous = _active.get((path, kind, key))
        if previous is not None and _matches(previous, result) and previous.fingerprint == fingerprint:
            task = previous
            raise _Unchanged
        task = _Task(path, kind, key, uuid.uuid4().hex, fingerprint, result["ocr_result_id"], owner)
        entry.update(ocr_generation=task.generation, ocr_fingerprint=fingerprint, ocr_status="pending")
        created = True

    with _registry_lock:
        try:
            update_json(path, claim)
        except _Unchanged:
            return task, False
        if created:
            previous = _active.get(task.identity)
            if previous is not None:
                _finish(previous, (None, "pending"))
            _active[task.identity] = task
        return task, created


def _prepare(task: _Task) -> tuple[list[bytes], str]:
    ensure_writable_result(task.path)
    with _registry_lock, file_lock(task.path):
        result = read_json(task.path)
        if _active.get(task.identity) is not task or not _matches(task, result):
            raise _Unchanged
        fingerprint, images, instruction, error = _input(result, task.path, task.kind, task.key)
        if fingerprint != task.fingerprint:
            raise _Unchanged
        if error is not None:
            raise error
        return images, instruction


def _commit(task: _Task, text: str | None, status: str, model: str | None = None,
            confidence: float | None = None, error: str | None = None) -> bool:
    def apply(result: dict) -> None:
        ensure_writable_result(task.path)
        if not _matches(task, result):
            raise _Unchanged
        fingerprint, _, _, _ = _input(result, task.path, task.kind, task.key)
        if fingerprint != task.fingerprint:
            raise _Unchanged
        _apply(result, task.kind, task.key, text, status, model, confidence, error)

    with _registry_lock:
        if _active.get(task.identity) is not task:
            return False
        try:
            update_json(task.path, apply)
        except _Unchanged:
            return False
        # Release the claim together with the write, so a concurrent retry cannot
        # take over between successful publication and completion of its joiners.
        _finish(task, (text, status))
        return True


def _execute(task: _Task, client: OcrClient) -> tuple[str | None, str]:
    outcome = (None, "pending")
    try:
        images, instruction = _prepare(task)
        transcription = client.transcribe(images, instruction)
        if _commit(task, transcription.text, "done", transcription.model, transcription.confidence):
            outcome = (transcription.text, "done")
    except _Unchanged:
        pass
    except Exception as exc:
        error = str(exc) if isinstance(exc, (OcrError, OSError)) else f"内部错误：{exc}"
        try:
            if _commit(task, None, "failed", error=error):
                outcome = (None, "failed")
        except Exception:
            outcome = (None, "failed")
    finally:
        _finish(task, outcome)
    return task.result.result()


def _cancel(task: _Task) -> None:
    def invalidate(result: dict) -> None:
        if not _matches(task, result):
            raise _Unchanged
        # The pending status is intentionally retained for recovery.
        _item(result, task.kind, task.key)["ocr_generation"] = uuid.uuid4().hex

    with _registry_lock:
        if _active.get(task.identity) is not task:
            return
        try:
            update_json(task.path, invalidate)
        except Exception:
            pass  # Deleted/damaged results remain untouched; memory still fences this task.
        _finish(task, (None, "pending"))


def _within(task: _Task, path: Path | None) -> bool:
    return path is None or task.path == path or path in task.path.parents


def pending_ocr(result_path: str | Path | None = None) -> int:
    """Count owned tasks, including manual retries; optionally restrict to a result or batch."""
    path = Path(result_path).resolve() if result_path is not None else None
    with _registry_lock:
        return sum(_within(task, path) for task in _active.values())


def cancel_ocr(result_path: str | Path) -> None:
    """Invalidate active tasks under a result/batch path without closing unrelated queues."""
    path = Path(result_path).resolve()
    with _registry_lock:
        for task in list(_active.values()):
            if _within(task, path):
                _cancel(task)


def cancel_all_ocr() -> None:
    """Close all queues and invalidate manual retries without waiting for network calls."""
    with _registry_lock:
        queues = list(_queues)
        for task in list(_active.values()):
            _cancel(task)
    for queue in queues:
        queue.close(cancel=True)


def _tasks(result: dict) -> list[tuple[str, str, list[str], str]]:
    """列出待转写项：(类别, 键, 图片相对路径, 提示词)。"""
    out = []
    for q in result.get("subjective", []):
        if q.get("ocr_status") == "pending":
            out.append(("subjective", subjective_item_key(q), q["images"],
                        question_instruction(q.get("content", "zh"))))
    for key, f in result["student"].get("fields", {}).items():
        if f.get("ocr_status") == "pending":
            out.append(("field", key, [f["image"]], field_instruction(f.get("label", key))))
    return out


def _apply(result: dict, kind: str, key: str, text: str | None, status: str, model: str | None,
           confidence: float | None, error: str | None) -> None:
    if kind == "subjective":
        entry = next(q for q in result["subjective"] if subjective_item_key(q) == key)
        entry.update({"text": text, "ocr_status": status, "ocr_model": model, "ocr_confidence": confidence})
        field = f"subjective.{key}"
    else:
        entry = result["student"]["fields"][key]
        entry.update({"text": text, "ocr_status": status})
        field = f"student.fields.{key}"
    _set_failure(result, entry, field, error if status == "failed" else None)


def _set_failure(result: dict, entry: dict, field: str, error: str | None) -> None:
    """失败原因只有一个来源：条目的 ocr_error；OCR_FAILED 警告是它的同文副本（同一 field 只保留一条）。

    error 为 None 时清除原因与警告。放弃转写的标记（ocr_skipped）在重新转写后不再适用，一并清除。"""
    entry.pop("ocr_skipped", None)
    result["warnings"] = [w for w in result.get("warnings", [])
                          if not (w.get("code") == "OCR_FAILED" and w.get("field") == field)]
    if error is None:
        entry.pop("ocr_error", None)
    else:
        entry["ocr_error"] = error
        result["warnings"].append({"code": "OCR_FAILED", "field": field, "msg": f"手写转写失败：{error}"})


def _entries(result: dict):
    """遍历可转写条目：(类别, 键, warnings 中使用的 field, 条目)。"""
    for q in result.get("subjective", []):
        key = subjective_item_key(q)
        yield "subjective", key, f"subjective.{key}", q
    for key, f in (result.get("student") or {}).get("fields", {}).items():
        yield "field", key, f"student.fields.{key}", f


def retry_item(result_path: str | Path, kind: str, key: str, cfg: OcrConfig) -> tuple[str | None, str]:
    """单项重试转写：读切图、调用转写、回写 result.json，返回 (文本, 状态)。

    在后台线程中调用，不依赖 OcrQueue 实例。"""
    try:
        task, created = _claim(Path(result_path).resolve(), kind, key, retry=True)
    except Exception:
        return None, "failed"
    if task is None:
        return None, "failed"
    if not created:
        return task.result.result()
    return _execute(task, OcrClient(cfg))


class OcrQueue:
    def __init__(self, cfg: OcrConfig, listener: Callable[[object], None] | None = None,
                 client: OcrClient | None = None):
        self.client = client or OcrClient(cfg)
        self.listener = listener or (lambda e: None)
        self._pool = ThreadPoolExecutor(cfg.concurrency, thread_name_prefix="ocr")
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._futures: list[Future] = []
        self._tracked: dict[str, Future] = {}
        self.total = self.done = self.failed = 0
        self.cancelled = 0  # failed 中因取消（扫描页继续扫描、退出等）而未完成、仍保留为等待转写的项
        self._snap_seq = 0  # 快照序号
        self._emitted = 0  # 已发出的最大快照序号
        self._closed = False  # close() 后置 True，阻止后续进度通知
        self._accepting = True
        with _registry_lock:
            _queues.add(self)

    def _snapshot(self) -> tuple[int, OcrProgress]:
        """取计数快照（调用方须持有 self._lock）。"""
        self._snap_seq += 1
        return self._snap_seq, OcrProgress(self.done, self.failed, self.total)

    def _emit(self, snap: tuple[int, OcrProgress]) -> None:
        """在计数锁外发出进度；比已发出的更旧的快照直接丢弃，保证计数单调不减。"""
        seq, ev = snap
        with self._emit_lock:
            # 在 emit 锁内复查 _closed：close() 置位后再排空该锁，之后不会再有任何通知
            if self._closed or seq <= self._emitted:
                return
            self._emitted = seq
            self.listener(ev)

    def _run(self, task: _Task, execute: bool = True) -> None:
        _, status = _execute(task, self.client) if execute else task.result.result()
        # 队列已关闭时跳过计数和通知，避免在途任务写回旧 session 的 manifest
        if self._closed:
            return
        with self._lock:
            self.done += int(status == "done")
            self.failed += int(status != "done")
            self.cancelled += int(status == "pending")
            snap = self._snapshot()
        self._emit(snap)

    def _track(self, task: _Task, created: bool) -> bool:
        """Attach one owned/shared generation; caller holds the queue lock."""
        if task.generation in self._tracked:
            return False
        self._tracked = {key: future for key, future in self._tracked.items() if not future.done()}
        self._futures[:] = [future for future in self._futures if not future.done()]
        try:
            future = self._pool.submit(self._run, task, created)
        except Exception:
            if created:
                _cancel(task)
            raise
        self._tracked[task.generation] = future
        self._futures.append(future)
        self.total += 1
        return True

    def enqueue_item(self, result_path: str | Path, kind: str, key: str, retry: bool = True) -> int:
        """Synchronously claim/join an item before starting a background wait.

        Return 1 for a new claim, 0 for a join, missing item, closed queue, or
        non-pending item with retry=False. Joined work is counted once locally
        and included in wait()/close(), without another client call or global
        pending count. Cancelling this queue only invalidates its own claims.
        """
        result_path = Path(result_path).resolve()
        with self._lock:
            if not self._accepting:
                return 0
            task, created = _claim(result_path, kind, key, self, retry=retry)
            if task is None or not self._track(task, created):
                return 0
            snap = self._snapshot()
        self._emit(snap)
        return int(created)

    def enqueue(self, result_path: str | Path) -> int:
        """投递一个学生的 result.json，返回新增的待转写项数。"""
        result_path = Path(result_path).resolve()
        tasks = _tasks(read_json(result_path))
        added = 0
        with self._lock:
            if not self._accepting:
                return 0
            self._futures[:] = [future for future in self._futures if not future.done()]
            for kind, key, _, _ in tasks:
                task, created = _claim(result_path, kind, key, self)
                if not created:
                    continue
                self._track(task, created)
                added += 1
            snap = self._snapshot()
        if added:
            self._emit(snap)
        return added

    def wait(self) -> None:
        """等待已投递的全部转写完成（不抛出任务异常）。"""
        with self._lock:
            futures = list(self._futures)
        # shutdown(cancel_futures=True) can leave a future in CANCELLED rather
        # than CANCELLED_AND_NOTIFIED; Future.result also handles that state.
        for future in futures:
            try:
                future.result()
            except Exception:
                pass

    @property
    def pending(self) -> int:
        """尚未完成的转写项数；已关闭的队列返回 0（其计数不再更新）。"""
        with self._lock:
            return 0 if self._closed else self.total - self.done - self.failed

    @property
    def idle(self) -> bool:
        """已投递的任务全部结束（含回写与进度通知）。"""
        with self._lock:
            return all(f.done() for f in self._futures)

    def detach(self, listener: Callable[[object], None]) -> None:
        """转入后台：换掉监听器，不再接受新投递，已投递的项继续转完（不等待）。

        监听器在 emit 锁内替换：替换返回后，旧监听器不会再被调用。"""
        with self._emit_lock:
            self.listener = listener
        with self._lock:
            self._accepting = False
        self._pool.shutdown(wait=False)

    def close(self, cancel: bool = False) -> None:
        with self._lock:
            self._accepting = False
            if cancel:
                self._closed = True
        if cancel:
            with _registry_lock:
                for task in list(_active.values()):
                    if task.owner is self:
                        _cancel(task)
        # Graceful shutdown keeps progress live until all workers and listeners finish.
        self._pool.shutdown(wait=not cancel, cancel_futures=cancel)
        with self._emit_lock:
            self._closed = True


def reset_failed(result_path: str | Path) -> int:
    """把 failed 的转写项改回 pending（命令行 `ocr --retry-failed` 用），返回改动项数。"""
    ensure_writable_result(result_path)
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for _kind, _key, _field, entry in _entries(r):
            if entry.get("ocr_status") == "failed":
                entry["ocr_status"] = "pending"
                entry.pop("ocr_error", None)
                count += 1
        r["warnings"] = [w for w in r["warnings"] if w.get("code") != "OCR_FAILED"]

    update_json(result_path, fix)
    return count


def enable_ocr(result_path: str | Path) -> int:
    """把 ocr_status 为 none 的项置为 pending（批次识别时未配置转写，事后补做），返回改动项数。"""
    ensure_writable_result(result_path)
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for _kind, _key, _field, entry in _entries(r):
            if entry.get("ocr_status") == "none":
                entry["ocr_status"] = "pending"
                entry.pop("ocr_skipped", None)  # 补做后不再是“已放弃”
                count += 1

    update_json(result_path, fix)
    return count


def skip_pending(result_path: str | Path, reason: str = "操作员放弃转写") -> tuple[int, int]:
    """把“等待转写”的项标为未启用（none）并记录 ocr_skipped，使交付不再被阻断；返回 (改动项数, 正在转写而未改动的项数)。

    正在由某个队列转写的项不改动（它很快会变成完成或失败）；之后仍可用 enable_ocr 补做。"""
    path = Path(result_path).resolve()
    ensure_writable_result(path)
    skipped = busy = 0
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")

    def fix(r: dict) -> None:
        nonlocal skipped, busy
        skipped = busy = 0
        for kind, key, _field, entry in _entries(r):
            if entry.get("ocr_status") != "pending":
                continue
            if (path, kind, key) in _active:
                busy += 1
                continue
            entry["ocr_status"] = "none"
            entry["ocr_generation"] = uuid.uuid4().hex  # 作废可能残留的旧请求
            entry["ocr_skipped"] = {"time": stamp, "reason": reason}
            skipped += 1
        if not skipped:
            raise _Unchanged

    with _registry_lock:
        try:
            update_json(path, fix)
        except _Unchanged:
            pass
    return skipped, busy


def fail_unclaimed(result_path: str | Path, error: str) -> int:
    """投递中途出错时，把没有任何队列认领的“等待转写”项改回失败（带原因），避免留下无人处理的等待项。返回改动项数。"""
    path = Path(result_path).resolve()
    ensure_writable_result(path)
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        count = 0
        for kind, key, where, entry in _entries(r):
            if entry.get("ocr_status") == "pending" and (path, kind, key) not in _active:
                entry["ocr_status"] = "failed"
                _set_failure(r, entry, where, error)
                count += 1
        if not count:
            raise _Unchanged

    with _registry_lock:
        try:
            update_json(path, fix)
        except _Unchanged:
            pass
    return count
