"""扫描页布局（方案 C）：左栏管批次、中栏看进度、右栏管本轮。控件位置、折叠行、参数浮层、结束批次前置检查。"""
import pytest
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QLabel, QVBoxLayout, QWidget

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings
from scan_answer.pipeline.events import SheetEvent
from scan_answer.ui.controller import (BUSY_STATES, FINALIZED, FINISHING, IDLE, PAUSED, RELOCATING, REPROCESSING,
                                       RESCANNING, RUNNING, STOPPING, ScanController)
from scan_answer.ui.help_text import CONCEPTS
from scan_answer.ui.pages.scan_page import ExceptionItem, ScanPage
from scan_answer.ui.widgets.guide import HelpButton

from conftest import TEMPLATE_DIR
from test_scan_page_guidance import _missing_tips

ALL_STATES = (IDLE, RUNNING, PAUSED, STOPPING, FINISHING, RESCANNING, RELOCATING, REPROCESSING, FINALIZED)


@pytest.fixture
def ctrl(qtbot):
    c = ScanController(workers=0)
    yield c
    c.state = IDLE
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


def _ok(seq, warnings=0):
    return SheetEvent(seq, f"s{seq}", True, None, "", "123", warnings, False, None)


def _err(seq, code="ALIGN_FAILED"):
    return SheetEvent(seq, f"s{seq}", False, code, "没能找到定位块", None, 0, False, None)


def _set_state(page, ctrl, state):
    ctrl.state = state
    page._on_state(state)


def _rect(page, w):
    return QRect(w.mapTo(page, QPoint()), w.size())


# ---------- 三栏的归属 ----------

def _show(page):
    page.resize(1440 - 64, 900 - 64)
    page.show()


def test_batch_controls_live_in_left_column(page):
    _show(page)
    left = page.batch_panel
    for w in (page.new_btn, page.batch_label, page.state_chip, page.meta_label, page.exc_header,
              page.void_fold, page.warn_fold, page.finish_btn, page.finish_checks):
        assert left.isAncestorOf(w), w
    # 新批次在左栏头，结束批次在左栏底
    assert _rect(page, page.new_btn).top() < _rect(page, page.exc_header).top()
    assert _rect(page, page.finish_btn).top() > _rect(page, page.warn_fold).top()
    helps = {b.text_html for b in left.findChildren(HelpButton)}
    for key in ("batch", "exceptions", "scan_warnings", "finalize"):
        assert CONCEPTS[key] in helps, key


def test_round_controls_live_in_right_column(page):
    _show(page)
    right = page.round_panel
    for w in (page.src_seg, page.scanner_box, page.folder_note, page.folder_box, page.param_summary,
              page.param_btn, page.template_label, page.out_label, page.main_btn, page.stop_btn, page.round_label):
        assert right.isAncestorOf(w), w
    assert _rect(page, page.main_btn).top() > _rect(page, page.out_label).top()
    assert _rect(page, page.stop_btn).top() > _rect(page, page.main_btn).top()
    assert CONCEPTS["end_round"] in {b.text_html for b in right.findChildren(HelpButton)}
    texts = " ".join(lab.text() for lab in right.findChildren(QLabel))
    assert "空格键 暂停 / 继续" in texts and "停止和结束批次的区别" in texts
    assert "批次不会结束" in page.stop_note.text() and right.isAncestorOf(page.stop_note)


def test_progress_stays_in_middle_and_region_legend_below_card(page):
    mid = page.progress_panel
    for w in (page.next_step, page.notice, page.t_scanned, page.mosaic, page.expand_btn, page.mosaic_hint,
              page.preview, page.region_legend):
        assert mid.isAncestorOf(w), w
    _show(page)
    assert _rect(page, page.region_legend).top() >= _rect(page, page.preview).bottom()
    doc = QTextDocument()
    doc.setHtml(page.region_legend.text())
    text = doc.toPlainText().replace("\xa0", " ").replace("\u2060", "")
    for label in ("考号", "客观题", "主观题", "条码", "定位点 / 同步头", "信息栏 / 缺考", "异常卡", "有警告"):
        assert label in text, label


def test_batch_identity_in_left_header(batch_page, ctrl):
    p = batch_page
    assert p.batch_label.text() == ctrl.session.batch_id
    assert "模板" in p.meta_label.text() and "期中考试" in p.meta_label.text()
    p.new_batch()
    assert p.batch_label.text() == "未开始批次"


# ---------- 结束批次（结算）前置检查 ----------

def _checks(page):
    return [(ok, text) for ok, text in page._finish_checks()]


def test_finish_checks_without_batch(page):
    checks = _checks(page)
    assert any(not ok and "还没有批次" in t for ok, t in checks)
    assert not page.finish_btn.isEnabled()
    shown = page.finish_checks.text()
    assert "还没有批次" in shown


def test_finish_checks_list_exceptions_running_and_warnings(batch_page, ctrl):
    p = batch_page
    p._on_done(_ok(1, warnings=2), animate=False)
    p._on_done(_err(2), animate=False)
    _set_state(p, ctrl, RUNNING)
    checks = _checks(p)
    assert (False, "异常卡还有 1 张未处理") in checks
    assert any(not ok and "还在识别" in t for ok, t in checks)
    assert any(ok and "警告 2 条不拦截" in t for ok, t in checks)
    assert not p.finish_btn.isEnabled()
    assert "异常卡还有 1 张未处理" in p.finish_checks.text()
    _set_state(p, ctrl, IDLE)
    p._on_voided(2)
    checks = _checks(p)
    assert all(ok for ok, _ in checks) and p.finish_btn.isEnabled()


@pytest.mark.parametrize("state", ALL_STATES)
def test_finish_state_check_agrees_with_button(batch_page, ctrl, state):
    """状态这一项打 ✕ 当且仅当按钮不能点（异常只在点击时拦下，与原逻辑一致，不禁用按钮）。"""
    p = batch_page
    p._on_done(_err(1), animate=False)
    _set_state(p, ctrl, state)
    checks = p._finish_checks()
    assert len(checks) == 3 and checks[0][1].startswith("异常卡")  # 异常 / 状态 / 警告
    assert checks[1][0] == p.finish_btn.isEnabled()
    assert p.finish_btn.isEnabled() == (state in (IDLE, PAUSED, FINALIZED))  # 启用条件与原逻辑一致
    assert p.finish_btn.isEnabled() or "现在不能点" in p.finish_btn.toolTip()


def test_finish_with_pending_exceptions_still_refused(batch_page, ctrl, no_dialogs):
    p = batch_page
    p._on_done(_err(1), animate=False)
    assert p.finish_btn.isEnabled()
    p.finish_btn.click()
    assert ctrl.state == IDLE and "异常卡未处理" in no_dialogs[-1][1]


# ---------- 折叠行：已作废 / 数据警告 ----------

def test_void_fold_expands_and_items_work(batch_page, ctrl, monkeypatch):
    p = batch_page
    p._on_done(_err(1), animate=False)
    p._on_voided(1)
    assert not p.void_fold.isHidden()
    assert "已作废 1" in p.void_title.text()
    assert not p.void_fold.is_open() and p.void_fold.body.isHidden()
    p.void_fold.head.click()
    assert p.void_fold.is_open() and not p.void_fold.body.isHidden()
    item = p.void_box.itemAt(0).widget()
    assert isinstance(item, ExceptionItem) and item.voided and p.void_fold.body.isAncestorOf(item)
    calls = []
    monkeypatch.setattr(ctrl, "unvoid", calls.append)
    next(b for b in item.findChildren(QAbstractButton) if b.text() == "恢复").click()
    assert calls == [1]
    p.void_fold.head.click()
    assert not p.void_fold.is_open() and p.void_fold.body.isHidden()


def test_void_fold_empty_state(batch_page):
    p = batch_page
    assert p.void_fold.isHidden()  # 还没有卡：不显示
    p._on_done(_ok(1), animate=False)
    assert not p.void_fold.isHidden() and "已作废 0" in p.void_title.text()
    assert "恢复" in p.void_empty.text() and not p.void_empty.isHidden()


def test_warn_fold_expands_and_lists_cards(batch_page):
    p = batch_page
    p._on_done(_ok(1, warnings=2), animate=False)
    p._on_done(_ok(2, warnings=1), animate=False)
    assert "警告 3" in p.warn_title.text()
    assert "2 张卡" in p.warn_note.text() and "不拦截" in p.warn_note.text()
    assert not p.warn_fold.is_open()
    p.warn_fold.head.click()
    assert p.warn_fold.is_open() and p.warn_fold.body.isAncestorOf(p.warn_list)
    assert "#0001" in p.warn_list.text() and "#0002" in p.warn_list.text()


def test_fold_heads_have_tooltips(batch_page):
    p = batch_page
    p._on_done(_ok(1), animate=False)
    for fold in (p.void_fold, p.warn_fold):
        assert fold.head.toolTip().strip()


# ---------- “修改…”参数浮层 ----------

def test_param_summary_only_for_scanner(page):
    page._on_source_changed("scanner")
    assert not page.param_summary.isHidden()
    page._on_source_changed("folder")
    assert page.param_summary.isHidden()


def test_param_popover_holds_every_parameter(qtbot, page):
    page._on_source_changed("scanner")
    pop = page.param_pop
    for w in (page.duplex, page.dpi, page.color, page.double_feed, page.dropout, page.crop, page.dpi_note):
        assert pop.isAncestorOf(w), w
    helps = {b.text_html for b in pop.findChildren(HelpButton)}
    assert CONCEPTS["dropout"] in helps and CONCEPTS["crop"] in helps
    page.show()
    page.param_btn.click()
    assert pop.isVisible()
    assert _missing_tips(pop) == []
    pop.hide()


def test_param_changes_take_effect_and_update_summary(page):
    page._on_source_changed("scanner")
    page.param_btn.click()
    page.dpi.buttons["600"].click()
    assert page.scan_config().dpi == 600
    assert not page.dpi_note.isHidden() and "4 倍" in page.dpi_note.text()
    assert "600 DPI" in page.param_summary_text.text()
    page.duplex.buttons["0"].click()
    page.color.buttons["color"].click()
    page.dropout.buttons["red"].click()
    page.crop.buttons["template"].click()
    page.double_feed.click()
    cfg = page.scan_config()
    assert (cfg.duplex, cfg.color, cfg.dropout, cfg.crop, cfg.double_feed) == (False, "color", "red", "template",
                                                                                 not page.settings.scan.double_feed)
    summary = page.param_summary_text.text() + page.param_summary_sub.text()
    assert "单面" in summary and "彩色" in summary and "红" in summary and "按模板" in summary
    page.param_pop.hide()


def test_applied_defaults_refresh_summary(page):
    page.settings.scan.dpi = 400
    page.apply_scan_defaults()
    assert page.dpi.value() == "400" and "400 DPI" in page.param_summary_text.text()


# ---------- 右栏底：开始 / 暂停 / 继续 / 结束本轮 ----------

def test_space_key_pauses_and_resumes(batch_page, ctrl, monkeypatch):
    p = batch_page
    calls = []
    monkeypatch.setattr(ctrl, "pause", lambda: calls.append("pause"))
    monkeypatch.setattr(ctrl, "resume", lambda: calls.append("resume"))
    _set_state(p, ctrl, RUNNING)
    assert p.main_btn.text() == "暂停"
    QTest.keyClick(p, Qt.Key.Key_Space)
    _set_state(p, ctrl, PAUSED)
    assert p.main_btn.text() == "继续"
    QTest.keyClick(p, Qt.Key.Key_Space)
    assert calls == ["pause", "resume"]


def test_round_label_counts_this_round(batch_page, ctrl):
    p = batch_page
    assert "还没开始" in p.round_label.text()
    p._begin_round()
    _set_state(p, ctrl, RUNNING)
    p._on_queued(1, "a")
    p._on_queued(2, "b")
    assert "本轮已读入 2 张" in p.round_label.text()
    _set_state(p, ctrl, RESCANNING)
    p._on_queued(3, "c")  # 重扫不算本轮读入
    assert "本轮已读入 2 张" in p.round_label.text()
    _set_state(p, ctrl, IDLE)
    p.new_batch()
    assert "还没开始" in p.round_label.text()


@pytest.mark.parametrize("state", ALL_STATES)
def test_every_control_has_tooltip_in_each_state(batch_page, ctrl, state):
    p = batch_page
    p._on_done(_ok(1, warnings=1), animate=False)
    p._on_done(_err(2, "WRONG_CARD"), animate=False)
    p._on_done(_err(3), animate=False)
    p._on_voided(3)
    p.void_fold.set_open(True)
    p.warn_fold.set_open(True)
    for source in ("folder", "scanner"):
        if state not in BUSY_STATES:
            p._on_source_changed(source)
        _set_state(p, ctrl, state)
        assert _missing_tips(p) == [], (state, source)


@pytest.mark.parametrize("size", [(1216, 736), (1376, 836)])
def test_columns_fit_window(qtbot, ctrl, app_home, tmp_path, size):
    page = ScanPage(ctrl, AppSettings(output_root=str(tmp_path / "out")))
    page._timer.stop()
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(page)
    host.setFixedSize(*size)
    qtbot.addWidget(host)
    host.show()
    qtbot.waitExposed(host)
    for w in (page.finish_btn, page.main_btn, page.stop_btn, page.new_btn, page.src_seg, page.region_legend):
        r = _rect(page, w)
        assert r.bottom() <= page.height() and r.right() <= page.width(), w
        assert w.visibleRegion().boundingRect().height() >= w.height() - 1, w


def test_param_popover_takes_focus_and_returns_it(qtbot, batch_page, ctrl, monkeypatch):
    p = batch_page
    p._on_source_changed("scanner")
    p.resize(1376, 836)
    p.show()
    qtbot.waitExposed(p)
    p.param_btn.setFocus()
    p.param_btn.click()
    qtbot.waitUntil(lambda: p.param_pop.isVisible())
    focused = p.param_pop.focusWidget()
    assert focused is p.duplex.buttons[p.duplex.value()] and p.param_pop.isAncestorOf(focused)
    # 浮层开着时空格作用在参数上，不会暂停 / 继续
    calls = []
    monkeypatch.setattr(ctrl, "pause", lambda: calls.append("pause"))
    _set_state(p, ctrl, RUNNING)
    QTest.keyClick(focused, Qt.Key.Key_Space)
    assert calls == []
    QTest.keyClick(p.param_pop, Qt.Key.Key_Escape)
    assert not p.param_pop.isVisible()
    assert p.focusWidget() is p.param_btn


def test_summary_shows_600_dpi_note(page):
    page._on_source_changed("scanner")
    assert page.param_summary_note.isHidden()
    page.dpi.buttons["600"].click()
    assert not page.param_summary_note.isHidden() and "4 倍" in page.param_summary_note.text()
    page.dpi.buttons["300"].click()
    assert page.param_summary_note.isHidden()


def test_finish_checks_escape_text(batch_page, monkeypatch):
    p = batch_page
    p._finish_items = [(False, "<b>a & b</b>")]
    p._render_finish_checks()
    assert "&lt;b&gt;a &amp; b&lt;/b&gt;" in p.finish_checks.text()


def test_exception_list_keeps_room_with_both_folds_open(qtbot, batch_page, ctrl):
    """1280×800 下两个折叠行都展开：物理异常列表仍至少能放下约两张卡（≥ 250px），折叠行内容自己滚动。"""
    from PySide6.QtWidgets import QScrollArea
    p = batch_page
    for seq in range(1, 9):
        p._on_done(_ok(seq, warnings=2), animate=False)
    for seq in range(9, 15):
        p._on_done(_err(seq), animate=False)
    for seq in (9, 10, 11):
        p._on_voided(seq)
    p.resize(1280 - 64, 800 - 64)
    p.show()
    qtbot.waitExposed(p)
    p.void_fold.set_open(True)
    p.warn_fold.set_open(True)
    qtbot.wait(10)
    exc_scroll = p.exc_scroll
    assert isinstance(exc_scroll, QScrollArea)
    assert exc_scroll.height() >= 250, exc_scroll.height()
    for fold in (p.void_fold, p.warn_fold):
        assert fold.body.isVisible() and fold.body.height() > 0
    assert _rect(p, p.finish_btn).bottom() <= p.height()


def _fill_left(p, phys):
    """8 张带警告的成功卡 + 3 张作废卡 + phys 张未处理的物理异常；两个折叠行都展开。"""
    for seq in range(1, 9):
        p._on_done(_ok(seq, warnings=2), animate=False)
    for seq in range(9, 12):
        p._on_done(_err(seq), animate=False)
        p._on_voided(seq)
    for seq in range(12, 12 + phys):
        p._on_done(_err(seq), animate=False)
    p.void_fold.set_open(True)
    p.warn_fold.set_open(True)


def _show_min_window(qtbot, p):
    p.resize(1280 - 64, 800 - 64)  # 1280×800 主窗口去掉导航栏（64）、顶栏和状态栏（36 + 28）
    p.show()
    qtbot.waitExposed(p)
    qtbot.wait(10)


def test_folds_get_room_when_no_exceptions(qtbot, batch_page):
    """没有物理异常时异常区只占空状态文字的高度，两个展开的折叠行分到剩余高度（每个至少约一张作废卡高）。"""
    p = batch_page
    _fill_left(p, 0)
    _show_min_window(qtbot, p)
    sa = p.exc_scroll
    exc_area = p.exc_empty.height() + (sa.height() if sa.isVisible() else 0)
    assert exc_area <= 80, exc_area
    for fold in (p.void_fold, p.warn_fold):
        assert fold.body.height() >= 70, fold.body.height()
    r = _rect(p, p.finish_btn)
    assert r.bottom() <= p.height() and p.finish_btn.visibleRegion().boundingRect().height() >= p.finish_btn.height() - 1


def test_exception_list_keeps_priority_when_exceptions_exist(qtbot, batch_page):
    p = batch_page
    _fill_left(p, 6)
    _show_min_window(qtbot, p)
    assert p.exc_scroll.height() >= 250, p.exc_scroll.height()
    assert p.exc_empty.isHidden()
    for fold in (p.void_fold, p.warn_fold):
        assert fold.body.isVisible() and fold.body.height() > 0
    r = _rect(p, p.finish_btn)
    assert r.bottom() <= p.height() and p.finish_btn.visibleRegion().boundingRect().height() >= p.finish_btn.height() - 1
    # 异常处理完后折叠行重新分到高度
    for seq in range(12, 18):
        p._on_voided(seq)
    qtbot.wait(10)
    for fold in (p.void_fold, p.warn_fold):
        assert fold.body.height() >= 70, fold.body.height()


@pytest.mark.parametrize("phys", [0, 6])
def test_scan_page_minimum_height_leaves_slack(qtbot, batch_page, phys):
    """扫描页最小高度不能顶到 1280×800 的边：主窗口要求的最小高度 ≤ 770（页面 ≤ 770 - 64）。"""
    p = batch_page
    _fill_left(p, phys)
    _show_min_window(qtbot, p)
    assert p.minimumSizeHint().height() <= 770 - 64, p.minimumSizeHint().height()


@pytest.mark.parametrize("phys", [0, 6])
def test_main_window_minimum_height_leaves_slack(qtbot, app_home, tmp_path, phys):
    """主窗口最小 1280×800：扫描页两个折叠行都展开时，窗口要求的最小高度仍留出余量（≤ 770）。"""
    from scan_answer.ui.main_window import MainWindow
    c = ScanController(workers=0)
    win = MainWindow(AppSettings(output_root=str(tmp_path / "out"), workers=0), c)
    qtbot.addWidget(win)
    try:
        p = win.scan_page
        p._timer.stop()
        exam = create_exam(tmp_path / "exams", ExamInfo("期中考试", "英语"), TEMPLATE_DIR)
        p.exam = exam
        c.new_batch(exam)
        p._reset_view()
        p._on_state(IDLE)
        _fill_left(p, phys)
        assert win.minimumSizeHint().height() <= 770, win.minimumSizeHint().height()
    finally:
        c.state = IDLE
        c.shutdown()
