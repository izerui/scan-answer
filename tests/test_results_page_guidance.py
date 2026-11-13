"""结果页（含交付面板、证据预览）的界面内提示：悬停说明、禁用原因、空状态、说明条、情境提示。"""
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLineEdit

from scan_answer.app.settings import AppSettings
from scan_answer.output.jsonfile import read_json
from scan_answer.ui.help_text import CONCEPTS
from scan_answer.ui.pages.results_page import ResultsPage
from scan_answer.ui.widgets.delivery_panel import DeliveryPanel
from scan_answer.ui.widgets.evidence_preview import EvidencePreview
from scan_answer.ui.widgets.guide import HelpButton
from conftest import settle
from test_results_flow_fixes import report, two, write_json  # noqa: F401
from test_delivery_export import batch as ready_batch, delivery_seed as delivery_seed  # noqa: F401


def visible_controls(root):
    """当前真正显示着的按钮、下拉框、输入框（已 deleteLater 的先删掉）。"""
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    QCoreApplication.processEvents()  # 新加入布局的控件在事件循环里才显示
    out = []
    for cls in (QAbstractButton, QComboBox, QLineEdit):
        out += [w for w in root.findChildren(cls) if w.isVisibleTo(root) and w.window() is root.window()]
    return out


def assert_all_tooltips(root):
    missing = [f"{type(w).__name__}:{getattr(w, 'text', lambda: '')()}" for w in visible_controls(root)
               if not w.toolTip().strip()]
    assert not missing, missing


def make_page(qtbot, settings=None):
    page = ResultsPage(settings=settings)
    qtbot.addWidget(page)
    page.resize(1280 - 64, 800 - 80)  # 最小窗口去掉侧栏、顶栏后的大小
    page.show()
    qtbot.waitExposed(page)
    return page


def test_every_visible_control_has_tooltip_before_and_after_opening(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert_all_tooltips(page)
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    assert_all_tooltips(page)
    assert page.select_seq(1)
    settle()  # 结果文件在后台读
    assert_all_tooltips(page)
    page.card_tabs.setCurrentWidget(page.evidence)  # 证据页里的图片下拉框等
    assert_all_tooltips(page)
    page.evidence.tabs.setCurrentIndex(1)
    assert_all_tooltips(page)
    for key in ("all", "warn", "err"):
        assert page.filter.buttons[key].toolTip()
    assert page.search.toolTip()


def test_disabled_buttons_explain_why(qtbot, two, app_home):
    settings = AppSettings()
    page = make_page(qtbot, settings)
    # 未打开批次
    for btn in (page.refresh_btn, page.retry_all_btn, page.skip_btn, page.prev_btn, page.next_btn,
                page.delivery_panel.check_btn, page.delivery_panel.export_btn):
        assert not btn.isEnabled() and "批次" in btn.toolTip(), btn.text()
    assert page.open_batch(two, select_seq=1)
    settle()  # 批次在后台读取
    # 没配置大模型：去设置页
    assert not page.retry_all_btn.isEnabled() and "设置" in page.retry_all_btn.toolTip()
    single = [b for b in visible_controls(page) if b.text() == "重试转写"]
    assert single and all(not b.isEnabled() and "设置" in b.toolTip() for b in single)
    assert not page.skip_btn.isEnabled() and "没有等待转写" in page.skip_btn.toolTip()
    assert not page.prev_btn.isEnabled() and "第一份" in page.prev_btn.toolTip()
    assert page.next_btn.isEnabled() and "下一份" in page.next_btn.toolTip()
    page.next_btn.click()
    settle()  # 结果文件在后台读
    assert page.current.seq == 2
    assert not page.next_btn.isEnabled() and "最后一份" in page.next_btn.toolTip()
    # 未检查：生成交付目录不可点，说明先检查
    panel = page.delivery_panel
    assert panel.check_btn.isEnabled() and "只检查" in panel.check_btn.toolTip()
    assert not panel.export_btn.isEnabled() and "检查交付" in panel.export_btn.toolTip()
    # 配置大模型后可以转写
    settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page.on_settings_changed()
    assert page.retry_all_btn.isEnabled() and "手写" in page.retry_all_btn.toolTip()
    single = [b for b in visible_controls(page) if b.text() == "重试转写"]
    assert single and all(b.isEnabled() and b.toolTip() for b in single)
    # 批次正在扫描：先结束本轮 / 批次
    page._active_batch_fn = lambda: str(two)
    page.on_scan_state_changed()
    for btn in (page.retry_all_btn, panel.check_btn, panel.export_btn):
        assert not btn.isEnabled() and "结束批次" in btn.toolTip(), btn.text()
    single = [b for b in visible_controls(page) if b.text() == "重试转写"]
    assert single and all(not b.isEnabled() and "结束批次" in b.toolTip() for b in single)
    assert "还在扫描中" in page.notice.text()


def test_export_button_follows_check_result(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(two, select_seq=1)
    settle()  # 批次在后台读取
    panel = page.delivery_panel
    panel._show_report(report([{"severity": "error", "code": "not_finalized", "message": "请先结束批次",
                                "seq": None, "path": "manifest.json"}]))
    panel.finished.emit()
    assert not panel.export_btn.isEnabled() and "1 项要处理" in panel.export_btn.toolTip()
    assert "批次还没结束" in page.notice.text() and "结束批次" in page.notice.text()
    panel._show_report(report([]))
    panel.finished.emit()
    assert panel.export_btn.isEnabled() and "不覆盖" in panel.export_btn.toolTip()
    assert "点“生成交付目录" in page.notice.text()
    panel.mark_stale()
    assert not panel.export_btn.isEnabled() and "过期" in panel.export_btn.toolTip()


def test_sealed_batch_cannot_export(qtbot, two):
    manifest = read_json(two / "manifest.json")
    manifest["delivery"] = {"sealed": True}
    write_json(two / "manifest.json", manifest)
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    assert not page.delivery_panel.export_btn.isEnabled()
    assert "只读" in page.delivery_panel.export_btn.toolTip()
    assert "只读" in page.retry_all_btn.toolTip()
    assert "只读" in page.notice.text()


def test_help_buttons_next_to_hard_concepts(qtbot):
    page = ResultsPage()
    qtbot.addWidget(page)
    texts = [b.toolTip() for b in page.findChildren(HelpButton)]
    for concept in ("filters", "ocr", "delivery"):
        assert CONCEPTS[concept] in texts, concept


def test_empty_states(qtbot, two):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert "尚未打开批次" in page.table._empty_hint.label.text()
    assert page.title.text() == "未选择批次"
    assert "还没打开批次" in page._answers_empty.text()
    assert "还没打开批次" in page.subj_empty.text()
    assert "选择学生" in page.evidence.view.placeholder
    assert "打开批次后" in page.delivery_panel.status.text()
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    assert "还没检查交付" in page.delivery_panel.status.text()
    assert "选择学生" in page._answers_empty.text() and "选择学生" in page.subj_empty.text()
    assert "选择学生" in page.evidence.view.placeholder
    assert "检查交付" in page.delivery_panel.status.text()
    page.search.setText("不存在的考号")
    assert "没有符合条件" in page.table._empty_hint.label.text()
    assert not page.prev_btn.isEnabled() and not page.next_btn.isEnabled()
    page.search.clear()
    assert page.select_seq(1)
    settle()  # 结果文件在后台读
    assert page.evidence.image_combo.count() and page.evidence.image_combo.isEnabled()


def test_evidence_preview_without_images():
    preview = EvidencePreview()
    assert not preview.image_combo.isEnabled() and preview.image_combo.toolTip()
    preview.set_result({"pages": {}, "student": {}, "objective": [], "subjective": []})
    assert not preview.image_combo.isEnabled()
    assert "没有" in preview.image_combo.toolTip() and "没有" in preview.view.placeholder
    preview.set_result(None)
    assert preview.view.placeholder == preview.empty_text


def test_situational_hints(qtbot, two, app_home):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.notice.isHidden()  # 未打开批次：列表的空状态已说明，提示行不出现
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    # 批次未结算
    assert "结束批次" in page.notice.text() and not page.notice.isHidden()
    manifest = read_json(two / "manifest.json")
    manifest["finalized"] = True
    write_json(two / "manifest.json", manifest)
    page.refresh()
    settle()  # 刷新在后台读取
    assert "检查交付" in page.notice.text()
    # 操作结果优先显示，不被情境提示覆盖
    page.refresh(announce=True)
    settle()
    assert page.notice.text() == "已刷新"
    page.on_settings_changed()
    assert page.notice.text() == "已刷新"


def test_delivery_panel_real_check_enables_export(qtbot, ready_batch):
    panel = DeliveryPanel()
    qtbot.addWidget(panel)
    panel.set_batch(ready_batch)
    assert not panel.export_btn.isEnabled()
    with qtbot.waitSignal(panel.finished, timeout=30000):
        panel.check()
    assert panel.last_report.ready and panel.export_btn.isEnabled()
