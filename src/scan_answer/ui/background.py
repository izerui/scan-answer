"""后台任务：读文件、解码图片等耗时操作放到线程池，结果回到界面线程处理，避免界面卡顿。"""
from __future__ import annotations

import weakref
from typing import Any, Callable

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QApplication


class Latest(QObject):
    """同一用途的后台任务，只认最后一次提交（如快速翻学生时只显示最后停下的那一个）：

    - 提交新任务后，还没开始执行的旧任务直接跳过，不读文件；
    - 已在执行的旧任务跑完后结果丢弃（线程不能安全地强行终止）；
    - done / failed 回调在界面线程执行，可以直接操作控件；
    - wait_for_modal：结果回来时若有其他模态对话框开着（如作废确认），等它关闭后再处理，
      免得在对话框里弹出新的对话框或切换页面。会弹窗、切页面的操作用它；图片加载等不用。"""

    _finished = Signal(int, object, object)  # 序号、(done, failed)、(是否成功, 结果或异常)
    _instances: weakref.WeakSet[Latest] = weakref.WeakSet()

    def __init__(self, parent: QObject | None = None, wait_for_modal: bool = False):
        super().__init__(parent)
        self._seq = 0
        self._wait_for_modal = wait_for_modal
        self._finished.connect(self._deliver)
        Latest._instances.add(self)

    def submit(self, work: Callable[[], Any], done: Callable[[Any], None],
               failed: Callable[[BaseException], None] | None = None) -> None:
        """在后台线程执行 work()，完成后在界面线程调用 done(结果)；work 抛异常时调用 failed(异常)。"""
        self._seq += 1
        seq, finished = self._seq, self._finished

        def run() -> None:
            if seq != self._seq:
                return  # 已有更新的任务：还没开始就作废
            try:
                outcome = (True, work())
            except Exception as e:  # noqa: BLE001 — 交给界面线程的 failed 处理
                outcome = (False, e)
            try:
                finished.emit(seq, (done, failed), outcome)
            except RuntimeError:
                pass  # 执行期间宿主控件已销毁

        QThreadPool.globalInstance().start(run)

    def cancel(self) -> None:
        """作废所有已提交的任务：没开始的不再执行，执行中的结果丢弃。"""
        self._seq += 1

    @classmethod
    def cancel_all(cls) -> None:
        """程序退出时：作废所有用途的后台任务，做完的任务也不再回调（免得退出过程中又建批次、切页面）。"""
        for latest in list(cls._instances):
            latest.cancel()

    def _deliver(self, seq: int, callbacks: tuple, outcome: tuple) -> None:
        if seq != self._seq:
            return
        if self._wait_for_modal and QApplication.activeModalWidget() is not None:
            # 定时器挂在自己身上：宿主销毁后不再触发
            QTimer.singleShot(100, self, lambda: self._deliver(seq, callbacks, outcome))
            return
        done, failed = callbacks
        ok, value = outcome
        if ok:
            done(value)
        elif failed is not None:
            failed(value)
        else:
            raise value
