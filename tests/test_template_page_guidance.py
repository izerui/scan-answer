"""模板页的界面内引导：说明条、悬停说明、“?”按钮、空状态和状态行的下一步提示。"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QDoubleSpinBox, QLabel, QLineEdit, QPushButton, QSpinBox, QWidget

from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.template.doc import RegionRef, TemplateDoc

B199 = bundled_templates_dir() / "guanzong199"
INPUTS = (QAbstractButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox)


def _page(qtbot, size=(1400, 860)):
    from scan_answer.ui.pages.template_page import TemplatePage
    page = TemplatePage(AppSettings(template_dirs=[str(bundled_templates_dir())]))
    qtbot.addWidget(page)
    page.resize(*size)
    page.show()
    return page


def _missing_tips(root: QWidget) -> list[str]:
    """root 下所有可见的交互控件中没有悬停说明的（数值框内部的输入框不算）。"""
    missing = []
    for w in root.findChildren(QWidget):
        if not isinstance(w, INPUTS) or not w.isVisibleTo(root):
            continue
        if isinstance(w, QLineEdit) and isinstance(w.parent(), (QSpinBox, QDoubleSpinBox, QComboBox)):
            continue
        if not w.toolTip().strip():
            text = w.text() if hasattr(w, "text") else ""
            missing.append(f"{type(w).__name__}({text!r})")
    return missing


def test_list_view_tooltips(qtbot, app_home):
    page = _page(qtbot)
    assert _missing_tips(page.list_view) == []
    # 未选中模板时“编辑”禁用并说明原因
    page.table.clearSelection()
    page.table.setCurrentCell(-1, -1)
    page._update_list_buttons()
    assert not page.edit_btn.isEnabled() and "选中" in page.edit_btn.toolTip()
    page.table.selectRow(0)
    assert page.edit_btn.isEnabled()


def test_editor_guide_text_covers_flow_and_canvas_keys():
    from scan_answer.ui.help_text import CONCEPTS, GUIDE_MD
    text = GUIDE_MD.split("## 模板编辑器", 1)[1]
    for word in ("吸附", "试识别", "保存", "滚轮", "空格", "方向键", "Delete", "Esc"):
        assert word in text, word
    assert "regions" in CONCEPTS


def test_empty_list_text(qtbot, app_home, tmp_path, monkeypatch):
    from scan_answer.ui.pages import template_page
    monkeypatch.setattr(template_page, "list_templates", lambda _dirs: [])
    page = _page(qtbot)
    hint = page.table._empty_hint.label
    assert page.table.rowCount() == 0 and hint.isVisibleTo(page) and "新建模板" in hint.text()


def test_editor_controls_all_have_tooltips(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    assert _missing_tips(page.editor_view) == []
    # 画框工具列：选择 + 每种区域类型各一个按钮，都有说明
    from scan_answer.ui.pages.template_page import TOOL_KINDS
    assert list(page.tool_btns)[0] is None and set(page.tool_btns) == {None, *TOOL_KINDS}
    assert all(b.toolTip() for b in page.tool_btns.values())
    # 自动定位菜单的三项都有说明
    assert [a.text() for a in page.locate_btn.menu().actions()] == ["自动找定位点", "自动检测外框", "自动识别同步头"]
    assert page.locate_btn.menu().toolTipsVisible() and all(a.toolTip() for a in page.auto_btns.values())
    for i in range(page.alignment_combo.count()):
        assert page.alignment_combo.itemData(i, Qt.ItemDataRole.ToolTipRole)
    # 旋转菜单显示每项的说明
    assert page.rotate_btn.menu().toolTipsVisible()
    assert all(a.toolTip() for a in page.rotate_btn.menu().actions() if not a.isSeparator())
    # 难懂概念旁的“?”
    from scan_answer.ui.help_text import CONCEPTS
    from scan_answer.ui.widgets.guide import HelpButton
    shown = {b.text_html for b in page.editor_view.findChildren(HelpButton) if b.isVisibleTo(page)}
    for concept in ("regions", "snap", "trial", "subjective_target", "anchors"):
        assert CONCEPTS[concept] in shown, concept
    # 撤销 / 重做禁用时说明原因
    assert not page.undo_btn.isEnabled() and "还没有可撤销" in page.undo_btn.toolTip()
    assert not page.redo_btn.isEnabled() and "撤销之后" in page.redo_btn.toolTip()
    assert not page.rotate_reset.isEnabled() and "没有旋转" in page.rotate_reset.toolTip()


def test_tool_dock_is_exclusive(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    page.tool_btns["objective"].click()
    assert page.canvas._tool == "objective"
    assert [k for k, b in page.tool_btns.items() if b.isChecked()] == ["objective"]
    # 画布上按 Esc 回到选择
    qtbot.keyClick(page.canvas, Qt.Key.Key_Escape)
    assert page.canvas._tool is None
    assert [k for k, b in page.tool_btns.items() if b.isChecked()] == [None]


def test_tool_dock_keyboard(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    btns = page.tool_btns
    page.activateWindow()
    page.tree.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(page.tree.hasFocus)

    def press(key):
        qtbot.keyClick(QApplication.focusWidget(), key)

    # Tab 从区域树进入工具列，停在当前工具
    press(Qt.Key.Key_Tab)
    assert btns[None].hasFocus()
    # ↓ 在工具之间移动并选中
    press(Qt.Key.Key_Down)
    press(Qt.Key.Key_Down)
    assert btns["objective"].hasFocus() and page.canvas._tool == "objective"
    # 焦点在工具按钮上时 Esc 回到选择
    press(Qt.Key.Key_Escape)
    assert page.canvas._tool is None and btns[None].isChecked()
    # 空格确认后焦点交给画布，画布上 Esc 回到选择
    press(Qt.Key.Key_Down)
    press(Qt.Key.Key_Space)
    assert page.canvas.hasFocus() and page.canvas._tool is not None
    press(Qt.Key.Key_Escape)
    assert page.canvas._tool is None
    # Tab 进入工具列总是落在当前工具上：↓ 到考号、Esc 回选择、鼠标点条码，再从区域树 Tab
    page.tree.setFocus(Qt.FocusReason.OtherFocusReason)
    press(Qt.Key.Key_Tab)
    assert btns[None].hasFocus()
    press(Qt.Key.Key_Down)
    assert btns["exam_no"].hasFocus() and page.canvas._tool == "exam_no"
    press(Qt.Key.Key_Escape)
    assert page.canvas._tool is None
    page.tree.setFocus(Qt.FocusReason.OtherFocusReason)
    press(Qt.Key.Key_Tab)
    assert btns[None].hasFocus()
    qtbot.mouseClick(btns["barcode"], Qt.MouseButton.LeftButton)
    assert page.canvas._tool == "barcode"
    page.tree.setFocus(Qt.FocusReason.OtherFocusReason)
    press(Qt.Key.Key_Tab)
    assert btns["barcode"].hasFocus()


def test_view_bar_leaves_scrollbar_clickable(qtbot, app_home):
    page = _page(qtbot, size=(1280, 800))
    assert page.open_editor(B199)
    qtbot.wait(50)
    page.fit()
    page.canvas.set_zoom(page._fit_zoom * 3)
    page._update_zoom_label()
    assert page.zoom_btn.text() == "300%"
    hsb = page.canvas.horizontalScrollBar()
    qtbot.waitUntil(lambda: hsb.isVisible())
    QApplication.processEvents()
    bar = page.editor_view.findChild(QWidget, "viewBar")

    def page_rect(w):
        return w.rect().translated(w.mapTo(page, w.rect().topLeft()))

    assert not page_rect(bar).intersects(page_rect(hsb))
    # 滚动条中间点到的是滚动条本身，不是视图条或它的透明外框
    for x in (hsb.width() // 4, hsb.width() // 2, hsb.width() * 3 // 4):
        hit = page.childAt(hsb.mapTo(page, QPoint(x, hsb.height() // 2)))
        assert hit is hsb or hsb.isAncestorOf(hit), hit
    # 视图条仍在卡面可视区下方、滚动条上方
    vp = page_rect(page.canvas.viewport())
    assert page_rect(bar).top() >= vp.bottom() and page_rect(bar).bottom() < page_rect(hsb).top()


def test_view_bar_floats_on_canvas(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    bar = page.editor_view.findChild(QWidget, "viewBar")
    for w in (page.face_seg, page.zoom_btn, page.rotate_btn):
        assert bar.isAncestorOf(w)
    stage = page.canvas.parentWidget()
    assert stage.isAncestorOf(bar) and bar.isVisibleTo(page)


def test_fit_keeps_paper_above_view_bar(qtbot, app_home):
    page = _page(qtbot, size=(1800, 620))  # 宽而矮：卡面按高度撑满
    assert page.open_editor(B199)
    qtbot.wait(50)
    page.fit()
    bar = page.editor_view.findChild(QWidget, "viewBar")
    paper = page.canvas.mapFromScene(page.canvas.sceneRect()).boundingRect()
    paper_bottom = page.canvas.viewport().mapTo(page, paper.bottomLeft()).y()
    assert paper_bottom <= bar.mapTo(page, bar.rect().topLeft()).y()


def test_auto_cards_follow_trial_and_face(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    cards = page.editor_view.findChild(QWidget, "autoPanel")
    for w in (page.layout_btn, page.locate_btn, page.snap_btn):
        assert cards.isAncestorOf(w)
    assert cards.isVisibleTo(page) and page.props.isVisibleTo(page)
    assert "正面定位点 4" in page.align_status.text()
    page.props_stack.setCurrentIndex(1)  # 试识别中
    assert not cards.isVisibleTo(page) and page.exit_trial_btn.isVisibleTo(page)
    page.exit_trial()
    assert cards.isVisibleTo(page)
    page.set_face("back")
    assert page.align_status.text().startswith("背面")


def test_trial_panel_tooltips_and_empty_text(qtbot, app_home):
    page = _page(qtbot)
    assert page.open_editor(B199)
    page.props_stack.setCurrentIndex(1)
    assert page.exit_trial_btn.isVisibleTo(page) and page.exit_trial_btn.toolTip()
    assert page.trial_list.toolTip()
    assert page.trial_list.count() == 0 and "试识别" in page.trial_empty.text()
    assert page.trial_empty.isVisibleTo(page)
    page.exit_trial()
    page._set_trial_btn(False)
    assert "正在试识别" in page.trial_btn.toolTip()
    page._set_trial_btn(True)


def _all_kind_refs(page) -> dict[str, tuple[str, RegionRef]]:
    doc = page.doc
    for kind, rect in (("omr_field", (20.0, 20.0, 30.0, 30.0)), ("choose_one", (60.0, 20.0, 20.0, 8.0))):
        if not any(r.kind == kind for r in doc.regions("front")):
            doc.add(kind, "front", rect)
    refs: dict[str, tuple[str, RegionRef]] = {}
    for face in doc.template.faces:
        for ref in doc.regions(face):
            refs.setdefault(ref.kind, (face, ref))
    return refs


def test_props_panel_tooltips_for_every_kind(qtbot, app_home):
    from scan_answer.ui.help_text import CONCEPTS
    from scan_answer.ui.widgets.guide import HelpButton
    page = _page(qtbot)
    assert page.open_editor(B199)
    # 未选中时说明怎么用
    assert page.props._empty.isVisibleTo(page) and "点选画布上的区域查看和修改属性" in page.props._empty.text()
    refs = _all_kind_refs(page)
    assert {"objective", "exam_no", "omr_field", "subjective", "choose_one", "field", "barcode", "absent",
            "anchor", "frame", "sync", "ignore", "identity"} <= set(refs)
    concept_of = {"identity": "identity", "ignore": "ignore", "anchor": "anchors", "frame": "anchors"}
    for kind, (face, ref) in refs.items():
        page.set_face(face)
        page.select_refs([ref])
        assert page.props.target() == ref, kind
        assert _missing_tips(page.props) == [], kind
        if kind in concept_of:
            helps = [b for b in page.props.findChildren(HelpButton) if b.isVisibleTo(page.props)]
            assert CONCEPTS[concept_of[kind]] in {b.text_html for b in helps}, kind
    # 主观题区域列表：上移 / 下移禁用时说明原因
    face, ref = refs["subjective"]
    page.set_face(face)
    page.select_refs([ref])
    from PySide6.QtWidgets import QPushButton
    moves = [b for b in page.props.findChildren(QPushButton) if b.accessibleName() in ("区域上移", "区域下移")]
    assert moves and all(b.toolTip() for b in moves)
    assert any(not b.isEnabled() and "已经是" in b.toolTip() for b in moves)
    # 取消选中后回到空状态
    page.select_refs([])
    assert page.props._empty.isVisibleTo(page)


def test_region_tree_empty_state(qtbot, app_home, tpl_bases):
    from scan_answer.template.auto import ImportedBase
    t, bases = tpl_bases
    page = _page(qtbot)
    doc = TemplateDoc.new("单面", {"front": ImportedBase(bases["front"], t.paper, 0.0, "anchors")})
    page._enter(doc, "新建", fresh=True)
    # 新建后状态行提示下一步：先核对定位块，再画区域
    assert "下一步" in page.msg_label.text() and "定位块" in page.msg_label.text()
    # 单面模板“背面”按钮禁用并说明原因
    back = page.face_seg.buttons["back"]
    assert not back.isEnabled() and "单面" in back.toolTip()
    assert not page.tree.empty_label.isVisible()  # 已自动找到定位块
    page._remove(list(doc.regions("front")))
    assert doc.regions("front") == []
    assert page.tree.empty_label.isVisible()
    assert "先确认定位" in page.tree.empty_label.text() and "添加区域" in page.tree.empty_label.text()


def test_status_line_next_steps(qtbot, app_home, tmp_path):
    import shutil
    d = tmp_path / "tpl"
    shutil.copytree(B199, d)
    page = _page(qtbot)
    assert page.open_editor(d)
    # 画完客观题提示自动吸附
    page.set_tool("objective")
    page._on_region_drawn("objective", (30.0, 200.0, 40.0, 20.0))
    assert "自动吸附" in page.msg_label.text()
    # 没试识别就保存：提醒但不阻止
    assert page.save() is True
    assert "已保存到" in page.msg_label.text() and "试识别" in page.msg_label.text()
    # 试识别过且之后没改：不再提醒
    page.doc.rename("改个名")
    page._tried_revision = (page.doc, page.doc.revision)
    assert page.save() is True
    assert "已保存到" in page.msg_label.text() and "建议先" not in page.msg_label.text()


def test_layout_fits_minimum_window(qtbot, app_home):
    """主窗口最小 1280×800：去掉左侧导航（64）和顶栏 / 状态栏（36 + 28）后模板页要放得下。"""
    page = _page(qtbot, size=(1280 - 64, 800 - 36 - 28))
    assert page.open_editor(B199)
    hint = page.minimumSizeHint()
    assert hint.width() <= 1280 - 64 and hint.height() <= 800 - 36 - 28, hint
    qtbot.wait(10)
    for b in page.editor_view.findChildren(QAbstractButton):
        if b.isVisibleTo(page) and b.text() and b.minimumWidth() != b.maximumWidth():  # 固定宽度的不算
            assert b.width() >= b.minimumSizeHint().width() - 1, b.text()


def test_guide_keys_exist():
    from scan_answer.ui.help_text import PAGE_GUIDES
    assert PAGE_GUIDES["template"][1]


def test_new_dialog_inputs_have_tooltips(qtbot, app_home):
    from scan_answer.ui.editor.new_dialog import NewTemplateDialog
    dlg = NewTemplateDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    assert _missing_tips(dlg) == []


def test_back_to_list_is_easy_to_find(qtbot, app_home, tmp_path):
    """“‹ 模板列表”在编辑器第一行最左边；在编辑器里再点左侧“模板”也回到列表。"""
    from test_ui_pages import make_window
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        page = win.template_page
        win.show_page("template")
        assert page.open_editor(B199)
        bar = page.back_btn.parentWidget()
        first = min((w for w in bar.findChildren(QPushButton) if w.isVisibleTo(bar)),
                    key=lambda w: (w.mapTo(bar, w.rect().topLeft()).y(), w.mapTo(bar, w.rect().topLeft()).x()))
        assert first is page.back_btn and "模板列表" in page.back_btn.text()
        win.show_page("template")  # 编辑器里再点“模板”
        assert page.doc is None and page.stack.currentWidget() is page.list_view
    finally:
        ctrl.shutdown()


def test_delete_not_triggered_from_view_bar(qtbot, app_home):
    """view-bar Delete/Backspace must not delete selected region (regression d2a40c2)."""
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QKeyEvent

    page = _page(qtbot)
    assert page.open_editor(B199)
    refs = page.doc.regions(page.face)
    total = len(refs)
    assert total > 0
    page.canvas.select([refs[0]])
    assert page.canvas.selected() == [refs[0]]

    bar = page.canvas._bottom_bar
    btn = page.face_seg.buttons["front"]
    assert bar.isAncestorOf(btn)

    # Focus the view-bar button and simulate key propagation reaching canvas
    btn.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(btn.hasFocus)
    for key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
        evt = QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(page.canvas, evt)
    assert len(page.doc.regions(page.face)) == total, "Delete/Backspace on view-bar must not remove region"

    # Canvas focus: Delete must still work
    page.canvas.setFocus(Qt.FocusReason.OtherFocusReason)
    qtbot.waitUntil(page.canvas.hasFocus)
    evt = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(page.canvas, evt)
    assert len(page.doc.regions(page.face)) == total - 1, "Delete on canvas must remove selected region"


def test_add_region_entry_is_obvious(qtbot, app_home):
    """添加区域的入口要一眼能找到：列标题、操作说明、选中类型后画布顶部醒目提示。"""
    page = _page(qtbot)
    assert page.open_editor(B199)
    dock = page.editor_view.findChild(QWidget, "toolDock")
    texts = [w.text() for w in dock.findChildren(QLabel)]
    assert "添加区域" in texts
    assert any("拖" in t and "框" in t for t in texts), texts
    banner = page.canvas.banner
    assert not banner.isVisibleTo(page)
    page.tool_btns["objective"].click()
    assert banner.isVisibleTo(page)
    assert "正在添加" in banner.text() and "客观题" in banner.text() and "Esc" in banner.text()
    # 提示条不挡鼠标：照样能从它下面开始拖框
    assert banner.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert page.canvas.viewport().geometry().contains(banner.geometry().center())
    page.set_tool(None)
    assert not banner.isVisibleTo(page)


def test_empty_template_shows_add_region_guide(qtbot, app_home, tpl_bases):
    from scan_answer.template.auto import ImportedBase
    t, bases = tpl_bases
    page = _page(qtbot)
    doc = TemplateDoc.new("单面", {"front": ImportedBase(bases["front"], t.paper, 0.0, "anchors")})
    page._enter(doc, "新建", fresh=True)
    guide = page.canvas.guide
    # 新建时自动识别出了题目区域：此时不显示引导
    assert any(r.kind not in ("anchor", "frame", "sync") for r in doc.regions("front"))
    assert not guide.isVisibleTo(page)
    # 删到只剩定位块、没有任何题目区域：画布中央给出怎么添加的引导
    page._remove([r for r in doc.regions("front") if r.kind not in ("anchor", "frame", "sync")])
    assert guide.isVisibleTo(page) and "添加区域" in guide.text() and "识别版面" in guide.text()
    assert guide.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    doc.add("objective", "front", (20.0, 20.0, 60.0, 30.0))
    page._after_change()
    assert not guide.isVisibleTo(page)
