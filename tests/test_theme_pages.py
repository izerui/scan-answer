"""Operational pages recolor existing views without reloading their data."""
import copy
import json
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPoint, QRect, Qt, QThreadPool
from PySide6.QtGui import QTextDocument
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QLabel, QLineEdit, QSpinBox, QScrollArea, QVBoxLayout, QWidget

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings
from scan_answer.pipeline.events import SheetEvent
from scan_answer.ui import theme
from scan_answer.ui.controller import FINISHING, IDLE, PAUSED, RUNNING, ScanController
from scan_answer.ui.pages.exam_page import ExamPage
from scan_answer.ui.pages.results_page import OVERLAY_COLOR, ResultsPage
from scan_answer.ui.pages.scan_page import ScanPage
from scan_answer.ui.pages.settings_page import SettingsPage

from conftest import TEMPLATE_DIR, settle


@pytest.fixture
def manager(qapp):
    manager = theme.apply_theme(qapp)
    assert manager is not None, "apply_theme must return the live theme manager"
    manager.apply_scheme(Qt.ColorScheme.Light)
    yield manager
    manager.apply_scheme(Qt.ColorScheme.Light)


def forbid_reload(*args, **kwargs):
    pytest.fail("A theme change must not reload data or change the workflow")


def plain_text(label):
    document = QTextDocument()
    document.setHtml(label.text())
    return document.toPlainText()


@pytest.mark.parametrize(("level", "ink", "background"), [
    ("error", "ERR", "ERR_BG"), ("warn", "WARN", "WARN_BG"), ("info", "INK2", "NOTICE"),
])
def test_scan_notices_and_state_keep_workflow_and_pending_defaults(
        qtbot, manager, tmp_path, monkeypatch, level, ink, background):
    ctrl = ScanController(workers=0)
    page = ScanPage(ctrl, AppSettings(output_root=str(tmp_path)))
    qtbot.addWidget(page)
    page._timer.stop()
    try:
        page._on_done(SheetEvent(1, "source", True, None, "", "123", 2, False, None), animate=False)
        page._on_done(SheetEvent(2, "source", False, "CUSTOM_PHYSICAL", "", None, 0, False, None), animate=False)
        exception = page.exc_box.itemAt(0).widget()
        reason = next(w for w in exception.findChildren(QLabel) if w.text() == "CUSTOM_PHYSICAL")
        page._show_notice("Device notice", level)
        page.folder_edit.setText("unsaved-input.pdf")
        page._on_state(PAUSED)
        pending = copy.deepcopy(page.settings.scan)
        pending.dpi = 600
        page._pending_defaults = pending
        events = dict(page.events)
        cells = dict(page.mosaic.cells)
        warning_text = plain_text(page.warn_list)
        light_notice = page.notice.styleSheet()
        light_chip = page.state_chip.styleSheet()
        light_counter = page.t_err.value.styleSheet()
        buttons = [(b.text(), b.isEnabled()) for b in (page.main_btn, page.finish_btn, page.new_btn)]
        with monkeypatch.context() as guard:
            for name in ("_on_state", "_reset_view", "_refresh_counts", "_apply_defaults", "_show_latest"):
                guard.setattr(page, name, forbid_reload)
            guard.setattr(Path, "read_text", forbid_reload)
            manager.apply_scheme(Qt.ColorScheme.Dark)
            assert page.notice.styleSheet() != light_notice
            assert theme.color(ink) in page.notice.styleSheet()
            assert theme.color(background) in page.notice.styleSheet()
            assert page.state_chip.styleSheet() != light_chip
            assert theme.color("WARN") in page.state_chip.styleSheet()
            assert page.t_err.value.styleSheet() != light_counter
            assert theme.color("ERR") in page.t_err.value.styleSheet()
            assert theme.color("ERR") in reason.styleSheet()
            assert page.exc_box.itemAt(0).widget() is exception
            assert theme.color("WARN") in page.warn_list.text()
            assert plain_text(page.warn_list) == warning_text
            assert page.notice.text() == "Device notice" and not page.notice.isHidden()
            assert page.events == events and page.mosaic.cells == cells
            assert page._pending_defaults is pending and page.dpi.value() == "300"
            assert page.folder_edit.text() == "unsaved-input.pdf"
            assert [(b.text(), b.isEnabled()) for b in (page.main_btn, page.finish_btn, page.new_btn)] == buttons
            manager.apply_scheme(Qt.ColorScheme.Light)
            assert page.notice.styleSheet() == light_notice
            assert page.state_chip.styleSheet() == light_chip
    finally:
        ctrl.shutdown()


@pytest.mark.parametrize(("state", "role"), [(RUNNING, "SUCCESS"), (FINISHING, "ACCENT"), (IDLE, "INK3")])
def test_scan_state_chip_created_in_dark_uses_readable_role(qtbot, manager, state, role):
    manager.apply_scheme(Qt.ColorScheme.Dark)
    ctrl = ScanController(workers=0)
    page = ScanPage(ctrl, AppSettings())
    qtbot.addWidget(page)
    page._timer.stop()
    try:
        page._on_state(state)
        assert theme.color(role) in page.state_chip.styleSheet()
        light_text = page.state_chip.text()
        manager.apply_scheme(Qt.ColorScheme.Light)
        assert theme.color(role) in page.state_chip.styleSheet()
        assert page.state_chip.text() == light_text
    finally:
        ctrl.shutdown()


@pytest.mark.parametrize("success", [True, False])
def test_settings_recolors_connection_result_without_loading_unsaved_inputs(
        qtbot, manager, tmp_path, monkeypatch, success):
    settings = AppSettings(output_root=str(tmp_path))
    original = copy.deepcopy(settings)
    page = SettingsPage(settings)
    qtbot.addWidget(page)
    page.api_key.setText("unsaved-key")
    page.api_key.setSelection(2, 4)
    page.operator.setText("Unsaved operator")
    page.output_root.setText(str(tmp_path / "unsaved"))
    page.workers.setValue(3)
    page.dpi.set_value("600")
    page._ping_done(success, "Connection result")
    inputs = [(w, w.text(), w.selectionStart(), w.selectedText()) for w in page.findChildren(QLineEdit)]
    numbers = [(w, w.value()) for w in page.findChildren(QSpinBox)]
    headings = [w for w in page.findChildren(QLabel) if "border-bottom" in w.styleSheet()]
    light_styles = [w.styleSheet() for w in headings]
    light_result = page.test_result.styleSheet()
    with monkeypatch.context() as guard:
        guard.setattr(page, "load", forbid_reload)
        guard.setattr(page, "save", forbid_reload)
        guard.setattr(Path, "read_text", forbid_reload)
        manager.apply_scheme(Qt.ColorScheme.Dark)
        assert page.test_result.styleSheet() != light_result
        assert theme.color("SUCCESS" if success else "ERR") in page.test_result.styleSheet()
        assert all(theme.color("LINE") in w.styleSheet() for w in headings)
        assert [w.styleSheet() for w in headings] != light_styles
        assert all((w.text(), w.selectionStart(), w.selectedText()) == (text, start, selected)
                   for w, text, start, selected in inputs)
        assert all(w.value() == value for w, value in numbers)
        assert page.dpi.value() == "600"
        assert page.test_result.text() == "Connection result"
        assert settings == original


def test_exam_existing_error_cell_recolors_without_rebuilding_rows(qtbot, manager, tmp_path, monkeypatch):
    exam = create_exam(tmp_path, ExamInfo("Theme exam"), TEMPLATE_DIR)
    batch = exam.directory / "batch_1"
    batch.mkdir()
    (batch / "manifest.json").write_text(json.dumps({"counts": {"total": 2, "physical": 1, "ok": 1}}))
    page = ExamPage(AppSettings(output_root=str(tmp_path), template_dirs=[]))
    qtbot.addWidget(page)
    QThreadPool.globalInstance().waitForDone()  # 等后台模板检查读完文件，再禁止读取
    page.f_name.setText("Unsaved exam")
    item = page.exam_table.item(0, 7)
    light_color = item.foreground().color()
    reset = QSignalSpy(page.exam_table.model().modelReset)
    selection = QSignalSpy(page.exam_table.itemSelectionChanged)
    batch_item = page.batch_table.item(0, 0)
    with monkeypatch.context() as guard:
        guard.setattr(page, "refresh", forbid_reload)
        guard.setattr(page, "_exam_selected", forbid_reload)
        guard.setattr(Path, "read_text", forbid_reload)
        manager.apply_scheme(Qt.ColorScheme.Dark)
        assert item.foreground().color().name() == theme.color("ERR")
        assert item.foreground().color() != light_color
        assert page.exam_table.item(0, 7) is item and page.batch_table.item(0, 0) is batch_item
        assert page.exam_table.currentRow() == 0 and page.batch_table.currentRow() == 0
        assert page.f_name.text() == "Unsaved exam"
        assert reset.count() == selection.count() == 0


@pytest.fixture
def result_batch(tmp_path):
    result = {
        "pages": {"front": "front.png"},
        "student": {},
        "warnings": [{"code": "CUSTOM", "msg": "Keep this warning"}],
        "objective": [{"no": "1", "answer": "", "status": "blank", "fill": {"A": 0.0}}],
        "choose_one": [{"key": "choice", "chosen": [], "status": "failed", "fill": {}}],
        "subjective": [{"no": "2", "images": [], "ocr_status": "failed", "text": None}],
    }
    path = tmp_path / "students" / "0001" / "result.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(result))
    Image.new("L", (61, 83), 180).save(path.parent / "front.png")
    (tmp_path / "template.json").write_text((TEMPLATE_DIR / "template.json").read_text())
    (tmp_path / "manifest.json").write_text(json.dumps({"sheets": [
        {"seq": 1, "status": "ok", "warnings": 1, "source": "exam.pdf",
         "result": "students/0001/result.json"},
        {"seq": 2, "status": "physical", "source": "exam.pdf", "physical": "BLANK_PAGE"},
        {"seq": 3, "status": "voided", "source": "exam.pdf"},
    ]}))
    return tmp_path


def test_results_existing_warnings_recolor_without_selection_or_evidence_reset(
        qtbot, manager, result_batch, monkeypatch):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(result_batch, select_seq=1)
    settle()  # 卡面图片在后台解码
    page.answers.selectRow(1)
    page.card_tabs.setCurrentWidget(page.evidence)
    page.card._auto_fit = False
    page.card.scale(1.7, 1.7)
    transform = page.card.transform()
    pixmap = page.card._pix
    pixels = pixmap.pixmap().toImage()
    result, current = page.result, page.current
    text = plain_text(page.anomalies)
    warning = page.model.item(0, 2)
    physical = page.model.item(1, 0)
    voided = page.model.item(2, 0)
    answer = page.answers.item(1, 2)
    light_warning = warning.foreground().color()
    light_answer = answer.background().color()
    reset = QSignalSpy(page.model.modelReset)
    removed = QSignalSpy(page.model.rowsRemoved)
    selection = QSignalSpy(page.table.selectionModel().selectionChanged)
    answer_selection = QSignalSpy(page.answers.itemSelectionChanged)
    subject = next(w for w in page.findChildren(QLabel) if w.text() == "转写失败" and w.objectName() != "kvKey")
    subject_style = subject.styleSheet()
    overlay_colors = dict(OVERLAY_COLOR)
    with monkeypatch.context() as guard:
        for name in ("open_batch", "_refill", "_show", "_show_card", "_fill_subjective", "_select_row"):
            guard.setattr(page, name, forbid_reload)
        guard.setattr(Path, "read_text", forbid_reload)
        guard.setattr(page.card, "set_image", forbid_reload)
        manager.apply_scheme(Qt.ColorScheme.Dark)
        assert warning.foreground().color().name() == theme.color("WARN")
        assert physical.foreground().color().name() == theme.color("ERR")
        assert voided.foreground().color().name() == theme.color("INK3")
        assert warning.foreground().color() != light_warning
        assert answer.background().color().name() == theme.color("ERR_BG")
        assert answer.background().color() != light_answer
        assert answer.foreground().color().name() == theme.color("ERR")
        assert theme.color("ERR") in page.anomalies.text() and plain_text(page.anomalies) == text
        assert theme.color("HEADER") in subject.styleSheet() and subject.styleSheet() != subject_style
        assert page.model.item(0, 2) is warning and page.answers.item(1, 2) is answer
        assert page.current is current and page.result is result
        assert page.table.currentIndex().row() == 0 and page.answers.currentRow() == 1
        assert reset.count() == removed.count() == selection.count() == answer_selection.count() == 0
        assert page.card_tabs.currentWidget() is page.evidence
        assert page.card._pix is pixmap and pixmap.pixmap().toImage() == pixels
        assert page.card.transform() == transform
        assert OVERLAY_COLOR == overlay_colors
        manager.apply_scheme(Qt.ColorScheme.Light)
        assert answer.background().color() == light_answer
        assert warning.foreground().color() == light_warning


@pytest.mark.parametrize("clear", ["filter", "physical"])
def test_results_clear_detail_does_not_restore_cached_warnings_on_theme_switch(
        qtbot, manager, result_batch, clear):
    page = ResultsPage()
    qtbot.addWidget(page)
    assert page.open_batch(result_batch, select_seq=1)
    if clear == "filter":
        page.search.setText("no matching student")
    else:
        assert page.select_seq(2)
    manager.apply_scheme(Qt.ColorScheme.Dark)
    assert not page.anomalies.text()
    assert page.answers.rowCount() == 0
    assert page.current is None if clear == "filter" else page.current.seq == 2


@pytest.mark.parametrize("scheme", [Qt.ColorScheme.Light, Qt.ColorScheme.Dark])
def test_settings_form_remains_readable_at_supported_small_window(qtbot, qapp, manager, tmp_path, scheme):
    manager.apply_scheme(scheme)
    page = SettingsPage(AppSettings(output_root=str(tmp_path)))
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)
    host.setFixedSize(1216, 736)
    qtbot.addWidget(host)
    host.show()
    qapp.processEvents()
    for control in (page.dpi.buttons["300"], page.base_url, page.api_key, page.output_root, page.student_pattern):
        scroll = page.findChild(QScrollArea)
        if scroll is not None:
            scroll.ensureWidgetVisible(control)
        qapp.processEvents()
        assert control.visibleRegion().boundingRect().height() >= control.height(), control.objectName()
        assert control.height() >= control.fontMetrics().height() + 10


@pytest.mark.parametrize("scheme", [Qt.ColorScheme.Light, Qt.ColorScheme.Dark])
def test_scan_preview_does_not_overlap_readout_at_small_window(qtbot, qapp, manager, tmp_path, scheme):
    manager.apply_scheme(scheme)
    controller = ScanController(workers=0)
    page = ScanPage(controller, AppSettings(output_root=str(tmp_path)))
    page._timer.stop()
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(page)
    host.setFixedSize(1216, 736)
    qtbot.addWidget(host)
    try:
        page.readout_values["exam"].setText("117349146679794")
        page.readout_values["align"].setText("anchors, -0.5")
        host.show()
        qapp.processEvents()
        preview = QRect(page.preview.mapTo(page, QPoint()), page.preview.size())
        readout_widget = page.readout.parentWidget()
        readout = QRect(readout_widget.mapTo(page, QPoint()), readout_widget.size())
        assert not preview.intersects(readout)
        assert page.preview.width() >= 200
    finally:
        controller.shutdown()
