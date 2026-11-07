"""TWAIN 扫描仪图像源（仅 Windows）。

TWAIN 数据源只能在打开它的线程里工作，并且该线程要运行 Windows 消息循环，
所以这里单开一个“扫描线程”：它创建隐藏的消息窗口、打开 DSM 和数据源、设置能力项、执行采集，
每扫到一面就放进队列；流水线线程在 TwainSource.sheets() 里从队列取页面，按模板面数组装成一张卡。

一轮采集 = 进纸器里的纸全部扫完，或因卡纸 / 双张中止。每轮结束 yield 一个 DeviceEvent，
控制器收到后自动暂停；用户处理好纸张点“继续”，生成器再请求下一轮。
"""
from __future__ import annotations

import ctypes as ct
import io
import queue
import struct
import sys
import threading
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np
from PIL import Image

from .base import DeviceEvent, SheetImages, SourceItem

# TWAIN 2.2 新增的能力项，pytwain 的常量表里可能没有
CAP_DOUBLEFEEDDETECTION = 0x1132
TWDF_ULTRASONIC = 0
HWND_MESSAGE = -3
_POLL = 0.2  # 流水线线程等待扫描线程时检查停止标志的间隔（秒）


@dataclass
class TwainConfig:
    scanner: str = ""  # 数据源名称；空表示用列表中的第一个
    duplex: bool = True
    dpi: int = 300
    color: str = "gray"  # gray / color
    double_feed: bool = True


# ---------- 扫描线程与流水线线程之间的消息 ----------
@dataclass
class _Ready:
    name: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Page:
    image: np.ndarray
    dpi: float


@dataclass
class _RoundEnd:
    reason: str  # done / double_feed / paper_jam / cancelled / error
    message: str = ""
    pages: int = 0


@dataclass
class _Failure:
    message: str


# ---------- Windows 隐藏窗口 ----------
def _create_hidden_window() -> int:
    """在当前线程创建一个隐藏的消息窗口，作为 TWAIN 的父窗口；返回 HWND。"""
    user32 = ct.windll.user32  # type: ignore[attr-defined]
    user32.CreateWindowExW.restype = ct.c_void_p
    user32.CreateWindowExW.argtypes = [ct.c_uint32, ct.c_wchar_p, ct.c_wchar_p, ct.c_uint32, ct.c_int, ct.c_int,
                                       ct.c_int, ct.c_int, ct.c_void_p, ct.c_void_p, ct.c_void_p, ct.c_void_p]
    hwnd = user32.CreateWindowExW(0, "STATIC", "scan-answer-twain", 0, 0, 0, 0, 0,
                                  ct.c_void_p(HWND_MESSAGE), None, None, None)
    if not hwnd:
        raise OSError("无法创建扫描线程的消息窗口")
    return int(hwnd)


def _destroy_window(hwnd: int) -> None:
    try:
        user32 = ct.windll.user32  # type: ignore[attr-defined]
        user32.DestroyWindow.argtypes = [ct.c_void_p]
        user32.DestroyWindow(ct.c_void_p(hwnd))
    except Exception:  # noqa: BLE001 — 退出清理，失败无妨
        pass


# ---------- 可用性与扫描仪列表 ----------
def _run_in_thread(fn, timeout: float = 20.0):
    """在一个新线程里执行（TWAIN 调用必须和创建 DSM 的线程一致），返回 (结果, 异常)。"""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — 原样带回调用方
            box["error"] = e

    t = threading.Thread(target=target, name="twain-probe", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, TimeoutError("扫描仪驱动无响应")
    return box.get("value"), box.get("error")


def _probe() -> list[str]:
    import twain

    hwnd = _create_hidden_window()
    try:
        sm = twain.SourceManager(hwnd, ProductName="答题卡扫描识别")
        try:
            return list(sm.source_list)
        finally:
            sm.close()
    finally:
        _destroy_window(hwnd)


last_error = ""


def twain_available() -> tuple[bool, str]:
    """扫描仪功能是否可用，以及不可用的原因（界面直接显示）。"""
    if sys.platform != "win32":
        return False, "扫描仪仅支持 Windows"
    try:
        import twain  # noqa: F401
    except ImportError:
        return False, "未安装 pytwain"
    _, err = _run_in_thread(_probe)
    if err is not None:
        return False, f"未找到 TWAIN 驱动管理器（TWAINDSM.dll）：{type(err).__name__}: {err}"
    return True, ""


def list_scanners() -> list[str]:
    """列出可用的 TWAIN 数据源；出错时返回空列表，原因记在模块变量 last_error。"""
    global last_error
    if sys.platform != "win32":
        last_error = "扫描仪仅支持 Windows"
        return []
    try:
        import twain  # noqa: F401
    except ImportError:
        last_error = "未安装 pytwain"
        return []
    names, err = _run_in_thread(_probe)
    if err is not None:
        last_error = f"{type(err).__name__}: {err}"
        return []
    last_error = ""
    return names or []


# ---------- 图像转换 ----------
def _dib_to_array(img_obj) -> tuple[np.ndarray, float | None]:
    """把 pytwain 返回的内存 DIB 转为 numpy 数组（灰度 L 或彩色 RGB），并取出 DPI。

    不依赖 GlobalSize（TWAIN 2 的内存分配器可能不兼容），按位图头计算数据长度。
    """
    handle = img_obj._handle
    ptr = img_obj._lock(handle)
    try:
        header = ct.string_at(ptr, 40)
        (bi_size, width, height, _planes, bit_count, _compr, size_image, xppm, _yppm, clr_used,
         _clr_imp) = struct.unpack("<IiiHHIIiiII", header)
        stride = ((width * bit_count + 31) // 32) * 4
        palette = clr_used or (1 << bit_count if bit_count <= 8 else 0)
        data_len = size_image or stride * abs(height)
        dib = ct.string_at(ptr, bi_size + palette * 4 + data_len)
    finally:
        img_obj._unlock(handle)
    offset = 14 + bi_size + palette * 4
    bmp = b"BM" + struct.pack("<IHHI", 14 + len(dib), 0, 0, offset) + dib
    with Image.open(io.BytesIO(bmp)) as im:
        im.load()
        if im.mode in ("RGB", "RGBA"):
            arr = np.asarray(im.convert("RGB"))
        else:
            arr = np.asarray(im.convert("L"))
    dpi = xppm * 0.0254 if xppm > 0 else None
    return arr.copy(), dpi


# ---------- 扫描线程 ----------
class _ScannerThread(threading.Thread):
    def __init__(self, cfg: TwainConfig, out: queue.Queue):
        super().__init__(name="twain-scanner", daemon=True)
        self.cfg = cfg
        self.out = out
        self.cmds: queue.Queue = queue.Queue()
        self.cancel = threading.Event()

    # 流水线线程调用
    def request_round(self, count: int) -> None:
        self.cancel.clear()
        self.cmds.put(("round", count))

    def shutdown(self) -> None:
        self.cancel.set()
        self.cmds.put(("quit", 0))

    # 扫描线程
    def run(self) -> None:
        try:
            import twain
        except ImportError:
            self.out.put(_Failure("未安装 pytwain，无法使用扫描仪"))
            return
        hwnd = 0
        sm = src = None
        try:
            hwnd = _create_hidden_window()
            sm = twain.SourceManager(hwnd, ProductName="答题卡扫描识别")
            names = list(sm.source_list)
            name = self.cfg.scanner if self.cfg.scanner in names else (names[0] if names else "")
            if not name:
                self.out.put(_Failure("没有找到扫描仪。请检查扫描仪电源、USB 连接和 TWAIN 驱动"))
                return
            src = sm.open_source(name)
            if src is None:
                self.out.put(_Failure(f"无法打开扫描仪：{name}"))
                return
            self.out.put(_Ready(name, self._configure(src, twain)))
            while True:
                cmd, count = self.cmds.get()
                if cmd == "quit":
                    return
                self._round(src, twain, count)
        except Exception as e:  # noqa: BLE001 — 任何驱动错误都报给流水线线程
            self.out.put(_Failure(f"扫描仪错误：{type(e).__name__}: {e}"))
        finally:
            for obj in (src, sm):
                try:
                    if obj is not None:
                        obj.close()
                except Exception:  # noqa: BLE001
                    pass
            if hwnd:
                _destroy_window(hwnd)

    def _configure(self, src, twain) -> list[str]:
        """逐项设置能力项；不支持的项记入警告，不中断采集。"""
        c = twain.lowlevel.constants
        cfg = self.cfg
        warnings: list[str] = []

        def setcap(label: str, cap: int, type_id: int, value) -> None:
            try:
                src.set_capability(cap, type_id, value)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{label}（{type(e).__name__}）")

        setcap("进纸器", c.CAP_FEEDERENABLED, c.TWTY_BOOL, True)
        setcap("自动进纸", c.CAP_AUTOFEED, c.TWTY_BOOL, True)
        setcap("双面", c.CAP_DUPLEXENABLED, c.TWTY_BOOL, bool(cfg.duplex))
        setcap("色彩", c.ICAP_PIXELTYPE, c.TWTY_UINT16, c.TWPT_RGB if cfg.color == "color" else c.TWPT_GRAY)
        setcap("分辨率", c.ICAP_XRESOLUTION, c.TWTY_FIX32, float(cfg.dpi))
        setcap("分辨率", c.ICAP_YRESOLUTION, c.TWTY_FIX32, float(cfg.dpi))
        if cfg.double_feed:
            setcap("双张检测", CAP_DOUBLEFEEDDETECTION, c.TWTY_UINT16, TWDF_ULTRASONIC)
        setcap("进度指示", c.CAP_INDICATORS, c.TWTY_BOOL, False)
        return sorted(set(warnings), key=warnings.index)

    def _round(self, src, twain, count: int) -> None:
        c = twain.lowlevel.constants
        exc = twain.exceptions
        pages = 0
        try:
            src.set_capability(c.CAP_XFERCOUNT, c.TWTY_INT16, count)
        except Exception:  # noqa: BLE001 — 不支持时按驱动默认（通常是扫完为止）
            pass

        def before(_info: dict) -> None:
            if self.cancel.is_set():
                raise exc.CancelAll

        def after(img, _more: int) -> None:
            nonlocal pages
            try:
                arr, dpi = _dib_to_array(img)
            finally:
                img.close()  # 及时释放驱动内存，大批量扫描时避免泄漏
            pages += 1
            self.out.put(_Page(arr, dpi or float(self.cfg.dpi)))
            if self.cancel.is_set():
                raise exc.CancelAll

        try:
            src.acquire_natively(after=after, before=before, show_ui=False, modal=False)
        except exc.PaperDoubleFeedError:
            self.out.put(_RoundEnd("double_feed", "检测到双张进纸，扫描已停止", pages))
            return
        except exc.PaperJam:
            self.out.put(_RoundEnd("paper_jam", "扫描仪卡纸，扫描已停止", pages))
            return
        except exc.DSTransferCancelled:
            self.out.put(_RoundEnd("cancelled", "扫描已取消", pages))
            return
        except exc.CheckDeviceOnlineError:
            self.out.put(_RoundEnd("error", "扫描仪未连接或未开机", pages))
            return
        except exc.TwainError as e:
            self.out.put(_RoundEnd("error", f"扫描仪报错：{type(e).__name__}: {e}", pages))
            return
        self.out.put(_RoundEnd("cancelled" if self.cancel.is_set() else "done", "", pages))


# ---------- 图像源 ----------
ROUND_MESSAGES = {
    "done": "进纸器已空。放入下一叠答题卡后点“继续”；全部扫完点“结束批次”。",
    "double_feed": "检测到双张进纸，已暂停。把这两张纸取出，放回进纸口最前面后点“继续”。",
    "paper_jam": "扫描仪卡纸，已暂停。取出卡住的纸，放回进纸口最前面后点“继续”。",
}


class TwainSource:
    """扫描仪图像源。single_sheet=True 时只扫一张卡（物理异常重扫），扫完即结束。"""

    def __init__(self, cfg: TwainConfig, faces: int, single_sheet: bool = False):
        self.cfg = cfg
        self.faces = max(1, faces)
        self.single_sheet = single_sheet
        self.scanner_name = cfg.scanner
        self.warnings: list[str] = []
        self._stop = threading.Event()
        self._thread: _ScannerThread | None = None
        self._counter = 0

    def stop(self) -> None:
        """任意线程调用：结束采集（ControlledSource.stop 会转调这里）。"""
        self._stop.set()
        if self._thread is not None:
            self._thread.cancel.set()

    def _get(self, out: queue.Queue):
        """等待扫描线程的下一条消息；停止时返回 None。"""
        while not self._stop.is_set():
            try:
                return out.get(timeout=_POLL)
            except queue.Empty:
                if self._thread is not None and not self._thread.is_alive() and out.empty():
                    return _Failure("扫描线程意外退出")
        return None

    def _sheet(self, pages: list[_Page]) -> SheetImages:
        self._counter += 1
        n = self._counter
        return SheetImages(source=f"扫描 {n:05d}", names=[f"scan{n:05d}_{i}.jpg" for i in range(1, len(pages) + 1)],
                           dpis=[p.dpi for p in pages], images=[p.image for p in pages])

    def sheets(self) -> Iterator[SourceItem]:
        out: queue.Queue = queue.Queue()
        th = _ScannerThread(self.cfg, out)
        self._thread = th
        th.start()
        try:
            first = self._get(out)
            if first is None:
                return
            if isinstance(first, _Failure):
                yield DeviceEvent("error", first.message)
                return
            self.scanner_name = first.name
            self.warnings = first.warnings
            if first.warnings:
                yield DeviceEvent("notice", "扫描仪不支持：" + "、".join(first.warnings) + "（按驱动默认设置扫描）")
            while not self._stop.is_set():
                th.request_round(self.faces if self.single_sheet else -1)
                pending: list[_Page] = []
                end: _RoundEnd | None = None
                while end is None:
                    item = self._get(out)
                    if item is None:
                        return
                    if isinstance(item, _Failure):
                        yield DeviceEvent("error", item.message)
                        return
                    if isinstance(item, _Page):
                        pending.append(item)
                        if len(pending) == self.faces:
                            yield self._sheet(pending)
                            pending = []
                            if self.single_sheet:
                                th.cancel.set()  # 只要一张：其余进纸取消
                    elif isinstance(item, _RoundEnd):
                        end = item
                if pending:
                    yield self._sheet(pending)  # 面数不齐：流水线会按缺页列入物理异常
                if self.single_sheet or end.reason == "cancelled":
                    return
                if end.reason == "error":
                    yield DeviceEvent("error", end.message + "。处理后点“继续”重试。")
                else:
                    yield DeviceEvent("no_paper" if end.reason == "done" else end.reason,
                                      ROUND_MESSAGES.get(end.reason, end.message))
        finally:
            th.shutdown()
