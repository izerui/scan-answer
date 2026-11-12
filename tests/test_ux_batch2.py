"""界面第二批：定位方式挪进右栏“定位”卡片；整批转写挪到核对答卷右栏。"""
import pytest
from PySide6.QtWidgets import QComboBox, QLabel

from scan_answer.app.settings import AppSettings
from scan_answer.ui.help_text import CONCEPTS
from scan_answer.ui.pages.results_page import ResultsPage
from scan_answer.ui.pages.template_page import TemplatePage
from scan_answer.ui.widgets.guide import HelpButton
from conftest import TEMPLATE_DIR, settle
from test_results_flow_fixes import two, write_json  # noqa: F401
from test_results_page_guidance import make_page
from test_results_page_layout import blockers, buttons, shown
from scan_answer.output.jsonfile import read_json


# ══════════ A. 模板编辑器：定位方式 ══════════
@pytest.fixture
def tpage(qtbot, app_home, no_dialogs):
    widget = TemplatePage(AppSettings())
    qtbot.addWidget(widget)
    widget.resize(1400, 860)
    widget.show()
    assert widget.open_editor(TEMPLATE_DIR)
    return widget


def test_left_panel_only_has_region_list(tpage):
    left = tpage.tree.parentWidget()
    assert not left.findChildren(QComboBox)
    texts = [w.text() for w in tpage.editor_view.findChildren(QLabel)]
    assert "当前面定位方式" not in texts
    assert not hasattr(tpage, "alignment_combo")


def test_locate_card_holds_method_segmented(tpage):
    seg = tpage.alignment_seg
    card = tpage.locate_card
    assert tpage.props_stack.isAncestorOf(card) and card.isAncestorOf(seg)
    assert card.isAncestorOf(tpage.locate_btn) and card.isAncestorOf(tpage.align_status)
    texts = [w.text() for w in card.findChildren(QLabel) if w.isVisibleTo(tpage)]
    assert "定位" in texts and "这一面靠" in texts and "对准" in texts
    helps = [b for b in card.findChildren(HelpButton) if b.isVisibleTo(tpage)]
    assert [b.text_html for b in helps] == [CONCEPTS["anchors"]]
    assert set(seg.buttons) == {"anchors", "frames"}
    assert "定位块" in seg.buttons["anchors"].toolTip() and "外框" in seg.buttons["frames"].toolTip()
    assert seg.isVisibleTo(tpage)


def test_locate_card_segmented_changes_method_undoably(tpage):
    seg = tpage.alignment_seg
    before = tpage.doc.template.faces["front"].align.method
    other = "frames" if before == "anchors" else "anchors"
    assert seg.value() == before
    seg.buttons[other].click()
    assert tpage.doc.template.faces["front"].align.method == other and seg.value() == other
    tpage.undo()
    assert tpage.doc.template.faces["front"].align.method == before and seg.value() == before
    tpage.redo()
    assert tpage.doc.template.faces["front"].align.method == other and seg.value() == other
    # 再点当前项不产生修改
    n = len(tpage.undo_stack._undo)
    seg.buttons[other].click()
    assert len(tpage.undo_stack._undo) == n


def test_locate_card_segmented_follows_face(tpage):
    seg = tpage.alignment_seg
    front = tpage.doc.template.faces["front"].align.method
    back_target = "frames" if front == "anchors" else "anchors"
    tpage.set_face("back")
    if tpage.doc.template.faces["back"].align.method != back_target:
        seg.buttons[back_target].click()
    assert tpage.doc.template.faces["back"].align.method == back_target and seg.value() == back_target
    tpage.set_face("front")
    assert seg.value() == front
    tpage.set_face("back")
    assert seg.value() == back_target


# ══════════ B. 结果页：整批转写挪到核对答卷 ══════════
def test_check_mode_has_batch_ocr_block(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two, select_seq=1)
    assert page.mode() == "check"
    assert shown(page, page.batch_ocr) and page.check_view.isAncestorOf(page.batch_ocr)
    for b in (page.retry_all_btn, page.skip_btn):
        assert shown(page, b) and page.batch_ocr.isAncestorOf(b)
    assert page.batch_ocr_stats.text() == "统计中…"
    settle()
    assert page.batch_ocr_stats.text() == "失败 4 · 等待 0"
    # 旧的页脚说明去掉了
    labels = [w.text() for w in page.check_view.findChildren(QLabel) if w.isVisibleTo(page)]
    assert not any("在“交付”模式里" in t for t in labels)


def test_check_mode_batch_ocr_without_batch(qtbot, app_home):
    page = make_page(qtbot, AppSettings())
    assert shown(page, page.retry_all_btn) and shown(page, page.skip_btn)
    assert page.batch_ocr_stats.text() == "—"


def test_delivery_mode_points_to_check_and_fix_buttons_still_work(qtbot, two, app_home, monkeypatch):
    calls = []
    monkeypatch.setattr(ResultsPage, "_retry_all_failed", lambda self: calls.append("retry"))
    monkeypatch.setattr(ResultsPage, "_skip_pending", lambda self: calls.append("skip"))
    settings = AppSettings()
    settings.ocr = {"base_url": "http://unused", "model": "m", "api_key": "k"}
    page = make_page(qtbot, settings)
    assert page.open_batch(two)
    settle()
    page.rows[0].ocr_pending = 1
    page._update_retry_controls()
    page.set_mode("deliver")
    assert not shown(page, page.retry_all_btn) and not shown(page, page.skip_btn)
    assert shown(page, page.summary.values["ocr_failed"]) and shown(page, page.ocr_help)
    assert shown(page, page.summary_note) and "核对答卷" in page.summary_note.text()
    page.delivery_panel._show_report(blockers())
    retry = buttons(page.delivery_panel.issues, "转写剩余内容")[0]
    skip = buttons(page.delivery_panel.issues, "不再转写剩余项…")[0]
    assert retry.isEnabled() and skip.isEnabled()
    retry.click()
    skip.click()
    assert calls == ["retry", "skip"]
    # 源按钮在另一模式里（看不见）时，状态与说明仍同步
    page.rows[0].ocr_pending = 0
    page._update_retry_controls()
    assert not skip.isEnabled() and skip.toolTip() == page.skip_btn.toolTip() and "没有等待转写" in skip.toolTip()
    page.settings.ocr = None
    page.on_settings_changed()
    assert not retry.isEnabled() and retry.toolTip() == page.retry_all_btn.toolTip()


def test_next_step_hint_names_check_mode_for_batch_ocr(qtbot, two, app_home):
    page = make_page(qtbot, AppSettings())
    assert page.open_batch(two)
    settle()
    page.rows[0].ocr_pending = 1
    manifest = read_json(two / "manifest.json")
    manifest["finalized"] = True
    write_json(two / "manifest.json", manifest)
    page._show_hint(force=True)
    assert "在“核对答卷”里点“转写剩余内容”" in page.notice.text()


# ══════════ 审查修复 ══════════
def test_unknown_align_method_still_opens_editor(qtbot, app_home, no_dialogs, tmp_path):
    import json
    import shutil
    tdir = tmp_path / "tpl"
    shutil.copytree(TEMPLATE_DIR, tdir)
    data = json.loads((tdir / "template.json").read_text(encoding="utf-8"))
    data["faces"]["front"]["align"]["method"] = "none"
    (tdir / "template.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    page = TemplatePage(AppSettings())
    qtbot.addWidget(page)
    assert page.open_editor(tdir)
    seg = page.alignment_seg
    assert not any(b.isChecked() for b in seg.buttons.values()) and seg.value() == ""
    seg.buttons["frames"].click()
    assert page.doc.template.faces["front"].align.method == "frames" and seg.value() == "frames"
    page.undo()
    assert not any(b.isChecked() for b in seg.buttons.values())


def test_check_side_cannot_collapse(qtbot, app_home):
    page = make_page(qtbot, AppSettings())
    split = page.side.parentWidget()
    i = split.indexOf(page.side)
    assert i >= 0 and not split.isCollapsible(i)
    assert all(not split.isCollapsible(j) for j in range(split.count()))


def test_align_status_says_anchor_blocks(tpage):
    text = tpage.align_status.text()
    assert "正面定位块 " in text and " 个" in text and "定位点" not in text
    assert tpage.align_label.text() == text


def test_many_warnings_keep_card_large(qtbot, two, app_home):
    from test_results_flow_fixes import make_result
    result = make_result()
    result["warnings"] = [{"code": "OBJ_BLANK", "field": f"objective.{i}"} for i in range(1, 48)]
    write_json(two / "students" / "0001" / "result.json", result)
    page = make_page(qtbot, AppSettings())
    page.resize(1376, 820)
    page.show()
    assert page.open_batch(two, select_seq=1)
    settle()
    assert "第 47 题" in page.anomalies.text()
    avail = page.card_tabs.height()
    assert page.card.height() >= avail * 0.5, (page.card.height(), avail)
    assert page.anomaly_scroll.height() <= 5 * 22 + 12
    # 悬浮翻页条不压住警告列表
    bar_top = page.float_bar.mapTo(page, page.float_bar.rect().topLeft()).y()
    warn_bottom = page.anomaly_scroll.mapTo(page, page.anomaly_scroll.rect().bottomLeft()).y()
    assert warn_bottom <= bar_top
