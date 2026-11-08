"""通用小控件：分区标题、色块、计数卡、分段按钮、开关、图例。"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (QAbstractButton, QButtonGroup, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QVBoxLayout, QWidget)

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
        lay.setContentsMargins(12, 10, 12, 12)
        lay.setSpacing(2)
        head = QHBoxLayout()
        head.setSpacing(6)
        if color:
            head.addWidget(Swatch(color, 8))
        t = QLabel(title)
        theme.set_style(t, "color:$INK2; font-size:13px;")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.value = QLabel("0")
        # 全局 QSS 设置了字号，这里也必须用样式表覆盖（setFont 会被 QSS 压住）
        role = value_color or "INK"
        ink = role if role.startswith("#") else f"${role}"
        theme.set_style(self.value, f"font-size:38px; font-weight:600; color:{ink};")
        row.addWidget(self.value, 0, Qt.AlignmentFlag.AlignBottom)
        self.unit = QLabel(unit)
        theme.set_style(self.unit, "color:$INK3; font-size:15px; padding-bottom:6px;")
        row.addWidget(self.unit, 0, Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        self.note = QLabel(note)
        self.note.setObjectName("muted")
        lay.addWidget(self.note)

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
                   if self.isEnabled() else theme.qcolor("DISABLED_BG"))
        p.drawRoundedRect(r, 9, 9)
        p.setBrush(theme.qcolor("TOGGLE_KNOB"))
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
