"""通用小控件：分区标题、色块、计数卡、分段按钮、开关、图例、省略标签与表格/表单的统一配置。"""
from __future__ import annotations

import shiboken6
from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractButton, QAbstractItemView, QButtonGroup, QFormLayout, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton, QSizePolicy, QTableView, QVBoxLayout, QWidget)

from .. import theme


def section_header(text: str, right: str = "") -> QWidget:
    w = QWidget()
    w.setObjectName("sectionHeader")
    lay = QHBoxLayout(w)
    lay.setContentsMargins(16, 10, 16, 10)
    title = QLabel(text)
    title.setStyleSheet("font-weight:600;")
    lay.addWidget(title)
    lay.addStretch(1)
    if right:
        r = QLabel(right)
        r.setObjectName("muted")
        lay.addWidget(r)
    theme.set_style(w, "#sectionHeader { border-bottom: 1px solid $LINE; }")
    return w


class Swatch(QWidget):
    """区域色码小方块。"""

    def __init__(self, color: str, size: int = 10, parent: QWidget | None = None):
        super().__init__(parent)
        self.color = color
        self.setFixedSize(size, size)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), theme.qcolor(self.color))


class CounterTile(QWidget):
    """大号计数：标题 + 数值（+ 单位）+ 说明。"""

    def __init__(self, title: str, color: str | None = None, value_color: str | None = None,
                 unit: str = "", note: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 12)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        if color:
            head.addWidget(Swatch(color, 8))
        t = ElidedLabel(title, Qt.TextElideMode.ElideRight)
        theme.set_style(t, "color:$INK2; font-size:13px;")
        head.addWidget(t, 1)
        lay.addLayout(head)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = QLabel("0")
        # 全局 QSS 设置了字号，这里也必须用样式表覆盖（setFont 会被 QSS 压住）
        role = value_color or "INK"
        ink = role if role.startswith("#") else f"${role}"
        theme.set_style(self.value, f"font-size:34px; font-weight:600; color:{ink};")
        row.addWidget(self.value, 0, Qt.AlignmentFlag.AlignBottom)
        self.unit = QLabel(unit)
        theme.set_style(self.unit, "color:$INK3; font-size:13px; padding-bottom:6px;")
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = QLabel(note)
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)  # 窄屏下六张卡并排，说明文字换行而不是被截断
        lay.addWidget(self.note)
        lay.addStretch(1)

    def set_value(self, v: int | str, unit: str | None = None) -> None:
        self.value.setText(str(v))
        if unit is not None:
            self.unit.setText(unit)


class Segmented(QWidget):
    """分段按钮（单选）。"""
    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], value: str, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, label in options:
            b = QPushButton(label)
            b.setObjectName("seg")
            b.setCheckable(True)
            b.setChecked(key == value)
            b.clicked.connect(lambda _c=False, k=key: self.changed.emit(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)

    def value(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def set_value(self, key: str) -> None:
        self.buttons[key].setChecked(True)


class Toggle(QAbstractButton):
    """开关（QSS 无法画圆角滑块，自绘）。"""

    def __init__(self, checked: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(36, 20)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(1, 1, 34, 18)
        on = self.isChecked()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(theme.qcolor("TOGGLE_ON" if on else "TOGGLE_OFF")
                   if self.isEnabled() else theme.qcolor("LINE2"))  # 禁用时也要看得出开/关
        p.drawRoundedRect(r, 9, 9)
        p.setBrush(theme.qcolor("TOGGLE_KNOB") if self.isEnabled() else theme.qcolor("DISABLED_BG"))
        p.drawEllipse(QRectF(19 if on else 3, 3, 14, 14))


class RegionLegend(QWidget):
    """区域色码图例（两列）。"""

    def __init__(self, extra: list[tuple[str, str]] | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(16, 10, 16, 12)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)
        items = [(theme.REGION[k], label) for k, label in theme.REGION_LABELS] + (extra or [])
        for i, (color, label) in enumerate(items):
            cell = QHBoxLayout()
            cell.setSpacing(8)
            cell.addWidget(Swatch(color))
            lab = QLabel(label)
            lab.setStyleSheet("font-size:13px;")
            cell.addWidget(lab)
            cell.addStretch(1)
            grid.addLayout(cell, i // 2, i % 2)


def hline() -> QWidget:
    w = QWidget()
    w.setFixedHeight(1)
    theme.set_style(w, "background:$LINE;")
    return w


class ElidedLabel(QLabel):
    """单行标签：放不下时用省略号截断（默认截中间，适合路径），完整文字放在提示里。"""

    def __init__(self, text: str = "", mode: Qt.TextElideMode = Qt.TextElideMode.ElideMiddle,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 — 覆盖 Qt 方法
        self._full = text
        self.setToolTip(text if text else "")
        self._elide()

    def text(self) -> str:
        return self._full

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 4, super().sizeHint().height())

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._elide()

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == e.Type.FontChange:  # 样式表改字号后重新截断
            self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, self._mode, max(0, self.width() - 2))
        QLabel.setText(self, shown)


def setup_table(view: QTableView, *, stretch: int | None = None, row_height: int = 34) -> None:
    """数据表的统一外观：整行单选、只读、无网格、表头与内容左对齐、行高一致。"""
    view.verticalHeader().setVisible(False)
    view.verticalHeader().setDefaultSectionSize(row_height)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.setShowGrid(False)
    view.setWordWrap(False)
    header = view.horizontalHeader()
    header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    header.setHighlightSections(False)
    header.setMinimumSectionSize(48)
    if stretch is not None:
        header.setSectionResizeMode(stretch, header.ResizeMode.Stretch)


class _EmptyHint(QObject):
    """表格没有数据时在视口中央显示一行说明。"""

    def __init__(self, view: QAbstractItemView, text: str):
        super().__init__(view)
        self.view = view
        self.label = QLabel(text, view.viewport())
        self.label.setObjectName("muted")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        view.viewport().installEventFilter(self)
        model = view.model()
        for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset, model.layoutChanged):
            sig.connect(self.update)
        self.update()

    def eventFilter(self, obj, e) -> bool:  # noqa: N802 — Qt 方法名
        if e.type() == QEvent.Type.Resize:
            self._place()
        return False

    def _place(self) -> None:
        r = self.view.viewport().rect().adjusted(24, 0, -24, 0)
        self.label.setGeometry(r.x(), min(r.height() // 3, 120), r.width(), 48)

    def update(self, *_args) -> None:
        if not shiboken6.isValid(self.view):  # 表格析构时模型还会发出行删除信号
            return
        self.label.setVisible(self.view.model().rowCount() == 0)
        self._place()


def set_empty_text(view: QAbstractItemView, text: str) -> QLabel:
    """给表格/列表设置空状态说明；返回说明标签，可随场景改文字。"""
    hint = getattr(view, "_empty_hint", None)
    if hint is None:
        hint = _EmptyHint(view, text)
        view._empty_hint = hint
    hint.label.setText(text)
    return hint.label


def form_layout(parent: QWidget | None = None, *, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QFormLayout:
    """表单：标签左对齐、字段撑满、行距统一。多个表单要对齐标签列时，用 form_label 给标签同样的宽度。"""
    f = QFormLayout(parent) if parent is not None else QFormLayout()
    f.setContentsMargins(*margins)
    f.setHorizontalSpacing(12)
    f.setVerticalSpacing(10)
    f.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    f.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    f.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
    return f


def form_label(text: str, width: int = 0) -> QLabel:
    lab = QLabel(text)
    if width:
        lab.setMinimumWidth(width)
    return lab
