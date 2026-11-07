from PySide6.QtCore import QPointF, Qt

from scan_answer.ui.widgets.common import CounterTile, Segmented, Toggle
from scan_answer.ui.widgets.mosaic import ERR, OK, PER_ROW, QUEUED, BatchMosaic, Cell


def test_mosaic_geometry_and_tooltips(qtbot):
    m = BatchMosaic()
    qtbot.addWidget(m)
    m.resize(760, 300)
    m.reset(120)
    assert m.total_slots() == 150 and m.visible_rows() == 3
    for seq in (1, 50, 51, 120):
        assert m.seq_at(m.cell_rect(seq).center()) == seq
    assert m.seq_at(QPointF(5, 5)) is None  # 刻度区
    m.set_cell(7, ERR, "空白页")
    m.set_cell(8, QUEUED)
    assert m.tooltip_text(7) == "#0007  物理异常：空白页" and m.tooltip_text(9) == "#0009  待扫"
    assert m.current == 8 and m.count(ERR) == 1
    clicked = []
    m.cellClicked.connect(clicked.append)
    qtbot.mouseClick(m, Qt.MouseButton.LeftButton, pos=m.cell_rect(7).center().toPoint())
    assert clicked == [7]


def test_mosaic_collapses_large_batches(qtbot):
    m = BatchMosaic()
    qtbot.addWidget(m)
    m.resize(760, 300)
    for seq in range(1, 1201):
        m.cells[seq] = Cell(OK)
    assert m.visible_rows() == 10 and m.first_row() == 1200 // PER_ROW - 10
    assert m.cell_rect(1) is None and m.cell_rect(1200) is not None
    m.expanded = True
    assert m.visible_rows() == 1200 // PER_ROW
    assert m.heightForWidth(760) > 300


def test_small_widgets(qtbot):
    t = CounterTile("速度", unit="张/分")
    s = Segmented([("a", "甲"), ("b", "乙")], "a")
    g = Toggle(False)
    for w in (t, s, g):
        qtbot.addWidget(w)
    t.set_value(82)
    assert t.value.text() == "82"
    got = []
    s.changed.connect(got.append)
    s.buttons["b"].click()
    assert got == ["b"] and s.value() == "b"
    g.click()
    assert g.isChecked()
