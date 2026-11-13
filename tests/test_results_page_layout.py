"""结果页 B 布局：“核对答卷 / 交付”两种模式，悬浮翻页条，交付清单与批次统计；重排后原有功能都可达。"""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel, QPushButton

from scan_answer.app.settings import AppSettings
from scan_answer.output.jsonfile import read_json
from scan_answer.ui.help_text import CONCEPTS
from scan_answer.ui.pages.results_page import ResultsPage
from scan_answer.ui.widgets.delivery_panel import ISSUE_FALLBACK
from scan_answer.ui.widgets.guide import HelpButton
from conftest import open_batch_held, settle
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
    settle()  # 批次在后台读取
    assert page.batch_title.text() == two.name and "2 份" in page.batch_sub.text()
    assert page.mode_badge.text() == "还没检查" and not page.mode_badge.isHidden()
    assert page.last_check.text() == ""
    # 核对模式：卡面、单项重试和整批转写按钮都在
    assert shown(page, page.card) and shown(page, page.answers)
    assert buttons(page, "重试转写")
    assert shown(page, page.retry_all_btn) and shown(page, page.skip_btn)
    page.mode_buttons["deliver"].click()
    assert page.mode() == "deliver" and page.mode_buttons["deliver"].isChecked()
    panel = page.delivery_panel
    for w in (panel, panel.check_btn, panel.export_btn, panel.help_btn, page.ocr_help, page.summary_note):
        assert shown(page, w)
    assert not shown(page, page.card) and not buttons(page, "重试转写")
    assert not shown(page, page.retry_all_btn) and not shown(page, page.skip_btn)
    assert page.ocr_help.toolTip() == CONCEPTS["ocr"]
    # 检查结论：阻断数、可交付、过期
    panel._show_report(blockers())
    panel.finished.emit()
    assert page.mode_badge.text() == "2 项要处理"
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
    settle()  # 批次在后台读取
    for w in (page.prev_btn, page.pos_label, page.next_btn, page.face_seg):
        assert page.float_bar.isAncestorOf(w) and shown(page, w)
    assert page.pos_label.text() == "未选 / 2 份"
    page.next_btn.click()  # 没选中时从第一份开始
    settle()  # 结果文件在后台读
    assert page.current.seq == 1 and page.pos_label.text() == "第 1 / 2 份"
    page.next_btn.click()
    settle()  # 结果文件在后台读
    assert page.current.seq == 2 and page.pos_label.text() == "第 2 / 2 份"
    page.prev_btn.click()
    settle()  # 结果文件在后台读
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
    assert joined.index("必须先处理 2 项") < joined.index("仅作记录 1 项")
    assert "文字转写尚未完成：第 2 题（a）" in joined and "第 3 题未作答" in joined
    assert "结束批次" in joined and "不再转写剩余项" in joined  # 每类阻断项的处理方法
    # 转写类阻断项：处理按钮就是整批转写的两个动作，可用状态和说明与右栏按钮一致
    retry = buttons(panel.issues, "转写剩余内容")
    skip = buttons(panel.issues, "不再转写剩余项…")
    assert len(retry) == 1 and len(skip) == 1
    assert retry[0].isEnabled() == page.retry_all_btn.isEnabled()
    assert retry[0].toolTip() == page.retry_all_btn.toolTip()
    assert skip[0].isEnabled() == page.skip_btn.isEnabled() and skip[0].toolTip() == page.skip_btn.toolTip()
    retry[0].click()
    assert calls == ["retry"]
    # 结算类阻断项没有现成的处理动作：只写去处，不造按钮
    assert not buttons(panel.issues, "结束批次")
    # 生成交付目录：不能点时写明原因
    assert not panel.export_btn.isEnabled() and "2 项要处理" in panel.export_why.text()
    assert "上方" in panel.export_btn.toolTip()  # 清单在按钮上方，措辞应指向上方
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
    retry = buttons(page.delivery_panel.issues, "转写剩余内容")[0]
    assert not retry.isEnabled() and "设置" in retry.toolTip()  # 没配置大模型
    page.settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page.on_settings_changed()
    assert retry.isEnabled() and retry.toolTip() == page.retry_all_btn.toolTip()


def test_next_step_hint_in_both_modes(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()  # 批次在后台读取
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
    resume = open_batch_held(page, two)
    values = page.summary.values
    assert values["ocr_failed"].text() == "统计中…"  # 转写状态还在后台统计，不显示假数
    resume()
    assert values["total"].text() == "3 份"
    assert values["ok"].text() == "0 份" and values["warn"].text() == "2 份" and values["bad"].text() == "1 份"
    assert values["ocr_failed"].text() == "4 项" and values["ocr_pending"].text() == "0 项"


def test_both_modes_fit_minimum_window(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two, select_seq=1)
    settle()  # 批次在后台读取
    for mode in ("check", "deliver"):
        page.set_mode(mode)
        page.delivery_panel._show_report(blockers())
        hint = page.minimumSizeHint()
        assert hint.width() <= 1280 - 64 and hint.height() <= 800 - 80, (mode, hint)


def test_long_issue_lists_collapse_so_export_stays_in_reach(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()  # 批次在后台读取
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
    settle()  # 批次在后台读取
    page.set_mode("deliver")
    notes = [{"severity": "warning", "code": "recognition_warning", "message": f"识别结果有警告：第 {i} 题未作答",
              "seq": 1, "path": "students/0001/result.json"} for i in range(1, 41)]
    page.delivery_panel._show_report(report(notes))
    buttons(page.delivery_panel.issues, "展开其余 37 项")[0].click()  # 清单比窗口长
    page.activateWindow()
    qtbot.waitUntil(lambda: page.flow_scroll.verticalScrollBar().maximum() > 0)
    bar = page.flow_scroll.verticalScrollBar()
    # 真实按键：焦点在清单滚动区、或清单里的按钮上，PgDn / PgUp 都翻清单，不在看不见的地方翻学生
    for target in (page.flow_scroll, page.delivery_panel.check_btn):
        target.setFocus()
        bar.setValue(0)
        QTest.keyClick(target, Qt.Key.Key_PageDown)
        assert bar.value() > 0 and page.current.seq == 1, target
        QTest.keyClick(target, Qt.Key.Key_PageUp)
        assert bar.value() == 0 and page.current.seq == 1, target


def test_open_batch_returns_to_check_mode_with_selected_card(qtbot, two, app_home):
    """扫描页点拼图格子 / 考试页“查看结果”打开批次：即使上次停在“交付”，也回到核对答卷并显示那一份。"""
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    page.set_mode("deliver")
    assert page.open_batch(two, select_seq=2)
    settle()  # 批次在后台读取
    assert page.mode() == "check" and page.mode_buttons["check"].isChecked()
    assert page.current.seq == 2 and shown(page, page.card)
    page.set_mode("deliver")
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    assert page.mode() == "check"


def test_next_step_hint_names_the_mode_holding_the_control(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()
    page.rows[0].ocr_pending = 1
    manifest = read_json(two / "manifest.json")
    manifest["finalized"] = True
    write_json(two / "manifest.json", manifest)
    page._refresh_manifest()  # manifest 缓存在后台刷新（批次结束时由扫描状态变化触发）
    settle()
    page._show_hint(force=True)
    assert "在“核对答卷”里点“转写剩余内容”" in page.notice.text()
    page.rows[0].status = "physical"
    page._show_hint(force=True)
    assert "在“核对答卷”里点“异常卡”筛选" in page.notice.text()
    page.delivery_panel._show_report(blockers())
    page._show_hint(force=True)
    assert "“交付”里" in page.notice.text() and "在“交付”里重新点“检查交付”" in page.notice.text()


def test_badge_shows_check_failure(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    panel = page.delivery_panel
    panel._finished(panel._generation, ("check", RuntimeError("磁盘读不了"), panel.batch_dir, panel._epoch))
    assert "未完成" in panel.status.text() and page.mode_badge.text() == "检查失败"
    panel._show_report(report([]))
    assert page.mode_badge.text() == "可交付"


def test_unknown_blocker_code_gets_generic_guide(qtbot, two, app_home):
    """没有专门去处的类别：不编造去处，只给通用办法（刷新后再检查，仍有就交给技术支持）；类别名用白话。
    通用处理方法的提示里带原始类别名和代码，方便技术支持定位。"""
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()  # 批次在后台读取
    page.delivery_panel._show_report(report([{"severity": "error", "code": "image_reused", "message": "不同证据共用了图片",
                                              "seq": 1, "path": "x.png"}]))
    labels = [w for w in page.delivery_panel.issues.findChildren(QLabel)]
    texts = [w.text() for w in labels]
    guides = [(w.text(), w.toolTip()) for w in labels if w.text().startswith("处理方法")]
    assert guides[0][0] == "处理方法：" + ISSUE_FALLBACK
    assert "image_reused" in guides[0][1] and "类别" in guides[0][1]
    assert any("不同题目共用了同一张图片" in t for t in texts)
    assert not any("不同证据共用了图片" in t for t in texts)


def test_mode_switch_emits_only_on_change(qtbot):
    from scan_answer.ui.widgets.results_parts import ModeSwitch
    switch = ModeSwitch()
    qtbot.addWidget(switch)
    seen = []
    switch.changed.connect(seen.append)
    switch.buttons["check"].click()
    assert seen == []
    switch.buttons["deliver"].click()
    switch.buttons["deliver"].click()
    assert seen == ["deliver"]


def test_help_buttons_reachable(qtbot):
    page = ResultsPage()
    qtbot.addWidget(page)
    texts = [b.toolTip() for b in page.findChildren(HelpButton)]
    for concept in ("filters", "ocr", "delivery"):
        assert CONCEPTS[concept] in texts, concept


def test_batch_title_not_squeezed_to_subtitle_width(qtbot, two, app_home):
    """批次名不能被下面一行（考试名 · 份数）的宽度卡住：位置够时显示全名，全名始终在提示里。"""
    named = two.rename(two.parent / "batch_20261111_160450")
    page = make_page(qtbot, AppSettings())
    page.resize(1440 - 64, 900 - 80)
    assert page.open_batch(named)
    settle()
    title = page.batch_title
    assert QLabel.text(title) == named.name, (QLabel.text(title), title.width())
    assert title.toolTip() == named.name
    # 刷新按钮仍紧跟在批次名后面
    assert page.refresh_btn.geometry().left() - title.geometry().right() < 40
