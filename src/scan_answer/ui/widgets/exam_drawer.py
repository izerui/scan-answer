"""考试页“新建考试”抽屉：从页面右侧滑出的面板，背后一层半透明遮罩；点遮罩、按 Esc 或点关闭都会收起。"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QFrame, QWidget

from .. import theme


class _Scrim(QWidget):
    clicked = Signal()

    def paintEvent(self, _e) -> None:
        QPainter(self).fillRect(self.rect(), theme.qcolor("CHASSIS", 165))

    def mousePressEvent(self, e) -> None:
        self.clicked.emit()
        e.accept()


class Drawer(QFrame):
    """贴在 host 右侧、与 host 等高的面板。内容由调用方往 layout 里放。"""
    closed = Signal()

    def __init__(self, host: QWidget, blocked: QWidget, width: int = 460):
        """blocked：抽屉打开期间不可操作、拿不到焦点的背后内容。"""
        super().__init__(host)
        self.host = host
        self.blocked = blocked
        self.setObjectName("drawer")
        self.setFixedWidth(width)
        theme.set_style(self, "#drawer { background: $PANEL; border-left: 1px solid $LINE2; }")
        self.scrim = _Scrim(host)
        self.scrim.clicked.connect(self.close_drawer)
        self.scrim.hide()
        self.hide()
        host.installEventFilter(self)

    def eventFilter(self, obj: QObject, e: QEvent) -> bool:  # noqa: N802 — Qt 方法名
        if obj is self.host and e.type() == QEvent.Type.Resize:
            self._place()
        return False

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Escape:  # 输入框不处理的 Esc 传到这里
            self.close_drawer()
            return
        super().keyPressEvent(e)

    def _place(self) -> None:
        r = self.host.rect()
        self.scrim.setGeometry(r)
        self.setGeometry(r.right() - self.width() + 1, 0, self.width(), r.height())

    def open_drawer(self) -> None:
        self._place()
        self.blocked.setEnabled(False)  # 抽屉是模态的：Tab、点击都到不了遮罩后面
        self.scrim.show()
        self.scrim.raise_()
        self.show()
        self.raise_()

    def close_drawer(self) -> None:
        if self.isHidden():
            return
        self.hide()
        self.scrim.hide()
        self.blocked.setEnabled(True)
        self.closed.emit()
