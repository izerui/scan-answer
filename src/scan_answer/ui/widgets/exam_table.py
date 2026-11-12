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
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setClipRect(b)  # 无论数据如何，都不画出本单元格
        for x, w, color in self.segments(data, r.left(), r.width()):
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, r.top(), w, r.height()), 2, 2)
        painter.restore()

    @staticmethod
    def segments(data: dict, left: float, width: float) -> list[tuple[float, float, str]]:
        """构成条各段（起点、宽度、颜色），按张数算比例，各段合计不超过 width。

        计数里的 warnings 是警告条数（一张卡可能有多条），不是张数：黄段最多按成功张数画。"""
        total = max(0, data.get("total", 0))
        ok = max(0, data.get("ok", 0))
        warned = min(max(0, data.get("warnings", 0)), ok)
        # 成功（无警告）= OK_FILL，有警告 = WARN_FILL，异常卡 = ERR，作废 = 灰色
        counts = [(ok - warned, theme.OK_FILL), (warned, theme.WARN_FILL),
                  (max(0, data.get("physical", 0)), theme.ERR), (max(0, data.get("voided", 0)), theme.DISABLED)]
        total = max(total, sum(c for c, _ in counts))
        out: list[tuple[float, float, str]] = []
        x = left
        for count, color in counts:
            if count <= 0 or total <= 0:
                continue
            seg_w = min(width * count / total, left + width - x)
            if seg_w <= 0:
                break
            out.append((x, seg_w, color))
            x += seg_w
        return out

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
    def _in_detail(self, e) -> bool:
        """事件落在展开区（展开行上沿以下）：从展开区里冒上来的点击、双击都不算点这一行。"""
        row = self.expanded_row()
        y = e.position().y() - self.rowViewportPosition(row) if row >= 0 else -1
        return ROW_H <= y < self.rowHeight(row)

    def mousePressEvent(self, e) -> None:
        if self._in_detail(e):
            e.accept()
            return
        index = self.indexAt(e.position().toPoint())
        pressed_current = index.isValid() and index.row() == self.currentRow()
        on_chevron = (pressed_current and index.column() == 0
                      and e.position().x() - self.columnViewportPosition(0) < CHEVRON_W + 4)
        super().mousePressEvent(e)
        if e.button() != Qt.MouseButton.LeftButton or not pressed_current:
            return
        # 三角：收起 / 展开；点当前行其它位置：收起着就展开，已展开则保持（双击不会先收起）
        if on_chevron or self.expanded_row() != index.row():
            self.toggleRequested.emit(index.row())

    def mouseReleaseEvent(self, e) -> None:
        if self._in_detail(e):
            e.accept()
            return
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:
        if self._in_detail(e):
            e.accept()
            return
        super().mouseDoubleClickEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self._in_detail(e):
            e.accept()
            return
        super().mouseMoveEvent(e)

    def keyPressEvent(self, e) -> None:
        row = self.currentRow()
        if row >= 0 and e.key() == Qt.Key.Key_Left and self.expanded_row() == row:
            self.toggleRequested.emit(row)
        elif row >= 0 and e.key() == Qt.Key.Key_Right and self.expanded_row() != row:
            self.toggleRequested.emit(row)
        else:
            super().keyPressEvent(e)
