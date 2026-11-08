"""System appearance changes preserve working state and document evidence."""
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QComboBox, QDialog, QLineEdit, QListWidget, QPlainTextEdit, QPushButton,
    QTableView, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from scan_answer.ui import theme
from scan_answer.ui.widgets.card_view import CardView
from scan_answer.ui.widgets.common import CounterTile, Toggle


@pytest.fixture
def themed(qapp):
    manager = theme.apply_theme(qapp)
    yield manager
    if manager is not None:
        manager.apply_scheme(Qt.ColorScheme.Light)


def luminance(color):
    rgb = QColor(color).getRgbF()[:3]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(v * w for v, w in zip(linear, (.2126, .7152, .0722)))


def contrast(foreground, background):
    a, b = sorted((luminance(foreground), luminance(background)))
    return (b + .05) / (a + .05)


def test_existing_controls_and_popup_follow_system_signal_without_losing_input(qtbot, qapp, themed):
    host = QDialog()
    layout = QVBoxLayout(host)
    edit = QLineEdit("unsaved input")
    combo = QComboBox()
    combo.addItems(["first", "second"])
    combo.setCurrentIndex(1)
    text = QPlainTextEdit('{"answer": "A"}')
    listing = QListWidget()
    listing.addItems(["one", "two"])
    listing.setCurrentRow(1)
    disabled = QPushButton("Unavailable")
    disabled.setEnabled(False)
    for widget in (edit, combo, text, listing, disabled):
        layout.addWidget(widget)
    qtbot.addWidget(host)
    host.show()
    edit.setSelection(2, 3)
    qapp.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    qtbot.waitUntil(lambda: luminance(edit.palette().color(QPalette.ColorRole.Base)) < .12)
    assert edit.text() == "unsaved input" and edit.selectedText() == "sav"
    assert combo.currentIndex() == 1 and listing.currentRow() == 1
    assert text.toPlainText() == '{"answer": "A"}'
    assert luminance(text.palette().color(QPalette.ColorRole.Text)) > .5
    assert luminance(combo.view().palette().color(QPalette.ColorRole.Base)) < .12
    qapp.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Light)
    qtbot.waitUntil(lambda: luminance(edit.palette().color(QPalette.ColorRole.Base)) > .8)
    assert edit.text() == "unsaved input" and combo.currentIndex() == 1


@pytest.mark.parametrize("scheme", [Qt.ColorScheme.Light, Qt.ColorScheme.Dark])
def test_initial_theme_reads_system_preference(qapp, monkeypatch, scheme):
    hints = qapp.styleHints()
    monkeypatch.setattr(hints, "colorScheme", lambda: scheme)
    manager = theme.apply_theme(qapp)
    try:
        assert manager.scheme == scheme
        assert (luminance(qapp.palette().color(QPalette.ColorRole.Window)) < .12) == (scheme == Qt.ColorScheme.Dark)
        assert theme.apply_theme(qapp) is manager
    finally:
        manager.apply_scheme(Qt.ColorScheme.Light)


def test_semantic_styles_and_model_brushes_update_without_model_reset(qtbot, themed):
    host = QWidget()
    qtbot.addWidget(host)
    theme.set_style(host, "background: $PANEL; color: $INK2;")
    table = QTableView(host)
    model = QStandardItemModel(table)
    item = QStandardItem("needs review")
    theme.set_item_colors(item, foreground="ERR", background="ERR_BG")
    model.appendRow(item)
    table.setModel(model)
    table.setCurrentIndex(model.index(0, 0))
    resets = []
    model.modelReset.connect(lambda: resets.append(True))
    tree = QTreeWidget(host)
    tree_item = QTreeWidgetItem(tree, ["region"])
    theme.set_item_colors(tree_item, column=0, foreground="REGION_TEXT_id")
    tree.setCurrentItem(tree_item)
    for scheme in (Qt.ColorScheme.Dark, Qt.ColorScheme.Light):
        themed.apply_scheme(scheme)
        assert theme.PANEL in host.styleSheet() and "$" not in host.styleSheet()
        assert item.foreground().color().name() == theme.ERR
        assert item.background().color().name() == theme.ERR_BG
        assert tree_item.foreground(0).color().name() == theme.color("REGION_TEXT_id")
        assert tree.currentItem() is tree_item and table.currentIndex().row() == 0
    assert resets == []


def test_counter_and_document_background_refresh_without_changing_pixels_or_zoom(qtbot, themed):
    counter = CounterTile("Errors", "ERR", "ERR")
    view = CardView()
    for widget in (counter, view):
        qtbot.addWidget(widget)
    counter.set_value(7)
    pixels = np.array([[255, 210, 40], [128, 0, 255]], np.uint8)
    view.set_array(pixels)
    view.scale(1.7, 1.7)
    image = view._pix.pixmap().toImage().copy()
    transform = view.transform()
    for scheme in (Qt.ColorScheme.Dark, Qt.ColorScheme.Light):
        themed.apply_scheme(scheme)
        assert counter.value.text() == "7"
        assert theme.ERR in counter.value.styleSheet()
        assert view.backgroundBrush().color().name() == theme.CANVAS
        assert view._pix.pixmap().toImage() == image
        assert view.transform() == transform


@pytest.mark.parametrize("scheme", [Qt.ColorScheme.Light, Qt.ColorScheme.Dark])
def test_readable_semantic_text_and_selected_states(qapp, themed, scheme):
    themed.apply_scheme(scheme)
    for ink, surface in [
        ("INK", "WORK"), ("INK2", "PANEL"), ("INK3", "HEADER"), ("INK", "SELECTION"),
        ("ERR", "ERR_BG"), ("WARN", "WARN_BG"), ("PRIMARY_TEXT", "PRIMARY"),
        ("REGION_TEXT_id", "PANEL"), ("REGION_TEXT_obj", "PANEL"),
        ("REGION_TEXT_sub", "PANEL"), ("REGION_TEXT_bar", "PANEL"),
    ]:
        assert contrast(theme.color(ink), theme.color(surface)) >= 4.5, (scheme, ink, surface)
    palette = qapp.palette()
    assert contrast(palette.color(QPalette.ColorRole.HighlightedText),
                    palette.color(QPalette.ColorRole.Highlight)) >= 4.5


def test_destroyed_widgets_and_repeated_initialization_are_safe(qtbot, qapp, themed):
    for _ in range(3):
        assert theme.apply_theme(qapp) is themed
        widget = Toggle()
        theme.set_style(widget, "color: $INK;")
        widget.deleteLater()
    qapp.sendPostedEvents()
    qapp.styleHints().colorSchemeChanged.emit(Qt.ColorScheme.Dark)
    qtbot.waitUntil(lambda: themed.scheme == Qt.ColorScheme.Dark)
