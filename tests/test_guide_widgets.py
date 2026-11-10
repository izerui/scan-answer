"""界面内引导的公共件：侧栏页面说明、“?”按钮、使用说明窗口与 F1。"""
from PySide6.QtWidgets import QTextBrowser

from scan_answer.ui.help_text import CONCEPTS, GUIDE_MD, PAGE_GUIDES
from scan_answer.ui.widgets.guide import HelpButton, HelpDialog, with_help

from test_ui_pages import make_window


def test_every_page_has_guide_text():
    for key in ("exam", "scan", "template", "results", "settings"):
        title, steps = PAGE_GUIDES[key]
        assert title and len(steps) >= 3
    assert all(CONCEPTS.values())


def test_help_button_and_with_help(qtbot):
    b = HelpButton("snap")
    qtbot.addWidget(b)
    assert b.toolTip() == CONCEPTS["snap"]
    b.show_help()  # 点击立即显示，不抛错
    row = with_help(b.__class__("trial"), "identity")
    qtbot.addWidget(row)
    assert len(row.findChildren(HelpButton)) == 2


def test_help_dialog_and_f1(qtbot, app_home, tmp_path, monkeypatch):
    dlg = HelpDialog()
    qtbot.addWidget(dlg)
    assert "整体流程" in dlg.browser.toPlainText() and GUIDE_MD.startswith("# 使用说明")
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        opened = []
        monkeypatch.setattr(HelpDialog, "exec", lambda self: opened.append(self.findChild(QTextBrowser)) or 0)
        win.show_help()
        assert opened and opened[0] is not None
        for key, button in win.rail_buttons.items():
            title, steps = PAGE_GUIDES[key]
            assert title in button.toolTip() and steps[0] in button.toolTip()
    finally:
        ctrl.shutdown()
