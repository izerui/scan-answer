"""结果页的组成部件：“核对答卷 / 交付”模式切换、叠层图例、交付模式右栏的批次与转写统计。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .. import theme
from .common import Swatch

MODE_TIPS = {"check": "逐份核对：卡面叠层、客观题答案、信息栏与主观题（PgUp / PgDn 翻份）",
             "deliver": "交付：先检查交付，再生成交付目录；整批补做和放弃转写也在这里"}
_BADGE_STYLE = {"ERR": "background:$ERR_BG; color:$ERR;", "SUCCESS": "background:$NOTICE; color:$SUCCESS;",
                "WARN": "background:$WARN_BG; color:$WARN;", "": "background:$NOTICE; color:$INK3;"}


class ModeSwitch(QWidget):
    """顶栏中间的两段式模式切换；“交付”里带检查结论徽标。只在点了与当前不同的模式时发出 changed。"""
    changed = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("modeSwitch")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(self, "#modeSwitch { background:$CHASSIS; border:1px solid $LINE; border-radius:8px; }"
                              "#modeSwitch QPushButton { border:0; background:transparent; border-radius:6px;"
                              " padding:0 22px; min-height:34px; font-size:15px; color:$INK2; }"
                              "#modeSwitch QPushButton:hover { background:$HEADER; }"
                              "#modeSwitch QPushButton:checked { background:$PRIMARY; color:$PRIMARY_TEXT;"
                              " font-weight:600; }")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(3)
        self.group = QButtonGroup(self)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in (("check", "核对答卷"), ("deliver", "")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setChecked(key == "check")
            b.setToolTip(MODE_TIPS[key])
            b.clicked.connect(lambda _c=False, k=key: self._clicked(k))
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
        # “交付”按钮里放文字和徽标两个标签（按钮自身的文字无法分段着色）
        deliver = self.buttons["deliver"]
        inner = QHBoxLayout(deliver)
        inner.setContentsMargins(22, 0, 22, 0)
        inner.setSpacing(6)
        self.deliver_text = QLabel("交付")
        self.badge = QLabel("")
        for lab in (self.deliver_text, self.badge):
            lab.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            inner.addWidget(lab)
        self.badge.hide()
        self._tone = ""
        self._current = "check"
        self._sync()

    def _clicked(self, key: str) -> None:
        changed = key != self._current
        self._sync()
        if changed:
            self.changed.emit(key)

    def value(self) -> str:
        return "deliver" if self.buttons["deliver"].isChecked() else "check"

    def set_value(self, key: str) -> None:
        self.buttons[key].setChecked(True)
        self._sync()

    def set_badge(self, text: str, tone: str = "") -> None:
        self.badge.setText(text)
        self.badge.setVisible(bool(text))
        self._tone = tone
        self._sync()

    def _sync(self) -> None:
        """按钮里的标签不跟随按钮的选中样式，这里手动着色并撑开按钮宽度。"""
        self._current = self.value()
        on = self._current == "deliver"
        theme.set_style(self.deliver_text, "background:transparent; font-size:15px;"
                        + ("color:$PRIMARY_TEXT; font-weight:600;" if on else "color:$INK2;"))
        theme.set_style(self.badge, "border-radius:9px; padding:1px 7px; font-size:12px;"
                        + _BADGE_STYLE.get(self._tone, _BADGE_STYLE[""]))
        deliver = self.buttons["deliver"]
        deliver.setMinimumWidth(deliver.layout().sizeHint().width())


class OverlayLegend(QWidget):
    """卡面叠层图例：色块 + 含义。地方不够时（窄窗口）改用短说明，完整含义在悬停说明里。"""

    def __init__(self, items: list[tuple[str, str, str]], parent: QWidget | None = None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.head = QLabel("叠层：")
        self.head.setObjectName("muted")
        lay.addWidget(self.head)
        self._labels: list[tuple[QLabel, str, str]] = []
        for color, text, short in items:
            lay.addWidget(Swatch(color, 10))
            lab = QLabel(text)
            lab.setObjectName("muted")
            lay.addWidget(lab)
            lay.addSpacing(6)
            self._labels.append((lab, text, short))
        self.compact = False
        self.setToolTip("卡面上的彩色框是程序识别到的位置；红框处要对照原图核对。<br>"
                        + "　".join(text for _lab, text, _short in self._labels))

    def set_compact(self, compact: bool) -> None:
        if compact == self.compact:
            return
        self.compact = compact
        self.head.setVisible(not compact)
        for lab, text, short in self._labels:
            lab.setText(short if compact else text)
        self.updateGeometry()


class BatchSummary(QWidget):
    """交付模式右栏：这一批的答卷构成、转写状态，以及整批转写操作（按钮由结果页放入）。"""

    ROWS = (("total", "答卷", ""), ("ok", "正常", ""), ("warn", "有警告", "WARN"), ("bad", "异常卡 / 作废", ""))
    OCR_ROWS = (("ocr_failed", "转写失败", "ERR"), ("ocr_pending", "等待转写", "WARN"))

    def __init__(self, ocr_help: QWidget, actions: list[QWidget], note: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("batchSummary")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(self, "#batchSummary { background:$PANEL; border-left:1px solid $LINE; }"
                              "QLabel#kvKey { color:$INK3; } QLabel#h3 { font-weight:600; }"
                              "QWidget#kvRow { border-bottom:1px solid $LINE; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(6)
        self.values: dict[str, QLabel] = {}
        self._tones: dict[str, str] = {}
        lay.addWidget(self._h3("这一批"))
        for key, text, tone in self.ROWS:
            lay.addWidget(self._kv(key, text, tone))
        lay.addSpacing(10)
        lay.addWidget(self._h3("转写", ocr_help))
        for key, text, tone in self.OCR_ROWS:
            lay.addWidget(self._kv(key, text, tone))
        lay.addSpacing(10)
        for w in actions:
            lay.addWidget(w)
        sub = QLabel(note)
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        lay.addWidget(sub)
        lay.addStretch(1)

    @staticmethod
    def _h3(text: str, extra: QWidget | None = None) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 4)
        row.setSpacing(6)
        t = QLabel(text)
        t.setObjectName("h3")
        row.addWidget(t)
        if extra is not None:
            row.addWidget(extra)
        row.addStretch(1)
        return w

    def _kv(self, key: str, text: str, tone: str) -> QWidget:
        w = QWidget()
        w.setObjectName("kvRow")
        w.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 5, 0, 5)
        k = QLabel(text)
        k.setObjectName("kvKey")
        row.addWidget(k)
        row.addStretch(1)
        v = QLabel("—")
        row.addWidget(v)
        self.values[key] = v
        self._tones[key] = tone
        return w

    def set_value(self, key: str, value: int | None, unit: str, pending_text: str = "—") -> None:
        """value 为 None 时显示 pending_text（没有批次或还在统计）；非零时按该项的颜色突出。"""
        label = self.values[key]
        label.setText(pending_text if value is None else f"{value} {unit}")
        tone = self._tones[key] if value else ""
        theme.set_style(label, f"color:${tone};" if tone else "")
