"""Theme switches update editor presentation without changing working state."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QLabel, QWidget

from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.app.trial import TrialSheet
from scan_answer.template.doc import KIND_COLOR, RegionRef, TemplateDoc
from scan_answer.ui import theme
from scan_answer.ui.editor.canvas import TemplateCanvas
from scan_answer.ui.editor.new_dialog import NewTemplateDialog
from scan_answer.ui.editor.props import PropsPanel


@pytest.fixture
def manager(qapp):
    manager = theme.apply_theme(qapp)
    assert manager is not None, "apply_theme must return the application theme manager"
    previous = manager.scheme
    manager.apply_scheme(Qt.ColorScheme.Light)
    yield manager
    manager.apply_scheme(previous)


def _page(qtbot):
    from scan_answer.ui.pages.template_page import TemplatePage

    page = TemplatePage(AppSettings(template_dirs=[str(bundled_templates_dir())]))
    qtbot.addWidget(page)
    page.resize(1440, 900)
    page.show()
    assert page.open_editor(bundled_templates_dir() / "guanzong199")
    return page


def _pixels(canvas):
    image = canvas._bg_item.pixmap().toImage()
    return bytes(image.constBits())


def test_canvas_refresh_changes_only_surroundings(qtbot, monkeypatch):
    monkeypatch.setattr(theme, "CANVAS", "#eef0f2", raising=False)
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")
    ref = RegionRef("field", 0)
    canvas.select([ref])
    canvas.set_zoom(2.25)
    image_item = canvas._bg_item
    image = _pixels(canvas)
    region_items = dict(canvas._region_items)
    monkeypatch.setattr(theme, "CANVAS", "#202124", raising=False)

    refresh = getattr(canvas, "refresh_theme", None)
    assert callable(refresh), "canvas needs a visual-only theme refresh"
    refresh()

    assert canvas.backgroundBrush().color().name() == "#202124"
    assert canvas._bg_item is image_item
    assert _pixels(canvas) == image
    assert canvas._region_items == region_items
    assert canvas.selected() == [ref]
    assert canvas.zoom() == 2.25
    assert not doc.dirty


@pytest.mark.parametrize("face", ["front", "back"])
def test_loaded_editor_switch_preserves_document_history_selection_and_pixels(
    qtbot, qapp, app_home, manager, face
):
    page = _page(qtbot)
    page.set_face(face)
    qapp.processEvents()
    ref = next(r for r in page.doc.regions(face) if r.kind == "objective")
    page.canvas.select([ref])
    x, y, w, h = page.doc.rect(ref)
    page._on_region_edited(ref, (x + 1, y, w, h))
    qapp.processEvents()
    page._on_region_edited(ref, (x + 2, y, w, h))
    page.undo()
    page.canvas.set_zoom(3.25)
    page.canvas.horizontalScrollBar().setValue(70)
    page.canvas.verticalScrollBar().setValue(80)
    group = next(page.tree.topLevelItem(i) for i in range(page.tree.topLevelItemCount())
                 if any(page.tree.topLevelItem(i).child(j).isSelected()
                        for j in range(page.tree.topLevelItem(i).childCount())))
    selected = page.tree.selectedItems()[0]
    page.tree.setCurrentItem(selected)
    page.props._label_edits[0].setText("pending edit")
    page.props.set_snap_result("Grid was not found", False)
    qapp.processEvents()

    doc = page.doc
    snapshot = doc.snapshot()
    revision = doc.revision
    history = deepcopy((page.undo_stack._undo, page.undo_stack._redo))
    assert doc.dirty and history[0] and history[1]
    props_input = page.props._label_edits[0]
    bg_item = page.canvas._bg_item
    image = _pixels(page.canvas)
    bases = {key: value.copy() for key, value in doc.bases.items()}
    region_items = dict(page.canvas._region_items)
    brushes = {key: (item.pen().color(), item.brush().color())
               for key, item in region_items.items()}
    scroll = (page.canvas.horizontalScrollBar().value(), page.canvas.verticalScrollBar().value())
    title = page.title_label.text()
    light_group_color = group.foreground(0).color()
    light_background = page.canvas.backgroundBrush().color()
    state_signals = []
    page.tree.regionSelectionChanged.connect(lambda *_: state_signals.append("tree"))
    page.canvas.selectionChanged.connect(lambda *_: state_signals.append("canvas"))
    page.props.paramsChanged.connect(lambda *_: state_signals.append("props"))

    for scheme in (Qt.ColorScheme.Dark, Qt.ColorScheme.Light):
        manager.apply_scheme(scheme)
        qapp.processEvents()
        assert page.doc is doc and doc.dirty
        assert doc.snapshot() == snapshot and doc.revision == revision
        assert (page.undo_stack._undo, page.undo_stack._redo) == history
        assert page.face == face and page.canvas.selected() == [ref]
        assert page.tree.selectedItems() == [selected]
        assert page.tree.currentItem() is selected and selected.parent() is group
        assert group.isExpanded()
        assert page.props._label_edits[0] is props_input
        assert props_input.text() == "pending edit"
        assert page.props.target() == ref
        assert page.title_label.text() == title
        assert page.canvas.zoom() == 3.25
        assert (page.canvas.horizontalScrollBar().value(),
                page.canvas.verticalScrollBar().value()) == scroll
        assert page.canvas._bg_item is bg_item and _pixels(page.canvas) == image
        assert page.canvas._region_items == region_items
        for key, item in region_items.items():
            assert (item.pen().color(), item.brush().color()) == brushes[key]
        for key, value in bases.items():
            np.testing.assert_array_equal(doc.bases[key], value)
        assert not state_signals
        assert group.foreground(0).color().name() == theme.color(f"REGION_TEXT_{KIND_COLOR[ref.kind]}")
        assert page.canvas.backgroundBrush().color().name() == theme.CANVAS
        assert theme.PANEL in page.tree.styleSheet()
        assert theme.SELECTION in page.tree.styleSheet()
        assert theme.ERR in page.props._snap_label.styleSheet()
        for name in ("editorBar", "editorStatus"):
            assert theme.HEADER in page.findChild(QWidget, name).styleSheet()
        assert theme.WORK in page.findChild(QWidget, "propsSide").styleSheet()
        if scheme == Qt.ColorScheme.Dark:
            assert group.foreground(0).color() != light_group_color
            assert page.canvas.backgroundBrush().color() != light_background


@pytest.mark.parametrize("ok, role", [(True, "INK"), (False, "ERR")])
def test_existing_property_feedback_tracks_theme(qtbot, qapp, manager, ok, role):
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.set_snap_result("Snap result", ok)
    before = panel._snap_label.styleSheet()

    manager.apply_scheme(Qt.ColorScheme.Dark)
    qapp.processEvents()

    assert panel._snap_label.text() == "Snap result"
    assert theme.color(role) in panel._snap_label.styleSheet()
    assert panel._snap_label.styleSheet() != before


def test_open_dialog_preserves_input_and_error_across_switch(qtbot, qapp, manager):
    dialog = NewTemplateDialog()
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.name_edit.setText("Unsaved template")
    dialog.front_edit.setText("/tmp/unsaved-front.png")
    dialog.error.setText("Cannot read image")
    before = dialog.error.styleSheet()

    manager.apply_scheme(Qt.ColorScheme.Dark)
    qapp.processEvents()

    assert dialog.name_edit.text() == "Unsaved template"
    assert dialog.front_edit.text() == "/tmp/unsaved-front.png"
    assert dialog.error.text() == "Cannot read image"
    assert theme.ERR in dialog.error.styleSheet()
    assert dialog.error.styleSheet() != before
    fresh = NewTemplateDialog()
    qtbot.addWidget(fresh)
    assert fresh.error.styleSheet() == dialog.error.styleSheet()


def test_trial_failure_text_recolors_in_place_and_paper_marks_stay_fixed(qtbot, qapp, app_home, manager):
    from scan_answer.ui.pages.template_page import TRIAL_MARK

    page = _page(qtbot)
    qapp.processEvents()
    page._trial_revision = (page.doc, page.doc.revision)
    failure = TrialSheet(["unreadable.png"], False, "Cannot read image", None, {})
    warped = page.doc.bases["front"].copy()
    warped[0, 0] = 17
    success = TrialSheet(["sample.png"], True, "", None, {"front": warped})
    page._trial_done(page._trial_token, [failure, success])
    page.trial_list.setCurrentRow(1)
    page.props_stack.setCurrentIndex(1)
    item = page.trial_list.item(0)
    page.canvas.set_marks([((1, 2, 3, 4), TRIAL_MARK["bad"][0], TRIAL_MARK["bad"][1])])
    mark = page.canvas._mark_items[0]
    before = item.foreground().color()
    paper_error = mark.pen().color()
    pixels = _pixels(page.canvas)

    manager.apply_scheme(Qt.ColorScheme.Dark)
    qapp.processEvents()

    assert page.trial_list.item(0) is item
    assert page.trial_list.currentRow() == 1 and page._trial_results == [failure, success]
    assert page._trial_current is success and page.trial_preview._sheet is success
    assert page.props_stack.currentIndex() == 1
    assert page.canvas._bg_override is warped
    assert item.foreground().color().name() == theme.ERR
    assert item.foreground().color() != before
    assert mark is page.canvas._mark_items[0]
    assert mark.pen().color() == paper_error
    assert paper_error.name() == theme.PAPER_ERR
    assert _pixels(page.canvas) == pixels
    note = next(label for label in page.list_view.findChildren(QLabel) if label.wordWrap())
    assert note.palette().color(QPalette.ColorRole.WindowText).name() == theme.INK2
