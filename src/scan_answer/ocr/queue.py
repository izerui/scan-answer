"""后台手写转写队列：对 result.json 中 ocr_status 为 pending 的主观题和信息栏逐项转写并回写。

每项转写完成后单独加锁回写（与"结束批次重新判定"互不覆盖）；扫描与识别不等待转写。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait as cf_wait
from pathlib import Path
from typing import Callable

from ..output.jsonfile import read_json, update_json
from ..pipeline.events import OcrProgress
from .client import OcrClient, OcrError
from .config import OcrConfig
from .prompts import field_instruction, question_instruction


def _tasks(result: dict) -> list[tuple[str, str, list[str], str]]:
    """列出待转写项：(类别, 键, 图片相对路径, 提示词)。"""
    out = []
    for q in result.get("subjective", []):
        if q.get("ocr_status") == "pending":
            out.append(("subjective", q["no"], q["images"], question_instruction(q.get("content", "zh"))))
    for key, f in result["student"].get("fields", {}).items():
        if f.get("ocr_status") == "pending":
            out.append(("field", key, [f["image"]], field_instruction(f.get("label", key))))
    return out


def _apply(result: dict, kind: str, key: str, text: str | None, status: str, model: str | None,
           confidence: float | None, error: str | None) -> None:
    if kind == "subjective":
        q = next(q for q in result["subjective"] if q["no"] == key)
        q.update({"text": text, "ocr_status": status, "ocr_model": model, "ocr_confidence": confidence})
        field = f"subjective.{key}"
    else:
        f = result["student"]["fields"][key]
        f.update({"text": text, "ocr_status": status})
        field = f"student.fields.{key}"
    result["warnings"] = [w for w in result["warnings"]
                          if not (w.get("code") == "OCR_FAILED" and w.get("field") == field)]
    if status == "failed":
        result["warnings"].append({"code": "OCR_FAILED", "field": field, "msg": f"手写转写失败：{error}"})


class OcrQueue:
    def __init__(self, cfg: OcrConfig, listener: Callable[[object], None] | None = None,
                 client: OcrClient | None = None):
        self.client = client or OcrClient(cfg)
        self.listener = listener or (lambda e: None)
        self._pool = ThreadPoolExecutor(cfg.concurrency, thread_name_prefix="ocr")
        self._lock = threading.Lock()
        self._emit_lock = threading.Lock()
        self._futures: list[Future] = []
        self.total = self.done = self.failed = 0
        self._snap_seq = 0  # 快照序号
        self._emitted = 0  # 已发出的最大快照序号

    def _snapshot(self) -> tuple[int, OcrProgress]:
        """取计数快照（调用方须持有 self._lock）。"""
        self._snap_seq += 1
        return self._snap_seq, OcrProgress(self.done, self.failed, self.total)

    def _emit(self, snap: tuple[int, OcrProgress]) -> None:
        """在计数锁外发出进度；比已发出的更旧的快照直接丢弃，保证计数单调不减。"""
        seq, ev = snap
        with self._emit_lock:
            if seq <= self._emitted:
                return
            self._emitted = seq
            self.listener(ev)

    def _run(self, result_path: Path, kind: str, key: str, images: list[str], instruction: str) -> None:
        ok = False
        try:
            jpegs = [(result_path.parent / rel).read_bytes() for rel in images]
            t = self.client.transcribe(jpegs, instruction)
            update_json(result_path, lambda r: _apply(r, kind, key, t.text, "done", t.model, t.confidence, None))
            ok = True
        except (OcrError, OSError) as e:
            self._write_failed(result_path, kind, key, str(e))
        except Exception as e:
            # 单项内部错误不中断批次
            self._write_failed(result_path, kind, key, f"内部错误：{e}")
        finally:
            # 每项恰好计数一次，回写失败也不例外
            with self._lock:
                self.done += int(ok)
                self.failed += int(not ok)
                snap = self._snapshot()
            self._emit(snap)

    @staticmethod
    def _write_failed(result_path: Path, kind: str, key: str, error: str) -> None:
        try:
            update_json(result_path, lambda r: _apply(r, kind, key, None, "failed", None, None, error))
        except Exception:
            pass  # result.json 已被删除（如重扫）或本身损坏，放弃回写

    def enqueue(self, result_path: str | Path) -> int:
        """投递一个学生的 result.json，返回新增的待转写项数。"""
        result_path = Path(result_path)
        tasks = _tasks(read_json(result_path))
        with self._lock:
            self.total += len(tasks)
            for kind, key, images, instruction in tasks:
                self._futures.append(self._pool.submit(self._run, result_path, kind, key, images, instruction))
            snap = self._snapshot()
        if tasks:
            self._emit(snap)
        return len(tasks)

    def wait(self) -> None:
        """等待已投递的全部转写完成（不抛出任务异常）。"""
        with self._lock:
            futures = list(self._futures)
        if futures:
            cf_wait(futures)

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def reset_failed(result_path: str | Path) -> int:
    """把 failed 的转写项改回 pending（命令行 `ocr --retry-failed` 用），返回改动项数。"""
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for q in r.get("subjective", []):
            if q.get("ocr_status") == "failed":
                q["ocr_status"] = "pending"
                count += 1
        for f in r["student"].get("fields", {}).values():
            if f.get("ocr_status") == "failed":
                f["ocr_status"] = "pending"
                count += 1
        r["warnings"] = [w for w in r["warnings"] if w.get("code") != "OCR_FAILED"]

    update_json(result_path, fix)
    return count


def enable_ocr(result_path: str | Path) -> int:
    """把 ocr_status 为 none 的项置为 pending（批次识别时未配置转写，事后补做），返回改动项数。"""
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for q in r.get("subjective", []):
            if q.get("ocr_status") == "none":
                q["ocr_status"] = "pending"
                count += 1
        for f in r["student"].get("fields", {}).values():
            if f.get("ocr_status") == "none":
                f["ocr_status"] = "pending"
                count += 1

    update_json(result_path, fix)
    return count
