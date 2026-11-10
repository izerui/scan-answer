"""界面内引导：概念“?”按钮、使用说明窗口（文案在 ui/help_text.py）。"""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QToolButton,
                               QToolTip, QVBoxLayout, QWidget)

from .. import theme
from ..help_text import CONCEPTS, GUIDE_MD


class HelpButton(QToolButton):
    """难懂概念旁的“?”：悬停显示说明，点击立即弹出（不用等悬停延迟）。"""

    def __init__(self, concept: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.text_html = CONCEPTS[concept]
        self.setObjectName("helpButton")
        self.setText("?")
        self.setFixedSize(20, 20)
        self.setCursor(Qt.CursorShape.WhatsThisCursor)
        self.setToolTip(self.text_html)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        theme.set_style(self, "#helpButton { border: 1px solid $LINE2; border-radius: 10px; color: $INK3;"
                              " font-size: 11px; font-weight: 600; padding: 0; background: transparent; }"
                              "#helpButton:hover { color: $ACCENT; border-color: $ACCENT; }")
        self.clicked.connect(self.show_help)

    def show_help(self) -> None:
        QToolTip.showText(self.mapToGlobal(QPoint(0, self.height() + 2)), self.text_html, self)


def with_help(widget: QWidget, concept: str) -> QWidget:
    """把控件和它的“?”按钮放在一行，返回容器。"""
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(widget)
    lay.addWidget(HelpButton(concept))
    return w


class HelpDialog(QDialog):
    """使用说明窗口（F1）：功能与流程总览。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("使用说明")
        self.resize(760, 680)
        lay = QVBoxLayout(self)
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(False)
        self.browser.setMarkdown(GUIDE_MD)
        lay.addWidget(self.browser, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        ok = QPushButton("关闭")
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)
