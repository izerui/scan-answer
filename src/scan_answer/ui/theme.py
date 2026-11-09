"""System light/dark appearance; document pixels and annotation colors stay fixed."""
from __future__ import annotations

from pathlib import Path
from string import Template
import sys
import tempfile

from PySide6.QtCore import QAbstractListModel, QModelIndex, QObject, QPointF, QSignalBlocker, Qt, Slot
from PySide6.QtGui import QBrush, QColor, QFont, QImage, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import QAbstractItemView, QAbstractSpinBox, QApplication, QProxyStyle, QStyle

# Paper annotation colors are independent of the surrounding application theme.
REGION = {
    "id": "#1d5bbf", "obj": "#15803d", "sub": "#b45309",
    "bar": "#6b3fb0", "loc": "#15181c", "info": "#4e5a66",
}
PAPER_ERR = "#cc2f1d"
REGION_LABELS = [("id", "考号"), ("obj", "客观题"), ("sub", "主观题"), ("bar", "条码"),
                 ("loc", "定位点 / 同步头"), ("info", "信息栏 / 缺考")]

_LIGHT = {
    "CHASSIS": "#e4e7eb", "RAIL": "#23272d", "PANEL": "#f3f4f6", "WORK": "#ffffff",
    "HEADER": "#f7f8f9", "CANVAS": "#eef0f2", "NOTICE": "#e9ecef",
    "LINE": "#d5d9de", "LINE2": "#bfc5cc",
    "INK": "#15181c", "INK2": "#3f464f", "INK3": "#5b636d",
    "SELECTION": "#e8eef9", "ACCENT": "#1d5bbf", "SUCCESS": "#15803d",
    "ERR": "#cc2f1d", "ERR_BG": "#fdf0ee", "WARN": "#9a5b00", "WARN_BG": "#fdf6e3",
    "WARN_FILL": "#e0a100", "OK_FILL": "#b9dcc4", "RUN_FILL": "#1d5bbf",
    "DISABLED": "#65707c", "DISABLED_BG": "#f1f2f4",
    "PRIMARY": "#15181c", "PRIMARY_TEXT": "#ffffff", "PRIMARY_HOVER": "#2c3238",
    "RAIL_INK": "#c9ced4", "RAIL_HOVER": "#2f343b",
    "TOOLTIP_BG": "#15181c", "TOOLTIP_TEXT": "#ffffff",
    "TOGGLE_OFF": "#89929d", "TOGGLE_KNOB": "#ffffff", "TOGGLE_ON": "#15181c",
    **{f"REGION_TEXT_{key}": value for key, value in REGION.items()},
}
_DARK = {
    "CHASSIS": "#191a1c", "RAIL": "#17181a", "PANEL": "#232427", "WORK": "#2b2c2f",
    "HEADER": "#202124", "CANVAS": "#171819", "NOTICE": "#303236",
    "LINE": "#424449", "LINE2": "#6b6e75",
    "INK": "#f0f1f2", "INK2": "#cbd0d5", "INK3": "#b1b8c1",
    "SELECTION": "#33455d", "ACCENT": "#82b1ff", "SUCCESS": "#6fd296",
    "ERR": "#ff9e94", "ERR_BG": "#462a2b", "WARN": "#f1c878", "WARN_BG": "#3e3524",
    "WARN_FILL": "#e0a100", "OK_FILL": "#36684a", "RUN_FILL": "#487cbd",
    "DISABLED": "#a9afb8", "DISABLED_BG": "#35373b",
    "PRIMARY": "#e7e9ec", "PRIMARY_TEXT": "#191a1c", "PRIMARY_HOVER": "#ffffff",
    "RAIL_INK": "#c9ced4", "RAIL_HOVER": "#333539",
    "TOOLTIP_BG": "#393b40", "TOOLTIP_TEXT": "#f0f1f2",
    "TOGGLE_OFF": "#686e76", "TOGGLE_KNOB": "#ffffff", "TOGGLE_ON": "#487cbd",
    "REGION_TEXT_id": "#82b1ff", "REGION_TEXT_obj": "#6fd296", "REGION_TEXT_sub": "#f5b971",
    "REGION_TEXT_bar": "#bba3f3", "REGION_TEXT_loc": "#f0f1f2", "REGION_TEXT_info": "#bdc7d0",
}
_ARROW_DIR = Path(tempfile.gettempdir()) / "scan_answer_ui"


def _arrow_tokens(palette: dict[str, str]) -> dict[str, str]:
    """QSS 只能用图片画箭头：按主题色生成小三角，路径作为 $ARROW_* 填进样式表。"""
    out = {}
    for name, role in (("ARROW", "INK2"), ("ARROW_OFF", "DISABLED")):
        hex_ = palette[role].lstrip("#")
        for direction in ("up", "down"):
            out[f"{name}_{direction.upper()}"] = (_ARROW_DIR / f"{direction}_{hex_}.png").as_posix()
    return out


def _write_arrows(tokens: dict[str, str]) -> None:
    try:
        _ARROW_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:  # 临时目录不可写：箭头缺失但不影响使用
        return
    for key, path in tokens.items():
        if not key.startswith("ARROW") or Path(path).exists():
            continue
        name = Path(path).stem
        direction, hex_ = name.split("_", 1)
        base = Path(path)
        for scale, target in ((1, base), (2, base.with_name(f"{name}@2x.png"))):
            size = 10 * scale
            img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
            img.fill(Qt.GlobalColor.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(f"#{hex_}"))
            top, bottom = 3 * scale, 7 * scale
            pts = ((1 * scale, bottom), (9 * scale, bottom), (5 * scale, top)) if direction == "up" else \
                ((1 * scale, top), (9 * scale, top), (5 * scale, bottom))
            p.drawPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
            p.end()
            img.save(str(target))


_LIGHT.update(_arrow_tokens(_LIGHT))
_DARK.update(_arrow_tokens(_DARK))
_tokens = dict(_LIGHT)
globals().update(_tokens)
_STYLE_PROPERTY = "_scan_answer_theme_style"
_FOREGROUND_ROLE = int(Qt.ItemDataRole.UserRole) + 731
_BACKGROUND_ROLE = int(Qt.ItemDataRole.UserRole) + 732


def color(role: str) -> str:
    return role if role.startswith("#") else _tokens[role]


def qcolor(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(color(hex_))
    c.setAlpha(alpha)
    return c


def ui_font(size: int = 14, bold: bool = False) -> QFont:
    family = "Microsoft YaHei UI" if sys.platform == "win32" else "PingFang SC"
    f = QFont(family)
    f.setPixelSize(size)
    f.setBold(bold)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def set_style(widget, template: str) -> None:
    """Retain color roles instead of capturing the current scheme in a string."""
    widget.setProperty(_STYLE_PROPERTY, template)
    widget.setStyleSheet(Template(template).substitute(_tokens))


def set_item_colors(item, *, foreground: str | None = None, background: str | None = None,
                    column: int | None = None) -> None:
    """Tag item brushes so appearance changes can repaint without rebuilding data."""
    if column is not None:
        view = item.treeWidget()
        model = view.model() if view is not None else None
    elif hasattr(item, "model"):
        model = item.model()
    else:
        view = item.tableWidget()
        model = view.model() if view is not None else None
    blocker = QSignalBlocker(model) if model is not None else None
    try:
        for name, metadata_role, brush_role in (
            (foreground, _FOREGROUND_ROLE, Qt.ItemDataRole.ForegroundRole),
            (background, _BACKGROUND_ROLE, Qt.ItemDataRole.BackgroundRole),
        ):
            if name is not None:
                if column is None:
                    if hasattr(item, "model"):
                        item.setData(name, metadata_role)
                        item.setData(QBrush(qcolor(name)), brush_role)
                    else:
                        item.setData(metadata_role, name)
                        item.setData(brush_role, QBrush(qcolor(name)))
                else:
                    item.setData(column, metadata_role, name)
                    item.setData(column, brush_role, QBrush(qcolor(name)))
    finally:
        if blocker is not None:
            blocker.unblock()


def _refresh_model(model) -> None:
    with QSignalBlocker(model):
        parents = [QModelIndex()]
        while parents:
            parent = parents.pop()
            for row in range(model.rowCount(parent)):
                columns = 1 if isinstance(model, QAbstractListModel) else model.columnCount(parent)
                for column in range(columns):
                    index = model.index(row, column, parent)
                    for role, brush_role in ((_FOREGROUND_ROLE, Qt.ItemDataRole.ForegroundRole),
                                             (_BACKGROUND_ROLE, Qt.ItemDataRole.BackgroundRole)):
                        name = index.data(role)
                        if name:
                            model.setData(index, QBrush(qcolor(name)), brush_role)
                    if column == 0 and model.rowCount(index):
                        parents.append(index)


def _palette() -> QPalette:
    palette = QPalette()
    roles = {
        "Window": "CHASSIS", "WindowText": "INK", "Base": "WORK", "AlternateBase": "PANEL",
        "Text": "INK", "Button": "WORK", "ButtonText": "INK", "BrightText": "PRIMARY_TEXT",
        "Light": "LINE2", "Midlight": "LINE", "Mid": "LINE2", "Dark": "LINE", "Shadow": "RAIL",
        "Highlight": "SELECTION", "HighlightedText": "INK", "Link": "ACCENT",
        "LinkVisited": "REGION_TEXT_bar", "PlaceholderText": "INK3",
        "ToolTipBase": "TOOLTIP_BG", "ToolTipText": "TOOLTIP_TEXT", "Accent": "ACCENT",
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled):
        for role, token in roles.items():
            palette.setColor(group, getattr(QPalette.ColorRole, role), qcolor(token))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, qcolor("DISABLED"))
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(QPalette.ColorGroup.Disabled, role, qcolor("DISABLED_BG"))
    return palette


_STYLESHEET = """
QWidget { color: $INK; font-size: 14px; }
QMainWindow, #chassis { background: $CHASSIS; }
QDialog, QMessageBox { background: $PANEL; }
#rail { background: $RAIL; }
#rail QToolButton { color: $RAIL_INK; background: transparent; border: none; border-radius: 4px;
    padding: 6px 2px; font-size: 12px; }
#rail QToolButton:hover { background: $RAIL_HOVER; color: $RAIL_INK; }
#rail QToolButton:checked { background: $WORK; color: $INK; }
#header, #statusbar { background: $HEADER; }
#header { border-bottom: 1px solid $LINE; }
#statusbar { border-top: 1px solid $LINE; }
#statusbar QLabel { color: $INK2; font-size: 12px; }
#panel { background: $PANEL; }
#work { background: $WORK; }
#sectionHeader { font-weight: 600; font-size: 14px; padding: 12px 16px; border-bottom: 1px solid $LINE; }
QLabel#muted { color: $INK3; font-size: 12px; }
QLabel#h1 { font-size: 22px; font-weight: 600; }
QLabel#h2 { font-size: 16px; font-weight: 600; }
QPushButton { background: $WORK; border: 1px solid $LINE2; border-radius: 3px; padding: 5px 14px;
    min-height: 20px; }
QPushButton:hover { border-color: $INK2; background: $HEADER; }
QPushButton:pressed { background: $SELECTION; }
QPushButton::menu-indicator { image: url($ARROW_DOWN); width: 8px; height: 8px;
    subcontrol-origin: padding; subcontrol-position: center right; right: 6px; }
QPushButton:flat { border: none; background: transparent; }
QPushButton:flat:hover { background: $SELECTION; }
QPushButton:focus, QToolButton:focus { border: 1px solid $ACCENT; }
QPushButton:disabled { color: $DISABLED; background: $DISABLED_BG; }
QPushButton#primary { background: $PRIMARY; color: $PRIMARY_TEXT; border-color: $PRIMARY; }
QPushButton#primary:hover { background: $PRIMARY_HOVER; }
QPushButton#primary:pressed, QPushButton#big:pressed, QPushButton#seg:checked:pressed { background: $INK2; }
QPushButton#primary:disabled { color: $DISABLED; background: $DISABLED_BG; border-color: $LINE; }
QPushButton#big { font-size: 18px; font-weight: 600; min-height: 48px; background: $PRIMARY;
    color: $PRIMARY_TEXT; border-color: $PRIMARY; }
QPushButton#big:hover { background: $PRIMARY_HOVER; }
QPushButton#big:disabled { color: $DISABLED; background: $DISABLED_BG; border-color: $LINE; }
QPushButton#bigHold { font-size: 18px; font-weight: 600; min-height: 48px; border: 2px solid $INK; }
QPushButton#danger { color: $ERR; }
QPushButton#seg { border-radius: 0; padding: 5px 12px; min-height: 20px; margin: 0; }
QPushButton#seg:checked { background: $PRIMARY; color: $PRIMARY_TEXT; border-color: $PRIMARY; }
QPushButton#seg:disabled { color: $DISABLED; background: $DISABLED_BG; border-color: $LINE; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: $WORK; color: $INK;
    border: 1px solid $LINE2; border-radius: 3px; padding: 5px 8px; min-height: 20px;
    selection-background-color: $SELECTION; selection-color: $INK; }
QComboBox { padding-right: 26px; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: center right; width: 24px;
    border: none; }
QComboBox::down-arrow { image: url($ARROW_DOWN); width: 10px; height: 10px; }
QComboBox::down-arrow:disabled { image: url($ARROW_OFF_DOWN); }
QComboBox QAbstractItemView { border: 1px solid $LINE2; outline: 0; padding: 2px; }
QSpinBox, QDoubleSpinBox { padding: 5px 24px 5px 8px; }
QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: padding; subcontrol-position: top right;
    width: 20px; border: none; border-left: 1px solid $LINE; border-top-right-radius: 2px; background: $PANEL; }
QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: padding;
    subcontrol-position: bottom right; width: 20px; border: none; border-left: 1px solid $LINE;
    border-top: 1px solid $LINE; border-bottom-right-radius: 2px; background: $PANEL; }
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover { background: $SELECTION; }
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url($ARROW_UP); width: 8px; height: 8px; }
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url($ARROW_DOWN); width: 8px; height: 8px; }
QSpinBox::up-arrow:disabled, QSpinBox::up-arrow:off, QDoubleSpinBox::up-arrow:disabled,
QDoubleSpinBox::up-arrow:off { image: url($ARROW_OFF_UP); }
QSpinBox::down-arrow:disabled, QSpinBox::down-arrow:off, QDoubleSpinBox::down-arrow:disabled,
QDoubleSpinBox::down-arrow:off { image: url($ARROW_OFF_DOWN); }
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus { border-color: $ACCENT; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled {
    color: $DISABLED; background: $DISABLED_BG; }
QPlainTextEdit, QTextEdit { background: $WORK; color: $INK; border: 1px solid $LINE;
    selection-background-color: $SELECTION; selection-color: $INK; }
QPlainTextEdit:disabled, QTextEdit:disabled { color: $DISABLED; background: $DISABLED_BG; }
QAbstractItemView { background: $WORK; color: $INK; alternate-background-color: $PANEL;
    selection-background-color: $SELECTION; selection-color: $INK; }
QTableView { border: none; gridline-color: $LINE; }
QTableView::item { padding: 0 6px; border-bottom: 1px solid $LINE; }
QAbstractItemView::item:selected { background: $SELECTION; color: $INK; }
QHeaderView::section { background: $HEADER; color: $INK3; border: none; border-bottom: 1px solid $LINE;
    padding: 6px 10px; font-size: 12px; font-weight: 600; }
QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget#qt_scrollarea_viewport { background: transparent; }
QToolTip { background: $TOOLTIP_BG; color: $TOOLTIP_TEXT; border: 1px solid $LINE; padding: 4px 8px; }
QMenu { background: $WORK; color: $INK; border: 1px solid $LINE2; }
QMenu::item:selected { background: $SELECTION; color: $INK; }
QMenu::item:disabled { color: $DISABLED; }
QToolButton { color: $INK; }
QToolButton:disabled, QCheckBox:disabled, QRadioButton:disabled, QLabel:disabled { color: $DISABLED; }
QTabWidget::pane { background: $WORK; border: 1px solid $LINE; }
QTabBar::tab { background: $PANEL; color: $INK2; border: 1px solid $LINE; padding: 6px 12px; }
QTabBar::tab:selected { background: $WORK; color: $INK; border-bottom-color: $ACCENT; }
QTabBar::tab:disabled { color: $DISABLED; }
QGroupBox { border: 1px solid $LINE; margin-top: 10px; padding-top: 8px; }
QGroupBox::title { color: $INK; subcontrol-origin: margin; padding: 0 4px; }
QProgressBar { background: $PANEL; color: $INK; border: 1px solid $LINE; text-align: center; }
QProgressBar::chunk { background: $OK_FILL; }
QSplitter::handle { background: $LINE; }
"""
QSS = Template(_STYLESHEET).substitute(_tokens)


class AppStyle(QProxyStyle):
    """沿用系统原生控件，只统一表单布局规则：macOS 默认把表单居中、输入框保持最小宽度，
    和 Windows 的表现差别很大，这里统一为左对齐、字段撑满。"""

    _HINTS = {
        QStyle.StyleHint.SH_FormLayoutFieldGrowthPolicy: 2,  # QFormLayout.AllNonFixedFieldsGrow
        QStyle.StyleHint.SH_FormLayoutFormAlignment:
            (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop).value,
        QStyle.StyleHint.SH_FormLayoutLabelAlignment:
            (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter).value,
        QStyle.StyleHint.SH_FormLayoutWrapPolicy: 0,  # QFormLayout.DontWrapRows
    }

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint in self._HINTS:
            return self._HINTS[hint]
        return super().styleHint(hint, option, widget, data)

    def polish(self, target):
        # macOS 样式计算数字框文字区时忽略 QSS 左内边距，文字会贴边
        if isinstance(target, QAbstractSpinBox) and target.lineEdit() is not None:
            target.lineEdit().setTextMargins(6, 0, 0, 0)
        return super().polish(target)


class ThemeManager(QObject):
    def __init__(self, app: QApplication):
        super().__init__(app)
        self.app = app
        self.scheme = Qt.ColorScheme.Unknown
        self._applying = False
        app.styleHints().colorSchemeChanged.connect(self.apply_scheme, Qt.ConnectionType.QueuedConnection)

    @Slot(Qt.ColorScheme)
    def apply_scheme(self, scheme: Qt.ColorScheme) -> None:
        global QSS
        if self._applying:
            return
        if scheme == Qt.ColorScheme.Unknown:
            native = self.app.style().standardPalette().color(QPalette.ColorRole.Window)
            scheme = Qt.ColorScheme.Dark if native.lightnessF() < .5 else Qt.ColorScheme.Light
        self._applying = True
        try:
            self.scheme = scheme
            _tokens.clear()
            _tokens.update(_DARK if scheme == Qt.ColorScheme.Dark else _LIGHT)
            globals().update(_tokens)
            _write_arrows(_tokens)
            QSS = Template(_STYLESHEET).substitute(_tokens)
            self.app.setPalette(_palette())
            self.app.setStyleSheet(QSS)
            models = set()
            widgets = self.app.allWidgets()
            for widget in widgets:
                template = widget.property(_STYLE_PROPERTY)
                if template:
                    widget.setStyleSheet(Template(template).substitute(_tokens))
                if isinstance(widget, QAbstractItemView) and widget.model() is not None:
                    models.add(widget.model())
            for model in models:
                _refresh_model(model)
            for widget in widgets:
                refresh = getattr(widget, "refresh_theme", None)
                if refresh is not None:
                    refresh()
                widget.update()
        finally:
            self._applying = False


def apply_theme(app: QApplication) -> ThemeManager:
    manager = getattr(app, "_scan_answer_theme_manager", None)
    if manager is None:
        manager = ThemeManager(app)
        app._scan_answer_theme_manager = manager
    if not isinstance(app.style(), AppStyle):
        style = AppStyle(app.style().name())
        app.setStyle(style)
        app._scan_answer_style = style
    app.setFont(ui_font(14))
    manager.apply_scheme(app.styleHints().colorScheme())
    return manager
