"""可暂停、可停止的图像源包装。

界面的"暂停 / 继续"要从上次的位置接着取卡，所以包装器只创建一次底层迭代器，
暂停时阻塞在取下一张之前，停止时结束迭代。供流水线所在的后台线程使用，暂停/继续/停止可在任意线程调用。
"""
from __future__ import annotations

import threading
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

    def resume(self) -> None:
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

    # ---------- 迭代（流水线线程） ----------
    def sheets(self) -> Iterator[SourceItem]:
        while True:
            self._running.wait()
            if self._stopped.is_set():
                return
            try:
                item = next(self._it)
            except StopIteration:
                return
            self.taken += 1
            yield item
