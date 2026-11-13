from scan_answer.app.settings import AppSettings
from scan_answer.pipeline.events import FinalizedEvent
from scan_answer.ui.controller import IDLE, RUNNING, ScanController
from scan_answer.ui.main_window import MainWindow
from conftest import save_now
from test_delivery_export import batch as batch, delivery_seed as delivery_seed


def test_delivery_panel_checks_and_exports_real_batch(qtbot, batch, tmp_path):
    from scan_answer.ui.widgets.delivery_panel import DeliveryPanel
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.set_batch(batch)
    panel.check()
    qtbot.waitUntil(lambda: not panel.busy, timeout=30000)
    assert panel.last_report.ready
    assert "可交付" in panel.status.text()
    target = tmp_path / "from-ui"
    panel.export_to(target)
    qtbot.waitUntil(lambda: panel.busy, timeout=10000)  # 先在后台检查目标位置，再开始生成
    qtbot.waitUntil(lambda: not panel.busy, timeout=30000)
    assert (target / "delivery.json").is_file()
    assert panel.last_export == target


def test_active_batch_blocks_delivery_action(qtbot, batch):
    from scan_answer.ui.widgets.delivery_panel import DeliveryPanel
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.set_batch(batch)
    panel.active_batch_fn = lambda: str(batch)
    panel.check()
    assert not panel.busy
    assert panel.last_report is None
    assert "正在" in panel.status.text()


def test_changed_batch_does_not_display_old_check_callback(qtbot, batch, tmp_path, monkeypatch):
    from scan_answer.output.audit import audit_batch
    from scan_answer.ui.widgets.delivery_panel import DeliveryPanel

    class DeferredThread:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

    report = audit_batch(batch)
    monkeypatch.setattr("scan_answer.ui.widgets.delivery_panel.threading.Thread", DeferredThread)
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.set_batch(batch)
    panel.check()
    token = panel._generation
    panel.set_batch(tmp_path / "other")
    panel._finished(token, ("check", report))
    assert panel.last_report is None
    assert "可交付" not in panel.status.text()


def test_saved_scan_defaults_apply_now_or_after_active_round(qtbot, app_home, no_dialogs):
    settings = AppSettings()
    controller = ScanController()
    window = MainWindow(settings, controller)
    qtbot.addWidget(window)
    try:
        settings.scan.dpi = 600
        window._settings_saved()
        assert window.scan_page.dpi.value() == "600"
        controller._operation = "scan"
        controller._set_state(RUNNING)
        settings.scan.dpi = 200
        window._settings_saved()
        assert window.scan_page.dpi.value() == "600"
        controller._operation = None
        controller._set_state(IDLE)
        assert window.scan_page.dpi.value() == "200"
    finally:
        controller._operation = None
        controller.shutdown()


def test_failed_settlement_is_not_presented_as_ended(qtbot, app_home):
    controller = ScanController()
    window = MainWindow(AppSettings(), controller)
    qtbot.addWidget(window)
    try:
        window.scan_page._on_finalized(FinalizedEvent(0.4, 0.2, 0, completed=False, failed_seqs=[1]))
        page = window.scan_page
        assert "没能结束" in page.finish_result.text() and "1 份重新判定失败" in page.finish_result.text()
        assert page.prev_title.text() == "最近一张"  # 结果不再写进“最近一张”的标题
    finally:
        controller.shutdown()


def test_invalid_settings_do_not_change_effective_defaults(qtbot, app_home, no_dialogs):
    from scan_answer.ui.pages.settings_page import SettingsPage
    settings = AppSettings()
    page = SettingsPage(settings)
    qtbot.addWidget(page)
    page.dpi.set_value("600")
    page.exam_pattern.setText("{unknown}")
    assert not page.save()
    assert settings.scan.dpi == 300


def test_failed_settings_write_preserves_effective_configuration(qtbot, app_home, no_dialogs, monkeypatch):
    from scan_answer.ui.pages.settings_page import SettingsPage
    settings = AppSettings()
    page = SettingsPage(settings)
    qtbot.addWidget(page)
    page.dpi.set_value("600")

    def fail_write(config):
        raise OSError("disk full")

    monkeypatch.setattr("scan_answer.ui.pages.settings_page.save_settings", fail_write)
    assert not save_now(page)  # 写盘在后台：失败后设置不生效
    assert settings.scan.dpi == 300 and "保存失败" in no_dialogs[-1][0] + no_dialogs[-1][1]


def test_completed_working_batch_can_be_rejudged_for_legacy_repairs(qtbot, app_home, batch):
    controller = ScanController()
    window = MainWindow(AppSettings(), controller)
    qtbot.addWidget(window)
    try:
        controller.open_batch(batch)
        assert window.scan_page.finish_btn.isEnabled()
        assert "再次结束批次" == window.scan_page.finish_btn.text()
    finally:
        controller.shutdown()
