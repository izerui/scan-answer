"""TWAIN 扫描仪图像源（仅 Windows）。

TWAIN 数据源只能在打开它的线程里工作，并且该线程要运行 Windows 消息循环，
所以这里单开一个“扫描线程”：它创建隐藏的消息窗口、打开 DSM 和数据源、设置能力项、执行采集，
每扫到一面先持久化，再把文件描述放进队列；流水线线程按模板面数组装成一张卡。

一轮采集 = 进纸器里的纸全部扫完，或因卡纸 / 双张中止。每轮结束 yield 一个 DeviceEvent，
控制器收到后自动暂停；用户处理好纸张点“继续”，生成器再请求下一轮。
"""
from __future__ import annotations

import ctypes as ct
import io
import queue
import struct
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import numpy as np
from PIL import Image

from .base import DeviceEvent, SourceItem
from .spool import AcquisitionSpool, SpooledPage, sheet_from_pages

# TWAIN 2.2 新增的能力项，pytwain 的常量表里可能没有
CAP_DOUBLEFEEDDETECTION = 0x1132
TWDF_ULTRASONIC = 0
HWND_MESSAGE = -3
_POLL = 0.2  # 流水线线程等待扫描线程时检查停止标志的间隔（秒）
_RELEASE_TIMEOUT = 5.0


@dataclass
class TwainConfig:
    scanner: str = ""  # 数据源名称；空表示用列表中的第一个
    duplex: bool = True
    dpi: int = 300
    color: str = "gray"  # gray / color
    double_feed: bool = True
    dropout: str = "none"  # 颜色滤除：none / red / green / blue
    crop: str = "auto"  # auto（扫描仪检测纸边）/ template（按模板纸张尺寸）/ driver（驱动默认）
    paper_mm: tuple[float, float] | None = None  # 模板纸张尺寸，crop="template" 时使用


# ---------- 扫描线程与流水线线程之间的消息 ----------
@dataclass
class _Ready:
    name: str
    warnings: list[str] = field(default_factory=list)


_Page = SpooledPage


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


_CHANNEL = {"red": 0, "green": 1, "blue": 2}


def _dropout(arr: np.ndarray, dropout: str) -> np.ndarray:
    """彩色图的软件滤色：取与滤除颜色相同的通道作为灰度图（该颜色的印刷在这个通道里接近白色）。"""
    ch = _CHANNEL.get(dropout)
    if ch is None or arr.ndim != 3:
        return arr
    return np.ascontiguousarray(arr[:, :, ch])


# ---------- 扫描线程 ----------
class _ScannerThread(threading.Thread):
    def __init__(self, cfg: TwainConfig, out: queue.Queue, spool: AcquisitionSpool):
        super().__init__(name="twain-scanner", daemon=True)
        self.cfg = cfg
        self.out = out
        self.cmds: queue.Queue = queue.Queue()
        self.cancel = threading.Event()
        self.spool = spool
        self.round_id = 0

    # 流水线线程调用
    def request_round(self, count: int) -> None:
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
            name = self.cfg.scanner or (names[0] if names else "")
            if name and name not in names:
                raise RuntimeError(f"找不到指定的扫描仪：{name}")
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
        """关键模式必须设置并回读成功；可选功能不支持时提示。"""
        c = twain.lowlevel.constants
        cfg = self.cfg
        warnings: list[str] = []

        def setcap(label: str, cap: int, type_id: int, value) -> None:
            try:
                src.set_capability(cap, type_id, value)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"{label}（{type(e).__name__}）")

        required = [
            ("进纸器", c.CAP_FEEDERENABLED, c.TWTY_BOOL, True),
            ("自动进纸", c.CAP_AUTOFEED, c.TWTY_BOOL, True),
            ("双面", c.CAP_DUPLEXENABLED, c.TWTY_BOOL, bool(cfg.duplex)),
            ("分辨率单位", c.ICAP_UNITS, c.TWTY_UINT16, c.TWUN_INCHES),
            ("横向分辨率", c.ICAP_XRESOLUTION, c.TWTY_FIX32, float(cfg.dpi)),
            ("纵向分辨率", c.ICAP_YRESOLUTION, c.TWTY_FIX32, float(cfg.dpi)),
        ]
        for label, cap, type_id, value in required:
            try:
                src.set_capability(cap, type_id, value)
            except Exception as error:
                raise RuntimeError(f"无法设置{label}：{error}") from error
        setcap("色彩", c.ICAP_PIXELTYPE, c.TWTY_UINT16, c.TWPT_RGB if cfg.color == "color" else c.TWPT_GRAY)
        if cfg.double_feed:
            setcap("双张检测", CAP_DOUBLEFEEDDETECTION, c.TWTY_UINT16, TWDF_ULTRASONIC)
        # 颜色滤除：多数扫描仪只在灰度 / 黑白模式下生效；彩色扫描时由软件取对应通道（见 _dropout）
        filt = {"red": c.TWFT_RED, "green": c.TWFT_GREEN, "blue": c.TWFT_BLUE}.get(cfg.dropout)
        if filt is not None and cfg.color != "color":
            setcap("颜色滤除", c.ICAP_FILTER, c.TWTY_UINT16, filt)
        elif cfg.dropout == "none":
            try:
                src.set_capability(c.ICAP_FILTER, c.TWTY_UINT16, c.TWFT_NONE)
            except Exception:  # noqa: BLE001 — 不支持滤色的扫描仪本来就没有滤色
                pass
        # 裁边
        if cfg.crop == "auto":
            setcap("自动裁边", c.ICAP_AUTOMATICBORDERDETECTION, c.TWTY_BOOL, True)
            setcap("自动裁边", c.ICAP_UNDEFINEDIMAGESIZE, c.TWTY_BOOL, True)
        elif cfg.crop == "template" and cfg.paper_mm:
            w_in, h_in = cfg.paper_mm[0] / 25.4, cfg.paper_mm[1] / 25.4
            try:
                src.set_capability(c.ICAP_SUPPORTEDSIZES, c.TWTY_UINT16, c.TWSS_NONE)
                src.set_image_layout((0.0, 0.0, w_in, h_in))
            except Exception as e:  # noqa: BLE001
                warnings.append(f"按模板纸张尺寸裁切（{type(e).__name__}）")
        setcap("进度指示", c.CAP_INDICATORS, c.TWTY_BOOL, False)
        # Require a singleton (TWTY_*, scalar) from pytwain MSG_GETCURRENT;
        # an ambiguous container must not silently select a default.
        for label, cap, _type_id, expected in required:
            try:
                _actual_type, actual = src.get_capability_current(cap)
            except Exception as error:
                raise RuntimeError(f"无法确认{label}实际设置：{error}") from error
            if actual != expected:
                raise RuntimeError(f"{label}实际设置 {actual!r} 与请求 {expected!r} 不一致")
        return sorted(set(warnings), key=warnings.index)

    def _round(self, src, twain, count: int) -> None:
        c = twain.lowlevel.constants
        exc = twain.exceptions
        pages = 0
        self.round_id += 1
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
            arr = _dropout(arr, self.cfg.dropout)
            page = self.spool.append(self.round_id, arr, dpi or float(self.cfg.dpi))
            pages += 1
            self.out.put(page)
            if count > 0 and pages >= count:
                self.cancel.set()
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
        except exc.CancelAll:
            self.out.put(_RoundEnd("cancelled", "扫描已取消", pages))
            return
        except exc.CheckDeviceOnlineError:
            self.out.put(_RoundEnd("error", "扫描仪未连接或未开机", pages))
            return
        except exc.TwainError as e:
            self.out.put(_RoundEnd("error", f"扫描仪报错：{type(e).__name__}: {e}", pages))
            return
        except Exception as e:
            self.out.put(_RoundEnd("error", f"采集保存失败：{type(e).__name__}: {e}", pages))
            return
        self.out.put(_RoundEnd("cancelled" if self.cancel.is_set() else "done", "", pages))


# ---------- 图像源 ----------
ROUND_MESSAGES = {
    "done": "进纸器已空。放入下一叠答题卡后点“继续”；全部扫完点“结束批次”。",
    "double_feed": "检测到双张进纸，已暂停。已接收页面保留；若有缺面异常，请先结束本轮，按原序号整份重扫，再继续其余答卷。",
    "paper_jam": "扫描仪卡纸，已暂停。取出卡住的纸；若有缺面异常，请先结束本轮，按原序号整份重扫，再继续其余答卷。",
}


class TwainSource:
    """扫描仪图像源。single_sheet=True 时只扫一张卡（物理异常重扫），扫完即结束。"""

    mode = "scanner"
    drain_on_stop = True
    drain_on_pause = True

    def __init__(self, cfg: TwainConfig, faces: int, single_sheet: bool = False):
        if faces not in (1, 2) or bool(cfg.duplex) != (faces == 2):
            raise ValueError("模板面数必须为 1 或 2，并与扫描仪单双面设置一致")
        if cfg.dpi <= 0:
            raise ValueError("扫描分辨率必须大于零")
        self.cfg = cfg
        self.faces = faces
        self.single_sheet = single_sheet
        self.scanner_name = cfg.scanner
        self.warnings: list[str] = []
        self._stop = threading.Event()
        self._stop_at: float | None = None
        self._thread: _ScannerThread | None = None
        self._spool: AcquisitionSpool | None = None
        self._running = threading.Event()
        self._running.set()
        self._lifecycle_lock = threading.Lock()
        self._started = False

    def bind_storage(self, batch_dir: Path, target_seq: int | None = None) -> None:
        """Bind before iteration; controllers must supply the durable batch dir."""
        with self._lifecycle_lock:
            if self._started:
                raise RuntimeError("采集开始后不能更改存储目录")
            self._spool = AcquisitionSpool(batch_dir, self.faces, target_seq)

    @property
    def released(self) -> bool:
        with self._lifecycle_lock:
            return self._thread is None or not self._thread.is_alive()

    def wait_released(self, timeout: float | None = None) -> bool:
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return self.released

    def pause(self) -> None:
        with self._lifecycle_lock:
            self._running.clear()
            if self._thread is not None:
                self._thread.cancel.set()

    def resume(self) -> None:
        self._running.set()

    def stop(self) -> None:
        """任意线程调用：结束采集（ControlledSource.stop 会转调这里）。"""
        with self._lifecycle_lock:
            if self._stop_at is None:
                self._stop_at = time.monotonic()
            self._stop.set()
            self._running.set()
            if self._thread is not None:
                self._thread.cancel.set()

    def _get(self, out: queue.Queue):
        """Drain accepted pages even after stop; a live driver still owns its device."""
        while True:
            try:
                return out.get(timeout=_POLL)
            except queue.Empty:
                if self._thread is not None and not self._thread.is_alive() and out.empty():
                    return _Failure("扫描线程意外退出")
                if self._stop_at is not None and time.monotonic() - self._stop_at >= _RELEASE_TIMEOUT:
                    return _Failure("扫描仪停止超时；已接收页已保存，迟到页将在重开批次时恢复")

    def sheets(self) -> Iterator[SourceItem]:
        out: queue.Queue = queue.Queue()
        with self._lifecycle_lock:
            if self._started:
                raise RuntimeError("扫描源已启动，不能重复使用")
            if self._stop.is_set():
                return
            if self._spool is None:
                self._spool = AcquisitionSpool(Path(tempfile.mkdtemp(prefix="scan-answer-")), self.faces)
            th = _ScannerThread(self.cfg, out, self._spool)
            self._thread = th
            self._started = True
            th.start()
        try:
            yield from self._acquire(out, th)
        finally:
            th.shutdown()
            th.join(_RELEASE_TIMEOUT)
        if not self.released:
            yield DeviceEvent("error", "扫描仪释放超时，设备仍被占用；确认释放前不能再次扫描")

    def _acquire(self, out: queue.Queue, th: _ScannerThread) -> Iterator[SourceItem]:
        first = self._get(out)
        if isinstance(first, _Failure):
            yield DeviceEvent("error", first.message)
            return
        self.scanner_name = first.name
        self.warnings = first.warnings
        if first.warnings:
            yield DeviceEvent("notice", "扫描仪不支持可选设置：" + "、".join(first.warnings))
        while not self._stop.is_set():
            self._running.wait()
            with self._lifecycle_lock:
                if self._stop.is_set():
                    return
                if not self._running.is_set():
                    continue
                th.cancel.clear()
                th.request_round(self.faces if self.single_sheet else -1)
            pending: list[_Page] = []
            end: _RoundEnd | None = None
            while end is None:
                item = self._get(out)
                if isinstance(item, _Failure):
                    if pending:
                        yield sheet_from_pages(pending)
                    yield DeviceEvent("error", item.message)
                    return
                if isinstance(item, _Page):
                    pending.append(item)
                    if len(pending) == self.faces:
                        if self.single_sheet:
                            th.cancel.set()
                        yield sheet_from_pages(pending)
                        pending = []
                elif isinstance(item, _RoundEnd):
                    end = item
            if pending:
                yield sheet_from_pages(pending)
            if end.reason == "error":
                yield DeviceEvent("error", end.message + "。处理后点“继续”重试。")
            if self.single_sheet or self._stop.is_set():
                return
            if end.reason != "error":
                yield DeviceEvent("no_paper" if end.reason == "done" else end.reason,
                                  ROUND_MESSAGES.get(end.reason, end.message))
