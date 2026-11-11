"""考试页的考试表：每场考试一行，当前行可原位展开，在行下方显示详情与批次（展开区是一块放在视口里的控件）。"""
from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPolygonF
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QTableWidget, QWidget)

from .. import theme

ROW_H = 46  # 考试行（展开行的上沿部分）高度
CHEVRON_W = 26  # 第一列左侧留给展开三角的宽度


class BandDelegate(QStyledItemDelegate):
    """展开行很高：单元格内容只画在上沿的一行高度里，下面留给展开区。第一列画展开三角。"""

    def __init__(self, table: "ExamTable"):
        super().__init__(table)
        self.table = table

    def band(self, option: QStyleOptionViewItem) -> QRect:
        r = QRect(option.rect)
        r.setHeight(min(r.height(), ROW_H))
        return r

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        opt.rect = self.band(option)
        if index.column() != 0:
            super().paint(painter, opt, index)
            return
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else None
        text = opt.text
        opt.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)  # 背景（含选中色）
        opt.text = text
        opt.rect = opt.rect.adjusted(CHEVRON_W - 6, 0, 0, 0)
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        self._chevron(painter, self.band(option), index.row() == self.table.expanded_row())

    @staticmethod
    def _chevron(painter: QPainter, band: QRect, open_: bool) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.qcolor("INK" if open_ else "INK3"))
        cx, cy = band.left() + 13, band.center().y() + 1
        if open_:
            pts = [QPointF(cx - 5, cy - 3), QPointF(cx + 5, cy - 3), QPointF(cx, cy + 3)]
        else:
            pts = [QPointF(cx - 2, cy - 5), QPointF(cx + 4, cy), QPointF(cx - 2, cy + 5)]
        painter.drawPolygon(QPolygonF(pts))
        painter.restore()


class CompositionDelegate(BandDelegate):
    """考试表"构成"列：水平比例条——成功/警告/异常/作废。"""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        opt = QStyleOptionViewItem(option)
        opt.rect = self.band(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)  # 背景（含选中色）
        data = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        if not data or not data.get("total"):
            painter.setPen(theme.qcolor(theme.INK3))
            painter.drawText(opt.rect.adjusted(10, 0, 0, 0),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "—")
            painter.restore()
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        b = opt.rect
        r = QRectF(b.left() + 8, b.center().y() - 4, max(0, b.width() - 16), 10)
        total = data["total"]
        # 成功（无警告）= OK_FILL，有数据警告 = WARN_FILL，物理异常 = ERR，作废 = 灰色
        ok_clean = max(0, data.get("ok", 0) - data.get("warnings", 0))
        segments = [
            (ok_clean, theme.OK_FILL),
            (data.get("warnings", 0), theme.WARN_FILL),
            (data.get("physical", 0), theme.ERR),
            (data.get("voided", 0), theme.DISABLED),
        ]
        x = r.left()
        for count, color in segments:
            if count <= 0:
                continue
            seg_w = r.width() * count / total
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, r.top(), seg_w, r.height()), 2, 2)
            x += seg_w
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(110, ROW_H)


class ExamTable(QTableWidget):
    """一行一场考试；expanded_row 那一行加高，展开区控件放在这一行上沿以下。

    点当前行的三角或按 ← / → 收起、展开；点其它行由页面决定是否展开（考试页：选中即展开）。"""
    toggleRequested = Signal(int)  # 用户要求切换某行的展开状态

    def __init__(self, rows: int, columns: int, parent: QWidget | None = None):
        super().__init__(rows, columns, parent)
        self._expanded = -1
        self._detail: QWidget | None = None
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        self.verticalHeader().setDefaultSectionSize(ROW_H)
        self.verticalHeader().sectionResized.connect(lambda *_a: self._place())
        self.horizontalHeader().sectionResized.connect(lambda *_a: self._place())
        self.setItemDelegate(BandDelegate(self))

    # ---------- 展开区 ----------
    def set_detail_widget(self, w: QWidget) -> None:
        self._detail = w
        w.setParent(self.viewport())
        w.hide()

    def expanded_row(self) -> int:
        return self._expanded if 0 <= self._expanded < self.rowCount() else -1

    def set_expanded(self, row: int) -> None:
        old = self.expanded_row()
        self._expanded = row if 0 <= row < self.rowCount() else -1
        if 0 <= old < self.rowCount() and old != self._expanded:
            self.setRowHeight(old, ROW_H)
        self.update_detail_height()

    def update_detail_height(self) -> None:
        """展开区内容变了（批次行数等）：按它的高度重设展开行的行高。"""
        row = self.expanded_row()
        if row >= 0 and self._detail is not None:
            self._detail.ensurePolished()
            if self._detail.layout() is not None:
                self._detail.layout().activate()
            self.setRowHeight(row, ROW_H + self._detail.sizeHint().height())
        self._place()
        self.viewport().update()

    def _place(self) -> None:
        if self._detail is None:
            return
        row = self.expanded_row()
        if row < 0:
            self._detail.hide()
            return
        top = self.rowViewportPosition(row) + ROW_H
        self._detail.setGeometry(0, top, self.viewport().width(), self.rowHeight(row) - ROW_H)
        self._detail.show()
        self._detail.raise_()

    def updateGeometries(self) -> None:  # noqa: N802 — Qt 方法名
        super().updateGeometries()
        self._place()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802 — Qt 方法名
        super().scrollContentsBy(dx, dy)
        self._place()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._place()

    # ---------- 收起 / 展开 ----------
    def mousePressEvent(self, e) -> None:
        index = self.indexAt(e.position().toPoint())
        on_chevron = (index.isValid() and index.column() == 0 and index.row() == self.currentRow()
                      and e.position().x() - self.columnViewportPosition(0) < CHEVRON_W + 4
                      and e.position().y() - self.rowViewportPosition(index.row()) < ROW_H)
        super().mousePressEvent(e)
        if on_chevron and e.button() == Qt.MouseButton.LeftButton:
            self.toggleRequested.emit(index.row())

    def keyPressEvent(self, e) -> None:
        row = self.currentRow()
        if row >= 0 and e.key() == Qt.Key.Key_Left and self.expanded_row() == row:
            self.toggleRequested.emit(row)
        elif row >= 0 and e.key() == Qt.Key.Key_Right and self.expanded_row() != row:
            self.toggleRequested.emit(row)
        else:
            super().keyPressEvent(e)
