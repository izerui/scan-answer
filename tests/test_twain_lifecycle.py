import queue
import sys
import threading
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image

from scan_answer.source import twain_source as ts
from scan_answer.source.base import DeviceEvent, SheetImages
from scan_answer.source.control import ControlledSource


CAPS = (
    "CAP_FEEDERENABLED CAP_AUTOFEED CAP_DUPLEXENABLED ICAP_PIXELTYPE "
    "ICAP_XRESOLUTION ICAP_YRESOLUTION ICAP_UNITS TWUN_INCHES TWTY_BOOL "
    "TWTY_UINT16 TWTY_FIX32 TWTY_INT16 TWPT_RGB TWPT_GRAY TWFT_RED TWFT_GREEN "
    "TWFT_BLUE TWFT_NONE ICAP_FILTER ICAP_AUTOMATICBORDERDETECTION "
    "ICAP_UNDEFINEDIMAGESIZE ICAP_SUPPORTEDSIZES TWSS_NONE CAP_INDICATORS CAP_XFERCOUNT"
).split()


class Driver:
    def __init__(self, rounds=(), fail_cap=None, actual=None, honor_cancel=True):
        self.rounds = list(rounds)
        self.fail_cap = fail_cap
        self.actual = actual or {}
        self.values = {}
        self.calls = 0
        self.honor_cancel = honor_cancel
        self.closed = threading.Event()
        self.close_gate = None
        self.close_entered = threading.Event()

    def set_capability(self, cap, type_id, value):
        if cap == self.fail_cap:
            raise RuntimeError("unsupported")
        self.values[cap] = (type_id, value)

    def get_capability_current(self, cap):
        return self.actual.get(cap, self.values[cap])

    def acquire_natively(self, after, before, **kwargs):
        values = self.rounds[self.calls]
        self.calls += 1
        for value in values:
            image = SimpleNamespace(
                array=np.full((8, 12), value, dtype=np.uint8), close=lambda: None,
            )
            try:
                before({})
            except self.twain.exceptions.CancelAll:
                if self.honor_cancel:
                    return
            try:
                after(image, -1)
            except self.twain.exceptions.CancelAll:
                if self.honor_cancel:
                    return

    def close(self):
        self.close_entered.set()
        if self.close_gate is not None:
            self.close_gate.wait(5)
        self.closed.set()


@pytest.fixture
def install_driver(monkeypatch):
    constants = SimpleNamespace(**{name: i for i, name in enumerate(CAPS, 1)})
    error = type("TwainError", (Exception,), {})
    exceptions = SimpleNamespace(TwainError=error, CancelAll=type("CancelAll", (Exception,), {}))
    for name in ("PaperDoubleFeedError", "PaperJam", "DSTransferCancelled", "CheckDeviceOnlineError"):
        setattr(exceptions, name, type(name, (error,), {}))
    api = SimpleNamespace(lowlevel=SimpleNamespace(constants=constants), exceptions=exceptions)
    monkeypatch.setattr(ts, "_create_hidden_window", lambda: 1)
    monkeypatch.setattr(ts, "_destroy_window", lambda hwnd: None)
    monkeypatch.setattr(ts, "_dib_to_array", lambda image: (image.array, 300.0))
    monkeypatch.setattr(ts, "_RELEASE_TIMEOUT", 0.02, raising=False)

    def install(driver):
        driver.twain = api
        api.SourceManager = lambda *a, **kw: SimpleNamespace(
            source_list=["fake"], open_source=lambda name: driver, close=lambda: None,
        )
        monkeypatch.setitem(sys.modules, "twain", api)
        return api

    return install, constants


@pytest.mark.parametrize("faces,duplex", [(1, True), (2, False), (0, False), (3, True)])
def test_invalid_face_mode_is_rejected_before_opening_scanner(faces, duplex):
    with pytest.raises(ValueError):
        ts.TwainSource(ts.TwainConfig(duplex=duplex), faces)


@pytest.mark.parametrize("cap", [
    "CAP_FEEDERENABLED", "CAP_AUTOFEED", "CAP_DUPLEXENABLED",
    "ICAP_XRESOLUTION", "ICAP_YRESOLUTION",
])
def test_mandatory_capability_failure_prevents_acquisition(install_driver, tmp_path, cap):
    install, c = install_driver
    driver = Driver([[1, 2]], fail_cap=getattr(c, cap))
    install(driver)
    source = ts.TwainSource(ts.TwainConfig(), 2, single_sheet=True)
    source.bind_storage(tmp_path)
    items = list(source.sheets())
    assert len(items) == 1 and isinstance(items[0], DeviceEvent)
    assert items[0].kind == "error"
    assert driver.calls == 0
    assert source.released


@pytest.mark.parametrize("cap,value", [
    ("CAP_DUPLEXENABLED", False), ("CAP_FEEDERENABLED", False),
    ("ICAP_XRESOLUTION", 200.0), ("ICAP_YRESOLUTION", 200.0),
])
def test_actual_critical_mode_must_match_request(install_driver, tmp_path, cap, value):
    install, c = install_driver
    driver = Driver([[1, 2]], actual={getattr(c, cap): (c.TWTY_BOOL, value)})
    install(driver)
    source = ts.TwainSource(ts.TwainConfig(), 2, single_sheet=True)
    source.bind_storage(tmp_path)
    items = list(source.sheets())
    assert len(items) == 1 and isinstance(items[0], DeviceEvent)
    assert items[0].kind == "error"
    assert driver.calls == 0


def test_single_rescan_retains_every_received_page(install_driver, tmp_path):
    install, _ = install_driver
    install(Driver([[11, 22, 33, 44, 55]], honor_cancel=False))
    source = ts.TwainSource(ts.TwainConfig(), 2, single_sheet=True)
    source.bind_storage(tmp_path, target_seq=10001)
    sheets = [s for s in source.sheets() if isinstance(s, SheetImages)]
    assert [len(s.paths) for s in sheets] == [2, 2, 1]
    assert all(s.images == [] for s in sheets)
    assert all(o["target_seq"] == 10001 for s in sheets for o in s.origins)
    assert all(p.is_relative_to(tmp_path / "acquisition") for s in sheets for p in s.paths)
    pixels = []
    for sheet in sheets:
        for path in sheet.paths:
            with Image.open(path) as image:
                pixels.append(np.asarray(image)[0, 0])
    assert pixels == [11, 22, 33, 44, 55]
    assert source.wait_released(1)


def test_stop_drains_callback_pages_and_partial_sheet(install_driver, tmp_path):
    install, _ = install_driver
    install(Driver([[1, 2, 3, 4, 5]], honor_cancel=False))
    source = ts.TwainSource(ts.TwainConfig(), 2)
    source.bind_storage(tmp_path)
    controlled = ControlledSource(source)
    iterator = controlled.sheets()
    first = next(iterator)
    controlled.pause()
    controlled.stop()
    remaining = list(iterator)
    sheets = [first] + [s for s in remaining if isinstance(s, SheetImages)]
    assert [len(s.paths) for s in sheets] == [2, 2, 1]
    assert controlled.released
    assert controlled.wait_released(0)


def test_pause_drains_current_round_then_resume_starts_a_new_round(install_driver, tmp_path):
    install, _ = install_driver
    driver = Driver([[1, 2, 3], [4, 5]], honor_cancel=False)
    install(driver)
    source = ts.TwainSource(ts.TwainConfig(), 2)
    source.bind_storage(tmp_path)
    controlled = ControlledSource(source)
    iterator = controlled.sheets()
    first = next(iterator)
    controlled.pause()
    second = next(iterator)
    event = next(iterator)
    assert isinstance(event, DeviceEvent)
    assert [len(first.paths), len(second.paths)] == [2, 1]
    pending = []
    read_started = threading.Event()

    def read():
        read_started.set()
        pending.append(next(iterator))

    reader = threading.Thread(target=read)
    reader.start()
    assert read_started.wait(1)
    reader.join(0.05)
    assert reader.is_alive() and driver.calls == 1
    controlled.resume()
    reader.join(2)
    assert not reader.is_alive()
    assert len(pending[0].paths) == 2
    assert pending[0].origins[0]["round_id"] != first.origins[0]["round_id"]
    controlled.stop()
    list(iterator)


def test_join_timeout_does_not_report_released(install_driver, tmp_path):
    install, _ = install_driver
    driver = Driver([[1, 2]])
    driver.close_gate = threading.Event()
    install(driver)
    source = ts.TwainSource(ts.TwainConfig(), 2, single_sheet=True)
    source.bind_storage(tmp_path)
    try:
        items = list(source.sheets())
        assert driver.close_entered.wait(1)
        assert any(isinstance(s, SheetImages) for s in items)
        assert any(isinstance(s, DeviceEvent) and s.kind == "error" for s in items)
        assert not source.released
        assert not source.wait_released(0.01)
        with pytest.raises(RuntimeError):
            list(source.sheets())
    finally:
        driver.close_gate.set()
    assert source.wait_released(1)


def test_callback_writes_before_publishing_descriptor(install_driver, tmp_path):
    install, _ = install_driver
    driver = Driver([[5]])
    api = install(driver)
    from scan_answer.source.spool import AcquisitionSpool, recover_spooled

    class InspectQueue(queue.Queue):
        def put(self, item, *args, **kwargs):
            if isinstance(item, ts._Page):
                assert item.path.is_file()
                assert len(list(recover_spooled(tmp_path))) == 1
                assert not hasattr(item, "image")
            return super().put(item, *args, **kwargs)

    out = InspectQueue()
    thread = ts._ScannerThread(ts.TwainConfig(duplex=False), out, AcquisitionSpool(tmp_path, faces=1))
    thread._round(driver, api, -1)
    assert isinstance(out.get_nowait(), ts._Page)
    assert isinstance(out.get_nowait(), ts._RoundEnd)


def test_single_sheet_cancels_feeding_before_consumer_reads(install_driver, tmp_path):
    install, _ = install_driver
    driver = Driver([[1, 2, 3, 4]])
    api = install(driver)
    from scan_answer.source.spool import AcquisitionSpool, recover_spooled

    out = queue.Queue()
    thread = ts._ScannerThread(ts.TwainConfig(), out, AcquisitionSpool(tmp_path, faces=2))
    thread._round(driver, api, 2)
    assert sum(len(s.paths) for s in recover_spooled(tmp_path)) == 2


def test_blocked_acquisition_times_out_stop_without_false_release(install_driver, tmp_path):
    install, _ = install_driver
    gate = threading.Event()
    entered = threading.Event()

    class BlockedDriver(Driver):
        def acquire_natively(self, after, before, **kwargs):
            entered.set()
            gate.wait(3)
            super().acquire_natively(after, before, **kwargs)

    driver = BlockedDriver([[1]], honor_cancel=False)
    install(driver)
    source = ts.TwainSource(ts.TwainConfig(duplex=False), 1)
    source.bind_storage(tmp_path)
    items = []
    reader = threading.Thread(target=lambda: items.extend(source.sheets()))
    reader.start()
    try:
        assert entered.wait(1)
        source.stop()
        reader.join(1)
        assert not reader.is_alive()
        assert any(isinstance(s, DeviceEvent) and "超时" in s.message for s in items)
        assert not source.released
    finally:
        gate.set()
        reader.join(2)
    assert source.wait_released(1)
    from scan_answer.source.spool import recover_spooled

    assert len(list(recover_spooled(tmp_path))) == 1


def test_spool_failure_flushes_already_received_half_sheet(install_driver, tmp_path, monkeypatch):
    install, _ = install_driver
    install(Driver([[1, 2]]))
    from scan_answer.source.spool import AcquisitionSpool

    append = AcquisitionSpool.append

    def fail_second(self, round_id, pixels, dpi):
        if pixels[0, 0] == 2:
            raise OSError("disk full")
        return append(self, round_id, pixels, dpi)

    monkeypatch.setattr(AcquisitionSpool, "append", fail_second)
    source = ts.TwainSource(ts.TwainConfig(), 2, single_sheet=True)
    source.bind_storage(tmp_path)
    items = list(source.sheets())
    assert isinstance(items[0], SheetImages) and len(items[0].paths) == 1
    assert isinstance(items[1], DeviceEvent) and "disk full" in items[1].message
    assert source.released


def test_controlled_storage_and_mode_reach_scanner(install_driver, tmp_path):
    install, _ = install_driver
    install(Driver([[1]]))
    scanner = ts.TwainSource(ts.TwainConfig(duplex=False), 1, single_sheet=True)
    source = ControlledSource(scanner)
    source.bind_storage(tmp_path, target_seq=9)
    assert source.mode == "scanner"
    assert source.drain_on_stop
    items = list(source.sheets())
    assert items[0].origins[0]["target_seq"] == 9
    assert items[0].paths[0].is_relative_to(tmp_path)
