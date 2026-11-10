"""可暂停、可停止的图像源包装。

界面的"暂停 / 继续"要从上次的位置接着取卡，所以包装器只创建一次底层迭代器，
文件源暂停时阻塞在取下一张之前，停止时结束迭代；扫描源由设备控制轮次，暂停和停止时排空已接收页。
供流水线所在的后台线程使用，暂停/继续/停止可在任意线程调用。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Iterator

from .base import ImageSource, SourceItem


class ControlledSource:
    def __init__(self, inner: ImageSource):
        self.inner = inner
        self._it: Iterator[SourceItem] = iter(inner.sheets())
        self._running = threading.Event()
        self._running.set()
        self._stopped = threading.Event()
        self.taken = 0  # 已交给流水线的条目数

    # ---------- 控制（任意线程） ----------
    def pause(self) -> None:
        self._running.clear()
        inner_pause = getattr(self.inner, "pause", None)
        if callable(inner_pause):
            inner_pause()

    def resume(self) -> None:
        inner_resume = getattr(self.inner, "resume", None)
        if callable(inner_resume):
            inner_resume()
        self._running.set()

    def stop(self) -> None:
        self._stopped.set()
        self._running.set()  # 唤醒可能在等待的迭代
        # 底层图像源可能阻塞在等设备（扫描仪）：有 stop() 的一并通知
        inner_stop = getattr(self.inner, "stop", None)
        if callable(inner_stop):
            inner_stop()

    @property
    def paused(self) -> bool:
        return not self._running.is_set() and not self._stopped.is_set()

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def mode(self) -> str:
        return getattr(self.inner, "mode", "file")

    @property
    def drain_on_stop(self) -> bool:
        return bool(getattr(self.inner, "drain_on_stop", False))

    @property
    def released(self) -> bool:
        return bool(getattr(self.inner, "released", True))

    def wait_released(self, timeout: float | None = None) -> bool:
        wait = getattr(self.inner, "wait_released", None)
        return bool(wait(timeout)) if callable(wait) else self.released

    def skip_registered(self, sheets: set[tuple[str, ...]]) -> None:
        skip = getattr(self.inner, "skip_registered", None)
        if callable(skip):
            skip(sheets)

    def bind_storage(self, batch_dir: Path, target_seq: int | None = None) -> None:
        bind = getattr(self.inner, "bind_storage", None)
        if callable(bind):
            bind(batch_dir, target_seq=target_seq)

    # ---------- 迭代（流水线线程） ----------
    def sheets(self) -> Iterator[SourceItem]:
        try:
            while True:
                if not getattr(self.inner, "drain_on_pause", False):
                    self._running.wait()
                if self._stopped.is_set() and not self.drain_on_stop:
                    return
                try:
                    item = next(self._it)
                except StopIteration:
                    return
                self.taken += 1
                yield item
        finally:
            close = getattr(self._it, "close", None)
            if callable(close):
                close()
