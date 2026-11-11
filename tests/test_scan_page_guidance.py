"""扫描页的界面内提示与引导：悬停说明、禁用原因、页面说明条、空状态、情境提示（下一步）。"""
import pytest
from PySide6.QtWidgets import QAbstractButton, QComboBox, QLabel, QLineEdit, QSpinBox, QWidget

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings
from scan_answer.pipeline.events import SheetEvent
from scan_answer.ui.controller import (FINALIZED, FINISHING, IDLE, PAUSED, RELOCATING, RUNNING, STOPPING,
                                       ScanController)
from scan_answer.ui.pages.scan_page import ExceptionItem, ScanPage
from scan_answer.ui.widgets.guide import HelpButton

from conftest import TEMPLATE_DIR
from test_manual_align import _clicks
from test_manual_align_ui import _dialog

CONTROLS = (QAbstractButton, QComboBox, QLineEdit, QSpinBox)


def _missing_tips(root: QWidget) -> list[str]:
    """root 下所有（相对 root）可见的可交互控件里，没有悬停说明的。"""
    missing = []
    for cls in CONTROLS:
        for w in root.findChildren(cls):
            if w.isVisibleTo(root) and not w.toolTip().strip():
                missing.append(f"{type(w).__name__}({getattr(w, 'text', lambda: '')() or w.objectName()})")
    return missing


@pytest.fixture
def ctrl(qtbot):
    c = ScanController(workers=0)
    yield c
    c.shutdown()


@pytest.fixture
def settings(tmp_path):
    return AppSettings(output_root=str(tmp_path / "out"))


@pytest.fixture
def page(qtbot, ctrl, app_home, settings):
    p = ScanPage(ctrl, settings)
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


# ---------- 悬停说明 ----------

@pytest.mark.parametrize("source", ["folder", "scanner"])
def test_every_visible_control_has_tooltip(page, source):
    page._on_source_changed(source)
    assert _missing_tips(page) == []


def test_every_control_has_tooltip_with_exceptions_and_states(batch_page, ctrl):
    p = batch_page
    p._on_done(_ok(1, warnings=2), animate=False)
    p._on_done(_err(2), animate=False)
    p._on_done(_err(3, "WRONG_CARD"), animate=False)
    p._on_done(_err(4, "BLANK_PAGE"), animate=False)
    p._on_voided(4)
    for state in (IDLE, RUNNING, PAUSED, STOPPING, FINISHING, RELOCATING, FINALIZED):
        ctrl.state = state
        p._on_state(state)
        assert _missing_tips(p) == [], state
    ctrl.state = IDLE


def test_segment_options_explain_each_choice(page):
    page._on_source_changed("scanner")
    for seg in (page.duplex, page.dpi, page.color, page.dropout, page.crop):
        tips = [b.toolTip() for b in seg.buttons.values()]
        assert all(tips) and len(set(tips)) == len(tips)  # 每个选项各有说明
    assert "4 倍" in page.dpi.buttons["600"].toolTip()
    page._on_source_changed("folder")
    assert "文件导入时不用设置" in page.dpi.buttons["300"].toolTip()
    assert "文件导入时不用设置" in page.double_feed.toolTip()


@pytest.mark.parametrize("code,raw_ok,voided,expected", [
    ("ALIGN_FAILED", True, False, {"重扫", "重新识别", "手动定位", "作废"}),
    ("ALIGN_FAILED", False, False, {"重扫", "作废"}),
    ("WRONG_CARD", True, False, {"重扫", "确认无误", "手动定位", "作废"}),
    ("WRONG_CARD", False, False, {"重扫", "作废"}),
    ("BLANK_PAGE", True, False, {"重扫", "作废"}),
    ("ALIGN_FAILED", True, True, {"重扫", "恢复"}),
])
def test_exception_item_buttons_have_tooltips(qtbot, code, raw_ok, voided, expected):
    item = ExceptionItem(7, code, "说明", None, raw_ok=raw_ok, voided=voided)
    qtbot.addWidget(item)
    buttons = [b for b in item.findChildren(QAbstractButton) if b.isVisibleTo(item)]
    assert {b.text() for b in buttons} == expected
    assert all(b.toolTip().strip() for b in buttons)
    note = any("原图不完整" in lab.text() for lab in item.findChildren(QLabel))
    assert note == (not raw_ok and not voided and code in ("ALIGN_FAILED", "WRONG_CARD"))


def test_manual_align_dialog_controls_have_tooltips(qtbot, ctx, tmp_path):
    dlg, _images, transforms, _ = _dialog(qtbot, ctx, tmp_path)
    assert _missing_tips(dlg) == []
    # 初始：撤销、清空、重新识别都不能点，提示说明原因
    for b in (dlg.undo_btn, dlg.clear_btn, dlg.ok_btn):
        assert not b.isEnabled() and "现在不能点" in b.toolTip()
    assert "还需要点满" in dlg.ok_btn.toolTip()
    front = _clicks(ctx, "front", transforms[0])
    dlg._place(0, *front[0])
    assert dlg.undo_btn.isEnabled() and "现在不能点" not in dlg.undo_btn.toolTip()
    assert dlg.clear_btn.isEnabled() and "现在不能点" not in dlg.clear_btn.toolTip()
    # 自动定位的图：跳过不能点，并说明原因
    combo = dlg.image_tabs[0].face_combo
    combo.setCurrentIndex(combo.findData(None))
    assert not dlg.skip_btn.isEnabled() and "自动定位" in dlg.skip_btn.toolTip()
    assert _missing_tips(dlg) == []


# ---------- 禁用原因 / 前置步骤 ----------

def test_start_button_says_what_is_missing(page, tmp_path):
    page._on_source_changed("folder")
    assert "考试" in page.main_btn.toolTip()  # 还没选考试
    page.exam = create_exam(tmp_path / "exams", ExamInfo("考试"), TEMPLATE_DIR)
    page._refresh_guidance()
    assert "文件…" in page.main_btn.toolTip() and "文件…" in page.next_step.text()
    page.folder_edit.setText(str(tmp_path))
    assert "先做这一步" not in page.main_btn.toolTip()
    assert "开始识别" in page.next_step.text()


def test_disabled_buttons_explain_why(batch_page, ctrl):
    p = batch_page
    ctrl.state = RUNNING
    p._on_state(RUNNING)
    assert not p.finish_btn.isEnabled() and "现在不能点" in p.finish_btn.toolTip() and "暂停" in p.finish_btn.toolTip()
    assert not p.new_btn.isEnabled() and "结束本轮" in p.new_btn.toolTip()
    assert "结束本轮" in p.src_seg.buttons["folder"].toolTip()
    assert "暂停" in p.main_btn.toolTip()
    ctrl.state = RELOCATING
    p._on_state(RELOCATING)
    assert not p.main_btn.isEnabled() and "手动定位" in p.main_btn.toolTip()
    assert "现在不能点" in p.stop_btn.toolTip()
    ctrl.state = IDLE
    p._on_state(IDLE)
    assert "现在没有正在进行的识别" in p.stop_btn.toolTip()
    p._on_done(_err(1), animate=False)
    assert p.finish_btn.isEnabled() and "1 张物理异常" in p.finish_btn.toolTip()


def test_finish_button_without_batch(page):
    assert not page.finish_btn.isEnabled() and "还没有批次" in page.finish_btn.toolTip()


# ---------- “?” ----------

def test_help_buttons_for_hard_concepts(page):
    concepts = {b.text_html for b in page.findChildren(HelpButton)}
    from scan_answer.ui.help_text import CONCEPTS
    for key in ("batch", "end_round", "exceptions", "dropout", "crop", "scan_warnings"):
        assert CONCEPTS[key] in concepts, key


# ---------- 空状态 ----------

def test_empty_states_tell_next_step(page, tmp_path):
    assert "处理完才能结束批次" in page.exc_empty.text() and not page.exc_empty.isHidden()
    assert "暂无" in page.warn_list.text()
    assert "考试" in page.mosaic_hint.text()  # 没有考试：先去考试页
    assert "开始识别" in page.preview.placeholder
    page.exam = create_exam(tmp_path / "exams", ExamInfo("考试"), TEMPLATE_DIR)
    page._refresh_guidance()
    assert "自动新建批次" in page.mosaic_hint.text()


def test_empty_states_inside_batch(batch_page):
    p = batch_page
    assert "还没有答卷" in p.mosaic_hint.text()
    p._on_done(_ok(1), animate=False)
    assert p.exc_empty.text() == "没有待处理的异常卡。"
    assert not p.void_fold.isHidden() and "已作废 0" in p.void_title.text()
    assert not p.void_empty.isHidden() and "恢复" in p.void_empty.text()  # 展开“已作废”看到的空状态说明
    assert "悬停" in p.mosaic_hint.text()


# ---------- 情境提示 ----------

def test_next_step_follows_batch_progress(batch_page, ctrl):
    p = batch_page
    ctrl.state = RUNNING
    p._on_state(RUNNING)
    p._on_done(_ok(1), animate=False)
    assert "正在识别" in p.next_step.text()
    ctrl.state = IDLE
    p._on_state(IDLE)  # 本轮完成，无异常
    assert "结束批次" in p.next_step.text() and "没有异常卡" in p.next_step.text()
    p._on_done(_err(2), animate=False)
    p._on_done(_err(3), animate=False)
    assert "先处理左栏 2 张异常卡" in p.next_step.text()
    p._on_voided(2)
    assert "先处理左栏 1 张异常卡" in p.next_step.text()
    p._on_voided(3)
    assert "结束批次" in p.next_step.text() and "没有异常卡" in p.next_step.text()
    ctrl.state = PAUSED
    p._on_state(PAUSED)
    assert "继续" in p.next_step.text()
    ctrl.state = FINALIZED
    p._on_state(FINALIZED)
    assert "结果" in p.next_step.text() and "检查交付" in p.next_step.text()
    assert "重新结算" in p.finish_btn.toolTip()
    ctrl.state = IDLE


def test_layout_fits_minimum_window(qtbot, page):
    """1280×800 主窗口里（去掉导航栏和状态栏），中栏内容不被挤出页面。"""
    page.resize(1280 - 64, 800 - 36 - 28)
    page.show()
    qtbot.waitExposed(page)
    for seq in range(1, 201):
        page._on_done(_ok(seq) if seq % 9 else _err(seq), animate=False)
    qtbot.wait(10)
    bottom = page.preview.mapTo(page, page.preview.rect().bottomLeft()).y()
    assert bottom <= page.height() and page.preview.height() >= 150
