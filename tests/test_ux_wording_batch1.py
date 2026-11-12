"""界面易懂性第 1 批：改名后的按钮、提示和两处小的界面补充（属性面板“删除”、画布上的操作结果提示）。"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.pipeline.events import DeviceNotice, FinalizedEvent, SheetEvent
from scan_answer.template.doc import NO_IDENTITY_NOTICE, RegionRef
from scan_answer.ui import help_text
from scan_answer.ui.controller import FINALIZED, IDLE, PAUSED, ScanController
from scan_answer.ui.pages.scan_page import ScanPage

from conftest import TEMPLATE_DIR

B199 = bundled_templates_dir() / "guanzong199"


@pytest.fixture
def ctrl(qtbot):
    c = ScanController(workers=0)
    yield c
    c.shutdown()


@pytest.fixture
def page(qtbot, ctrl, app_home, tmp_path):
    p = ScanPage(ctrl, AppSettings(output_root=str(tmp_path / "out")))
    qtbot.addWidget(p)
    p._timer.stop()
    return p


@pytest.fixture
def batch_page(page, ctrl, tmp_path):
    exam = create_exam(tmp_path / "exams", ExamInfo("期中考试", "英语"), TEMPLATE_DIR)
    page.exam = exam
    ctrl.new_batch(exam)
    page._reset_view()
    page._on_state(IDLE)
    return page


# ---------- 1. 停止导入剩余文件 / 停止进纸 ----------

def test_stop_button_named_by_source_with_permanent_note(page):
    page._on_source_changed("folder")
    assert page.stop_btn.text() == "停止导入剩余文件"
    assert "批次不会结束" in page.stop_note.text() and "导入" in page.stop_note.text()
    page._twain_ok = True  # 没有扫描仪的机器上也能检查扫描仪模式的文字
    page.src_seg.set_value("scanner")
    page._on_source_changed("scanner")
    assert page.stop_btn.text() == "停止进纸"
    assert "批次不会结束" in page.stop_note.text() and "批次不会结束" in page.stop_btn.toolTip()


def test_double_feed_notice_uses_current_button_names(page):
    page._on_device(DeviceNotice("double_feed", "检测到双张进纸，已暂停。若有缺面异常，请先结束本轮", 1))
    text = page.notice.text()
    assert "暂停" in text and "继续" in text and "停止进纸" in text and "结束本轮" not in text


# ---------- 5 / 8. 结束批次：叫法与结果位置 ----------

def test_finish_result_shown_in_finish_block_not_latest_title(batch_page, ctrl):
    p = batch_page
    p._on_done(SheetEvent(1, "s1", True, None, "", "123", 0, False, None), animate=False)
    title = p.prev_title.text()
    ctrl.state = FINALIZED
    p._on_finalized(FinalizedEvent(0.312, 0.14, 3, [1, 2, 3]))
    p._on_state(FINALIZED)
    assert p.prev_title.text() == title  # “最近一张”的标题不动
    text = p.finish_result.text()
    assert p.finish_result.isVisibleTo(p) and "有 3 张卡的答案变了" in text and "结果页" in text
    assert "0.312" not in text and "阈值" not in text and "0.312" in p.finish_result.toolTip()
    assert p.finish_btn.text() == "再次结束批次"
    assert "再次结束批次" in p.finish_btn.toolTip()
    assert any("批次已结束" in t for _ok, t in p._finish_checks())
    assert "结算" not in p.finish_btn.toolTip() + p.next_step.text()
    ctrl.state = IDLE
    p._on_state(IDLE)


def test_finish_result_hidden_when_more_sheets_are_imported(batch_page):
    p = batch_page
    p._on_finalized(FinalizedEvent(0.3, 0.1, 0))
    assert "没有卡的答案变化" in p.finish_result.text()
    p._begin_round()
    assert not p.finish_result.isVisibleTo(p)


# ---------- 9. 涂卡质量提示 ----------

def test_quality_notice_plain_words_without_numbers(page):
    issues = [{"code": "LIGHT_MARKS", "msg": "本批填涂整体偏浅（已涂选项涂黑度中位数 0.32）"},
              {"code": "THRESHOLD_CLAMPED", "msg": "阈值偏高（0.65），已按 0.60 截断"}]
    page._show_threshold_issues(issues)
    text = page.notice.text()
    assert text.startswith("涂卡质量提示：") and "阈值" not in text
    assert "0.32" not in text and "0.65" not in text
    assert "0.32" in page.notice.toolTip()  # 数字只在悬停说明里


# ---------- 6. 另起一批 ----------

def test_new_batch_explains_where_closed_batch_went(batch_page, ctrl):
    p = batch_page
    old = ctrl.session.batch_id
    assert p.new_btn.text() == "另起一批"
    p.new_batch()
    assert ctrl.session is None
    text = p.next_step.text()
    assert f"已关闭批次 {old}" in text and "继续扫描" in text and "开始识别" in text


def test_new_batch_busy_hint_names_stop_button(batch_page, ctrl):
    ctrl.state = PAUSED
    try:
        batch_page._on_state(PAUSED)
        assert "停止导入剩余文件" in batch_page.new_btn.toolTip()
    finally:
        ctrl.state = IDLE


# ---------- 15. 扫描参数两处说法一致 ----------

def test_scan_param_wording_matches_settings_page(qtbot, page, app_home, tmp_path):
    from scan_answer.ui.pages.settings_page import SettingsPage
    settings_page = SettingsPage(AppSettings(output_root=str(tmp_path / "out")))
    qtbot.addWidget(settings_page)
    scan_labels = {lab.text() for lab in page._param_labels}
    for key, label in help_text.SCAN_PARAM_LABELS.items():
        assert label in scan_labels, label
        assert settings_page.field_rows[key].label.text() == label
    for key in ("duplex", "dropout", "crop"):
        a = {k: b.text() for k, b in getattr(page, key).buttons.items()}
        b = {k: b.text() for k, b in getattr(settings_page, key).buttons.items()}
        assert a == b, key
    assert {b.text() for b in page.dropout.buttons.values()} == {"不去掉", "去红色", "去绿色", "去蓝色"}
    assert {b.text() for b in page.crop.buttons.values()} == {"自动找纸边", "按模板尺寸", "扫描仪默认"}
    # 双张检测：两处说法相同，且与实际行为（暂停并提示）一致
    assert page.double_feed.toolTip().startswith(help_text.DOUBLE_FEED_TIP)
    assert settings_page.double_feed.toolTip() == help_text.DOUBLE_FEED_TIP
    assert "暂停" in help_text.DOUBLE_FEED_TIP
    from scan_answer.ui.controller import PAUSE_ON_DEVICE
    assert "double_feed" in PAUSE_ON_DEVICE
    # 旧说法不再出现
    for old in ("进纸", "颜色滤除", "裁边", "超声波", "物理异常"):
        assert old not in " ".join(scan_labels), old
    assert page.param_btn.text() == "修改扫描参数…"
    assert help_text.SCAN_PARAMS_SYNC_NOTE in page.param_btn.toolTip()


def test_scan_page_params_become_settings_defaults(page, app_home):
    """浮层说“会成为设置页里的默认值”：开始扫描时确实写回设置。"""
    page.dropout.set_value("red")
    page.crop.set_value("template")
    page._remember_scan_params(page.scan_config())
    assert page.settings.scan.dropout == "red" and page.settings.scan.crop == "template"


# ---------- 10 / 12 / 13. 模板编辑器 ----------

def _editor(qtbot):
    from scan_answer.ui.pages.template_page import TemplatePage
    p = TemplatePage(AppSettings(template_dirs=[str(bundled_templates_dir())]))
    qtbot.addWidget(p)
    p.resize(1440, 900)
    p.show()
    assert p.open_editor(B199)
    return p


def test_auto_panel_buttons_say_what_they_do(qtbot, app_home):
    p = _editor(qtbot)
    assert p.layout_btn.text() == "重新识别版面…"
    assert p.locate_btn.text() == "找定位 ▾" and p.locate_btn.menu() is not None
    p.canvas.select([])
    assert p.snap_btn.text() == "吸附全部网格" and "全部" in p.snap_btn.toolTip()
    grids = [r for r in p.doc.regions(p.face) if r.kind == "objective"][:2]
    p.canvas.select(grids)
    assert p.snap_btn.text() == "吸附选中的 2 块" and "选中" in p.snap_btn.toolTip()
    p.canvas.select([RegionRef("field", 0)])
    assert p.snap_btn.text() == "吸附选中的网格" and "没有可吸附的网格" in p.snap_btn.toolTip()


def test_props_delete_button_removes_region_and_undo_restores(qtbot, app_home):
    p = _editor(qtbot)
    ref = RegionRef("field", 0)
    count = len(p.doc.template.fields)
    p.canvas.select([ref])
    btn = p.props._btn_delete
    assert btn.isVisibleTo(p) and btn.text() == "删除" and "Delete" in btn.toolTip()
    btn.click()
    assert len(p.doc.template.fields) == count - 1
    assert "已删除 1 个区域" in p.msg_label.text()
    p.undo()
    assert len(p.doc.template.fields) == count
    p.canvas.select([])
    assert not btn.isVisibleTo(p)  # 没选中时不显示
    assert "删除" in p.props._empty.text() and "Delete" in p.props._empty.text()


def test_toast_shows_auto_hides_and_keeps_drawing_banner(qtbot, app_home):
    p = _editor(qtbot)
    c = p.canvas
    p.set_tool("objective")
    assert c.banner.isVisible()
    c.show_toast("已吸附 2 块网格", "ok", ms=150)
    assert c.toast.isVisible() and c.toast.text() == "已吸附 2 块网格"
    assert c.toast.geometry().top() >= c.banner.geometry().bottom()  # 排在横幅下面，不盖住它
    qtbot.waitUntil(lambda: not c.toast.isVisible(), timeout=2000)
    assert c.banner.isVisible() and c.banner.text().startswith("正在添加")
    p.set_tool(None)


def test_operation_result_also_shown_on_canvas(qtbot, app_home):
    p = _editor(qtbot)
    p.canvas.select([RegionRef("field", 0)])
    p.delete_selected()
    assert p.canvas.toast.isVisible() and "已删除 1 个区域" in p.canvas.toast.text()


def test_save_without_identity_states_consequence_prominently(qtbot, app_home, no_dialogs):
    from scan_answer.ui.pages.template_page import NO_IDENTITY_TOAST
    p = _editor(qtbot)
    refs = [r for face in p.doc.template.faces for r in p.doc.regions(face) if r.kind == "identity"]
    if refs:
        p.doc.remove(refs)
        p._after_change(select=[])
    assert NO_IDENTITY_NOTICE in p.doc.notices()
    assert p.save()
    t = p.canvas.toast
    assert t.isVisible() and t.text() == NO_IDENTITY_TOAST and t.property("sticky")
    assert "拿错别科的卡" in t.text()
    QApplication.processEvents()
    assert t.isVisible()  # 常驻，直到下一次修改
    p.canvas.select([RegionRef("field", 0)])
    p.delete_selected()
    assert "已删除" in t.text()
