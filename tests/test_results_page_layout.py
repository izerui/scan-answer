"""结果页 B 布局：“核对答卷 / 交付”两种模式，悬浮翻页条，交付清单与批次统计；重排后原有功能都可达。"""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton

from scan_answer.app.settings import AppSettings
from scan_answer.output.jsonfile import read_json
from scan_answer.ui.help_text import CONCEPTS
from scan_answer.ui.pages.results_page import ResultsPage
from scan_answer.ui.widgets.guide import HelpButton
from conftest import settle
from test_results_flow_fixes import report, two, write_json  # noqa: F401
from test_results_page_guidance import assert_all_tooltips, make_page, visible_controls


def shown(page, widget) -> bool:
    return widget.isVisibleTo(page)


def buttons(root, text):
    return [b for b in visible_controls(root) if isinstance(b, QPushButton) and b.text() == text]


def blockers():
    return report([
        {"severity": "error", "code": "ocr_pending", "message": "文字转写尚未完成：第 2 题（a）",
         "seq": 2, "path": "students/0002/result.json"},
        {"severity": "error", "code": "not_finalized", "message": "批次尚未完成结算，请先结束批次",
         "seq": None, "path": "manifest.json"},
        {"severity": "warning", "code": "recognition_warning", "message": "识别结果有警告：第 3 题未作答",
         "seq": 1, "path": "students/0001/result.json"},
    ])


def test_no_batch_empty_state(qtbot, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.mode() == "check"
    assert page.batch_title.text() == "未打开批次"
    assert page.title.text() == "未选择批次"
    assert page.mode_badge.isHidden()  # 没有批次：不显示检查结论
    assert page.notice.isHidden() and page.hint_bar.isHidden()
    assert page.pos_label.text() == "—"
    for btn in (page.prev_btn, page.next_btn):
        assert not btn.isEnabled() and "批次" in btn.toolTip()
    page.set_mode("deliver")
    panel = page.delivery_panel
    assert shown(page, panel) and "打开批次后" in panel.status.text()
    assert not panel.export_btn.isEnabled() and "批次" in panel.export_why.text()
    assert page.summary.values["total"].text() == "—"
    assert_all_tooltips(page)


def test_mode_switch_and_badge_follow_delivery_check(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two, select_seq=1)
    assert page.batch_title.text() == two.name and "2 份" in page.batch_sub.text()
    assert page.mode_badge.text() == "还没检查" and not page.mode_badge.isHidden()
    assert page.last_check.text() == ""
    # 核对模式：卡面、单项重试在；整批转写按钮在交付模式
    assert shown(page, page.card) and shown(page, page.answers)
    assert buttons(page, "重试转写")
    assert not shown(page, page.retry_all_btn) and not shown(page, page.skip_btn)
    page.mode_buttons["deliver"].click()
    assert page.mode() == "deliver" and page.mode_buttons["deliver"].isChecked()
    panel = page.delivery_panel
    for w in (panel, panel.check_btn, panel.export_btn, panel.help_btn, page.retry_all_btn, page.skip_btn,
              page.ocr_help):
        assert shown(page, w)
    assert not shown(page, page.card) and not buttons(page, "重试转写")
    assert page.ocr_help.toolTip() == CONCEPTS["ocr"]
    # 检查结论：阻断数、可交付、过期
    panel._show_report(blockers())
    panel.finished.emit()
    assert page.mode_badge.text() == "2 项阻断"
    assert "10:00" in page.last_check.text()
    panel._show_report(report([]))
    assert page.mode_badge.text() == "可交付"
    panel.mark_stale()
    assert "重新检查" in page.mode_badge.text() and "过期" in page.last_check.text()
    page.mode_buttons["check"].click()
    assert page.mode() == "check" and shown(page, page.card)


def test_float_bar_paging_face_and_keys(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    for w in (page.prev_btn, page.pos_label, page.next_btn, page.face_seg):
        assert page.float_bar.isAncestorOf(w) and shown(page, w)
    assert page.pos_label.text() == "未选 / 2 份"
    page.next_btn.click()  # 没选中时从第一份开始
    assert page.current.seq == 1 and page.pos_label.text() == "第 1 / 2 份"
    page.next_btn.click()
    assert page.current.seq == 2 and page.pos_label.text() == "第 2 / 2 份"
    page.prev_btn.click()
    assert page.current.seq == 1
    # PgDn / PgUp：焦点在卡面、列表时都翻份
    page.card.setFocus()
    QTest.keyClick(page.card, Qt.Key.Key_PageDown)
    assert page.current.seq == 2
    QTest.keyClick(page.table, Qt.Key.Key_PageUp)
    assert page.current.seq == 1
    page.face_seg.buttons["back"].click()
    assert page.face == "back"
    page.face_seg.buttons["front"].click()
    assert page.face == "front"


def test_delivery_checklist_items_and_fix_actions(qtbot, two, app_home, monkeypatch):
    calls = []
    monkeypatch.setattr(ResultsPage, "_retry_all_failed", lambda self: calls.append("retry"))
    monkeypatch.setattr(ResultsPage, "_skip_pending", lambda self: calls.append("skip"))
    settings = AppSettings()
    settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page = make_page(qtbot, settings)
    assert page.open_batch(two)
    settle()
    page.set_mode("deliver")
    panel = page.delivery_panel
    panel._show_report(blockers())
    panel.finished.emit()
    texts = [w.text() for w in panel.issues.findChildren(QLabel) if w.isVisibleTo(page)]
    joined = "\n".join(texts)
    assert joined.index("阻断项 2") < joined.index("说明项 1")
    assert "文字转写尚未完成：第 2 题（a）" in joined and "第 3 题未作答" in joined
    assert "结束批次" in joined and "放弃未完成转写" in joined  # 每类阻断项的处理方法
    # 转写类阻断项：处理按钮就是整批转写的两个动作，可用状态和说明与右栏按钮一致
    retry = buttons(panel.issues, "补做和重试转写")
    skip = buttons(panel.issues, "放弃未完成转写…")
    assert len(retry) == 1 and len(skip) == 1
    assert retry[0].isEnabled() == page.retry_all_btn.isEnabled()
    assert retry[0].toolTip() == page.retry_all_btn.toolTip()
    assert skip[0].isEnabled() == page.skip_btn.isEnabled() and skip[0].toolTip() == page.skip_btn.toolTip()
    retry[0].click()
    assert calls == ["retry"]
    # 结算类阻断项没有现成的处理动作：只写去处，不造按钮
    assert not buttons(panel.issues, "结束批次")
    # 生成交付目录：不能点时写明原因
    assert not panel.export_btn.isEnabled() and "2 项阻断" in panel.export_why.text()
    assert_all_tooltips(page)
    # 到核对里看：切回核对模式并选中那一份（即使当前筛选里看不到它）
    page.filter.set_value("err")
    page._refill()
    jump = buttons(panel.issues, "到核对里看 #0002")
    assert jump
    jump[0].click()
    assert page.mode() == "check" and page.current.seq == 2


def test_fix_buttons_follow_retry_state_changes(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()
    page.set_mode("deliver")
    page.delivery_panel._show_report(blockers())
    retry = buttons(page.delivery_panel.issues, "补做和重试转写")[0]
    assert not retry.isEnabled() and "设置" in retry.toolTip()  # 没配置大模型
    page.settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page.on_settings_changed()
    assert retry.isEnabled() and retry.toolTip() == page.retry_all_btn.toolTip()


def test_next_step_hint_in_both_modes(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    assert shown(page, page.hint_bar) and "结束批次" in page.notice.text()
    page.set_mode("deliver")
    assert shown(page, page.hint_bar) and "结束批次" in page.notice.text()
    page.refresh(announce=True)
    settle()
    assert page.notice.text() == "已刷新" and shown(page, page.hint_bar)


def test_batch_summary_counts_from_rows(qtbot, two, app_home):
    manifest = read_json(two / "manifest.json")
    manifest["sheets"].append({"seq": 3, "status": "voided", "warnings": 0, "source": "s3.jpg"})
    write_json(two / "manifest.json", manifest)
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    values = page.summary.values
    assert values["ocr_failed"].text() == "统计中…"  # 转写状态还在后台统计，不显示假数
    settle()
    assert values["total"].text() == "3 份"
    assert values["ok"].text() == "0 份" and values["warn"].text() == "2 份" and values["bad"].text() == "1 份"
    assert values["ocr_failed"].text() == "4 项" and values["ocr_pending"].text() == "0 项"


def test_both_modes_fit_minimum_window(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two, select_seq=1)
    for mode in ("check", "deliver"):
        page.set_mode(mode)
        page.delivery_panel._show_report(blockers())
        hint = page.minimumSizeHint()
        assert hint.width() <= 1280 - 64 and hint.height() <= 800 - 80, (mode, hint)


def test_long_issue_lists_collapse_so_export_stays_in_reach(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    page.set_mode("deliver")
    notes = [{"severity": "warning", "code": "recognition_warning", "message": f"识别结果有警告：第 {i} 题未作答",
              "seq": 1, "path": "students/0001/result.json"} for i in range(1, 13)]
    page.delivery_panel._show_report(report(notes))
    issues = page.delivery_panel.issues
    rows = [w for w in issues.findChildren(QLabel) if w.text().startswith("识别结果有警告")]
    assert sum(w.isVisibleTo(page) for w in rows) == 3
    more = buttons(issues, "展开其余 9 项")
    assert more and more[0].toolTip()
    more[0].click()
    assert all(w.isVisibleTo(page) for w in rows) and buttons(issues, "收起")


def test_page_keys_in_delivery_mode_scroll_the_checklist(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two, select_seq=1)
    page.set_mode("deliver")
    page.delivery_panel._show_report(blockers())
    bar = page.flow_scroll.verticalScrollBar()
    bar.setRange(0, 1000)  # 模拟清单比窗口长
    page._page_key(1)
    assert bar.value() > 0 and page.current.seq == 1  # 不在看不见的地方翻学生


def test_help_buttons_reachable(qtbot):
    page = ResultsPage()
    qtbot.addWidget(page)
    texts = [b.toolTip() for b in page.findChildren(HelpButton)]
    for concept in ("filters", "ocr", "delivery"):
        assert CONCEPTS[concept] in texts, concept
