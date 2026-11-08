"""批次拼图：每格一张纸、每行 50 张，形状 + 颜色双编码。

成功 = 浅绿方块；数据警告 = 琥珀扇形；物理异常 = 红圆；识别中 = 蓝方块；作废 = 灰色叉；待扫 = 空框。
悬停显示序号与原因，点击发 cellClicked(seq)。批次超过 1000 张时默认只显示最近 10 行，可展开。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from .. import theme

PER_ROW = 50
RULER_W = 44
GAP = 2
COLLAPSE_AFTER = 1000
COLLAPSED_ROWS = 10

QUEUED, OK, WARN, ERR, VOID = "queued", "ok", "warn", "err", "void"
LABELS = {QUEUED: "识别中", OK: "成功", WARN: "数据警告", ERR: "物理异常", VOID: "已作废", None: "待扫"}


@dataclass
class Cell:
    state: str
    note: str = ""


class BatchMosaic(QWidget):
    cellClicked = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.cells: dict[int, Cell] = {}
        self.planned = 0  # 计划张数（未知时为 0，拼图随已登记张数增长）
        self.current: int | None = None
        self.expanded = False
        self.setMouseTracking(True)
        self.setMinimumHeight(60)

    # ---------- 数据 ----------
    def reset(self, planned: int = 0) -> None:
        self.cells.clear()
        self.planned = planned
        self.current = None
        self.updateGeometry()
        self.update()

    def set_cell(self, seq: int, state: str, note: str = "") -> None:
        self.cells[seq] = Cell(state, note)
        if state == QUEUED:
            self.current = seq
        self.updateGeometry()
        self.update()

    def count(self, state: str) -> int:
        return sum(1 for c in self.cells.values() if c.state == state)

    # ---------- 几何 ----------
    def total_slots(self) -> int:
        n = max(self.planned, max(self.cells, default=0))
        return max(PER_ROW, math.ceil(n / PER_ROW) * PER_ROW)

    def first_row(self) -> int:
        rows = self.total_slots() // PER_ROW
        if self.expanded or self.total_slots() <= COLLAPSE_AFTER:
            return 0
        return max(0, rows - COLLAPSED_ROWS)

    def visible_rows(self) -> int:
        return self.total_slots() // PER_ROW - self.first_row()

    def cell_size(self) -> float:
        return max(6.0, (self.width() - RULER_W - GAP * (PER_ROW - 1)) / PER_ROW)

    def _row_y(self, r: int, size: float) -> float:
        """第 r 个可见行的顶端；每满 100 张（两行）多留 4 像素。"""
        first = self.first_row()
        breaks = sum(1 for i in range(1, r + 1) if (i + first) % 2 == 0)
        return r * (size + GAP) + breaks * 4

    def sizeHint(self) -> QSize:
        return QSize(760, self.heightForWidth(760))

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        size = max(6.0, (w - RULER_W - GAP * (PER_ROW - 1)) / PER_ROW)
        rows = self.visible_rows()
        return int(self._row_y(rows - 1, size) + size + 2)

    def cell_rect(self, seq: int) -> QRectF | None:
        idx = seq - 1
        r = idx // PER_ROW - self.first_row()
        if r < 0 or r >= self.visible_rows():
            return None
        s = self.cell_size()
        c = idx % PER_ROW
        return QRectF(RULER_W + c * (s + GAP), self._row_y(r, s), s, s)

    def seq_at(self, pos: QPointF) -> int | None:
        s = self.cell_size()
        if pos.x() < RULER_W:
            return None
        c = int((pos.x() - RULER_W) // (s + GAP))
        if c >= PER_ROW:
            return None
        for r in range(self.visible_rows()):
            y = self._row_y(r, s)
            if y - 1 <= pos.y() <= y + s + 1:
                seq = (r + self.first_row()) * PER_ROW + c + 1
                rect = self.cell_rect(seq)
                return seq if rect and rect.adjusted(-1, -1, 1, 1).contains(pos) else None
        return None

    # ---------- 绘制 ----------
    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(theme.ui_font(12))
        first = self.first_row()
        for r in range(self.visible_rows()):
            row_first = (r + first) * PER_ROW + 1
            rect0 = self.cell_rect(row_first)
            p.setPen(theme.qcolor(theme.INK3))
            p.drawText(QRectF(0, rect0.top() - 2, RULER_W - 6, rect0.height() + 4),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, str(row_first))
            for c in range(PER_ROW):
                seq = row_first + c
                self._draw_cell(p, self.cell_rect(seq), self.cells.get(seq), seq == self.current)

    def _draw_cell(self, p: QPainter, r: QRectF, cell: Cell | None, current: bool) -> None:
        p.setPen(Qt.PenStyle.NoPen)
        state = cell.state if cell else None
        if state is None:
            p.setPen(QPen(theme.qcolor(theme.LINE), 1))
            p.setBrush(theme.qcolor(theme.WORK))
            p.drawRect(r.adjusted(0.5, 0.5, -0.5, -0.5))
        elif state == OK:
            p.setBrush(theme.qcolor(theme.OK_FILL))
            p.drawRect(r)
        elif state == QUEUED:
            p.setBrush(theme.qcolor(theme.RUN_FILL))
            p.drawRect(r)
        elif state == ERR:
            p.setBrush(theme.qcolor(theme.ERR))
            p.drawEllipse(r)
        elif state == WARN:
            path = QPainterPath(r.bottomRight())
            path.arcTo(QRectF(r.left(), r.top(), r.width() * 2, r.height() * 2), 90, 90)
            path.closeSubpath()
            p.setBrush(theme.qcolor(theme.WARN_FILL))
            p.drawPath(path)
        elif state == VOID:
            p.setPen(QPen(theme.qcolor(theme.INK3), 1.2))
            p.drawLine(r.topLeft(), r.bottomRight())
            p.drawLine(r.topRight(), r.bottomLeft())
        if current:
            p.setPen(QPen(theme.qcolor(theme.INK), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(-1.5, -1.5, 1.5, 1.5))

    # ---------- 交互 ----------
    def tooltip_text(self, seq: int) -> str:
        cell = self.cells.get(seq)
        state = cell.state if cell else None
        text = f"#{seq:04d}  {LABELS[state]}"
        if cell and cell.note:
            text += f"：{cell.note}"
        return text

    def mouseMoveEvent(self, e) -> None:
        seq = self.seq_at(e.position())
        if seq is None:
            QToolTip.hideText()
            return
        QToolTip.showText(e.globalPosition().toPoint(), self.tooltip_text(seq), self)

    def mousePressEvent(self, e) -> None:
        seq = self.seq_at(e.position())
        if seq is not None and seq in self.cells:
            self.cellClicked.emit(seq)


class MosaicLegend(QWidget):
    """拼图图例（与拼图同一绘制逻辑）。"""

    ITEMS = [(OK, "成功"), (WARN, "数据警告"), (ERR, "物理异常"), (QUEUED, "识别中"), (None, "待扫")]

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(18)
        self._m = BatchMosaic()

    def sizeHint(self) -> QSize:
        return QSize(380, 18)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setFont(theme.ui_font(12))
        x = 0.0
        for state, label in self.ITEMS:
            self._m._draw_cell(p, QRectF(x, 3, 12, 12), Cell(state) if state else None, False)
            p.setPen(theme.qcolor(theme.INK2))
            w = p.fontMetrics().horizontalAdvance(label)
            p.drawText(QRectF(x + 17, 0, w + 4, 18), Qt.AlignmentFlag.AlignVCenter, label)
            x += 17 + w + 14
