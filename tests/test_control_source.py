import threading
import time

from scan_answer.source.base import SheetImages
from scan_answer.source.control import ControlledSource


class Counting:
    def __init__(self, n):
        self.n = n
        self.calls = 0

    def sheets(self):
        self.calls += 1
        for i in range(self.n):
            yield SheetImages(source=str(i), names=[], dpis=[])


def test_pause_resume_continues_from_same_position():
    inner = Counting(5)
    src = ControlledSource(inner)
    got = []

    def consume():
        for item in src.sheets():
            got.append(item.source)
            if item.source == "1":
                src.pause()

    t = threading.Thread(target=consume)
    t.start()
    time.sleep(0.3)
    assert got == ["0", "1"] and src.paused
    src.resume()
    t.join(2)
    assert got == ["0", "1", "2", "3", "4"] and inner.calls == 1 and src.taken == 5


def test_stop_while_paused_ends_iteration():
    src = ControlledSource(Counting(5))
    it = src.sheets()
    assert next(it).source == "0"
    src.pause()
    threading.Timer(0.2, src.stop).start()
    assert list(it) == [] and src.stopped and not src.paused


def test_stop_closes_underlying_input_resource():
    """停止 PDF 输入时，即使包装器仍被控制器持有，也要关闭其内部读取器。"""
    closed = []

    class Source:
        def sheets(self):
            try:
                yield "page"
                yield "another"
            finally:
                closed.append(True)

    source = ControlledSource(Source())
    iterator = source.sheets()
    assert next(iterator) == "page"
    source.stop()
    assert list(iterator) == []
    assert closed == [True]
