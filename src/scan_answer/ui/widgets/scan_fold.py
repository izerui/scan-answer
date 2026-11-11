"""扫描页左栏的折叠行：点标题行展开 / 收起下面的内容（已作废、数据警告）。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .. import theme
from .guide import HelpButton

# 展开后内容区最多这么高，多出的部分在折叠行里自己滚动：左栏的物理异常列表优先占高度
FOLD_BODY_MAX = 140
FOLD_BODY_MIN = 32  # 窗口矮时内容区可以缩到约一行（滚动查看），不和物理异常列表抢高度


class FoldSection(QWidget):
    """标题行（三角 + 标题 + 可选“?” + 右侧说明）+ 可滚动的内容区，默认收起。

    title / note 是标题行上的两个标签，调用方直接改文字；内容放进 content_layout。"""

    def __init__(self, title: str, note: str = "", help: str | None = None, tip: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.head = QPushButton()
        self.head.setObjectName("foldHead")
        self.head.setCheckable(True)
        self.head.setCursor(Qt.CursorShape.PointingHandCursor)
        self.head.setToolTip(tip)
        # 按钮的高度由样式表决定（全局 min-height 会压过 setMinimumHeight），里面的标签靠子布局摆放
        theme.set_style(self.head, "#foldHead { border:none; border-top:1px solid $LINE; border-radius:0;"
                                   " background:transparent; padding:0; min-height:38px; max-height:38px; }"
                                   "#foldHead:hover { background:$HEADER; }")
        row = QHBoxLayout(self.head)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(6)
        self.arrow = QLabel("▸")
        theme.set_style(self.arrow, "color:$INK3; font-size:12px;")
        self.arrow.setFixedWidth(12)
        row.addWidget(self.arrow)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-size:14px;")
        row.addWidget(self.title)
        if help:
            row.addWidget(HelpButton(help))
        row.addStretch(1)
        self.note = QLabel(note)
        self.note.setObjectName("muted")
        row.addWidget(self.note)
        outer.addWidget(self.head)
        inner = QWidget()
        self.content_layout = QVBoxLayout(inner)
        self.content_layout.setContentsMargins(0, 0, 0, 0)
        self.content_layout.setSpacing(0)
        self.body = QScrollArea()
        self.body.setWidgetResizable(True)
        self.body.setWidget(inner)
        inner.setAutoFillBackground(False)  # setWidget 会打开自动填充，露出窗口底色
        self.body.setMinimumHeight(FOLD_BODY_MIN)
        # 不按内容的建议高度要位置：只分剩余高度（伸展系数比物理异常列表小）
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.body.setMaximumHeight(FOLD_BODY_MAX)
        self.body.setVisible(False)
        outer.addWidget(self.body, 1)
        self.head.toggled.connect(self._toggled)

    def _toggled(self, on: bool) -> None:
        self.arrow.setText("▾" if on else "▸")
        self.body.setVisible(on)
        # 展开时分一点左栏的剩余高度（内容区有上限，物理异常列表的伸展系数更大），收起时只剩标题行
        parent = self.parentWidget()
        lay = parent.layout() if parent is not None else None
        if lay is not None and lay.indexOf(self) >= 0:
            lay.setStretchFactor(self, 1 if on else 0)

    def is_open(self) -> bool:
        return self.head.isChecked()

    def set_open(self, on: bool) -> None:
        self.head.setChecked(on)
