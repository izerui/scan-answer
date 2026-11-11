"""设置页的分节部件：左栏目录项、右侧一节的外框、带“原为…”的字段行、占位符小标签。"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .. import theme
from .common import ElidedLabel
from .guide import HelpButton

LABEL_WIDTH = 128  # 字段行的标签列宽，同一节内字段左边缘对齐

_TAG = "border:1px solid $LINE2; border-radius:3px; color:$INK3; font-size:12px; padding:0 6px;"
_TAG_MUST = "border:1px solid $ACCENT; border-radius:3px; color:$ACCENT; font-size:12px; padding:0 6px;"


def mono(widget: QWidget) -> QWidget:
    """等宽字体（占位符、地址、密钥）：字号仍由样式表控制。"""
    widget.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
    return widget


def tag_label(text: str, must: bool = False) -> QLabel:
    lab = QLabel(text)
    theme.set_style(lab, _TAG_MUST if must else _TAG)
    return lab


def dot_label(tip: str) -> QLabel:
    """未保存标记：小圆点。"""
    dot = QLabel()
    dot.setFixedSize(8, 8)
    dot.setToolTip(tip)
    theme.set_style(dot, "background:$WARN; border-radius:4px;")
    return dot


class NavItem(QPushButton):
    """左栏目录的一节：标题、未保存圆点、标签，下面一行当前取值摘要。"""

    def __init__(self, title: str, tag: str, must: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("settingsNav")
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # 点选不留焦点框；Tab 键仍能走到
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        theme.set_style(self, "QPushButton#settingsNav { text-align:left; border:none; border-radius:6px;"
                              " padding:0; min-height:0; background:transparent; }"
                              "QPushButton#settingsNav:hover { background:$HEADER; }"
                              "QPushButton#settingsNav:focus { border:1px solid $ACCENT; }"
                              "QPushButton#settingsNav:checked { background:$SELECTION; border:none;"
                              " border-left:3px solid $ACCENT; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(title)
        theme.set_style(self.title, "font-size:14px; font-weight:600; background:transparent;")
        self.dot = dot_label("本节有未保存的修改")
        self.dot.hide()
        self.tag = tag_label(tag, must)
        top.addWidget(self.title)
        top.addWidget(self.dot)
        top.addStretch(1)
        top.addWidget(self.tag)
        lay.addLayout(top)
        self.summary = ElidedLabel("", Qt.TextElideMode.ElideRight)
        theme.set_style(self.summary, "color:$INK3; font-size:12px; background:transparent;")
        lay.addWidget(self.summary)
        for w in (self.title, self.dot, self.tag, self.summary):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

    def sizeHint(self) -> QSize:  # noqa: N802 — 按内部两行文字定高（QPushButton 默认只按按钮文字算）
        return QSize(240, max(56, self.layout().sizeHint().height()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return self.sizeHint()

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)
        self._update_tip()

    def set_dirty(self, dirty: bool) -> None:
        self.dot.setVisible(dirty)
        self._update_tip()

    def _update_tip(self) -> None:
        dirty = "（本节有未保存的修改）" if not self.dot.isHidden() else ""
        self.setToolTip(f"{self.title.text()}{dirty}：{self.summary.text()}")


class SectionPage(QWidget):
    """右侧一节：标题（可带“?”）+ 标签 + 一句说明，下面放字段行。"""

    def __init__(self, title: str, tag: str, lead: str, concept: str | None = None, must: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setMaximumWidth(980)
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(40, 24, 40, 24)
        self.body.setSpacing(0)
        row = QWidget()
        row.setObjectName("settingsHeading")
        theme.set_style(row, "#settingsHeading { border-bottom:1px solid $LINE; }")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 10)
        hl.setSpacing(8)
        self.heading = QLabel(title)
        theme.set_style(self.heading, "font-size:18px; font-weight:600;")
        hl.addWidget(self.heading)
        if concept:
            hl.addWidget(HelpButton(concept))
        hl.addWidget(tag_label(tag, must))
        hl.addStretch(1)
        self.body.addWidget(row)
        self.lead = QLabel(lead)
        self.lead.setWordWrap(True)
        theme.set_style(self.lead, "color:$INK2; font-size:14px; padding:12px 0 8px 0;")
        self.body.addWidget(self.lead)

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def sub_heading(self, text: str) -> QLabel:
        lab = QLabel(text)
        theme.set_style(lab, "color:$INK3; font-size:13px; padding:18px 0 2px 10px;")
        self.body.addWidget(lab)
        return lab

    def finish(self) -> None:
        self.body.addStretch(1)


class FieldRow(QFrame):
    """一个字段：标签（可带英文副标签）| 控件 | 说明。改过的行左侧标黄并在说明上方显示原值。"""

    _BASE = "QFrame#settingsField { border:none; border-left:3px solid transparent; background:transparent; }"
    _CHANGED = ("QFrame#settingsField { border:none; border-left:3px solid $WARN; background:$WARN_BG; }"
                "QLineEdit, QSpinBox { border-color:$WARN; }")

    def __init__(self, label: str, control: QWidget, note: str | QWidget = "", sub: str = "",
                 wide: bool = False, parent: QWidget | None = None):
        """wide：控件占满标签之后的整行（如“示例”框），不留说明列。"""
        super().__init__(parent)
        self.setObjectName("settingsField")
        self.changed = False
        theme.set_style(self, self._BASE)
        h = QHBoxLayout(self)
        h.setContentsMargins(10, 8, 8, 8)
        h.setSpacing(16)
        col = QWidget()
        col.setFixedWidth(LABEL_WIDTH)
        cl = QVBoxLayout(col)
        cl.setContentsMargins(0, 0, 0, 0)
        cl.setSpacing(0)
        self.label = QLabel(label)
        self.sub_label = QLabel(sub)
        self.sub_label.setObjectName("muted")
        if not sub:
            self.sub_label.hide()
        cl.addWidget(self.label)
        cl.addWidget(self.sub_label)
        h.addWidget(col)
        h.addWidget(control, 1 if wide else 0)
        right = QWidget()
        if wide:
            right.hide()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(2)
        self.was = QLabel("")
        self.was.setWordWrap(True)
        theme.set_style(self.was, "color:$WARN; font-size:13px;")
        self.was.hide()
        rl.addWidget(self.was)
        if isinstance(note, QWidget):
            self.note = note
        else:
            self.note = QLabel(note)
            self.note.setObjectName("muted")
            self.note.setWordWrap(True)
            if not note:
                self.note.hide()
        rl.addWidget(self.note)
        right.setMinimumWidth(160)
        h.addWidget(right, 1)

    def set_changed(self, changed: bool, was: str = "") -> None:
        if changed != self.changed:
            theme.set_style(self, self._CHANGED if changed else self._BASE)
        self.changed = changed
        self.was.setText(f"原为 {was}" if changed else "")
        self.was.setVisible(changed)


def chips(fields: list[str], per_row: int = 4) -> QWidget:
    """可用占位符的小标签，排成几行。"""
    w = QWidget()
    w.setToolTip("可用占位符")
    grid = QGridLayout(w)
    grid.setContentsMargins(0, 0, 0, 0)
    grid.setHorizontalSpacing(6)
    grid.setVerticalSpacing(6)
    for i, name in enumerate(fields):
        c = mono(QLabel("{" + name + "}"))
        theme.set_style(c, "color:$INK2; background:$CHASSIS; border:1px solid $LINE; border-radius:3px;"
                           " padding:1px 6px; font-size:12px;")
        grid.addWidget(c, i // per_row, i % per_row)
    grid.setColumnStretch(per_row, 1)
    return w
