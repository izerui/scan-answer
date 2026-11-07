"""界面主题：浅色冷灰机箱 + 白色工作面 + 区域色码（与设计正式稿一致）。"""
from __future__ import annotations

import sys

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication

# 基础色
CHASSIS = "#e4e7eb"
RAIL = "#23272d"
PANEL = "#f3f4f6"
WORK = "#ffffff"
LINE = "#d5d9de"
LINE2 = "#bfc5cc"
INK = "#15181c"
INK2 = "#3f464f"
INK3 = "#5b636d"

# 区域色码（贯穿所有页面）
REGION = {
    "id": "#1d5bbf",  # 考号
    "obj": "#15803d",  # 客观题
    "sub": "#b45309",  # 主观题
    "bar": "#6b3fb0",  # 条码
    "loc": "#15181c",  # 定位点 / 同步头
    "info": "#4e5a66",  # 信息栏 / 缺考
}
ERR = "#cc2f1d"  # 物理异常
WARN = "#9a5b00"  # 数据警告（文字）
WARN_FILL = "#e0a100"  # 数据警告（色块）
OK_FILL = "#b9dcc4"  # 拼图：成功
RUN_FILL = "#1d5bbf"  # 拼图：识别中

REGION_LABELS = [("id", "考号"), ("obj", "客观题"), ("sub", "主观题"), ("bar", "条码"),
                 ("loc", "定位点 / 同步头"), ("info", "信息栏 / 缺考")]


def qcolor(hex_: str, alpha: int = 255) -> QColor:
    c = QColor(hex_)
    c.setAlpha(alpha)
    return c


def ui_font(size: int = 14, bold: bool = False) -> QFont:
    family = "Microsoft YaHei UI" if sys.platform == "win32" else "PingFang SC"
    f = QFont(family)
    f.setPixelSize(size)
    f.setBold(bold)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


QSS = f"""
QWidget {{ color: {INK}; font-size: 14px; }}
QMainWindow, #chassis {{ background: {CHASSIS}; }}
#rail {{ background: {RAIL}; }}
#rail QToolButton {{ color: #c9ced4; background: transparent; border: none; border-radius: 4px;
    padding: 6px 2px; font-size: 12px; }}
#rail QToolButton:hover {{ background: #2f343b; color: #ffffff; }}
#rail QToolButton:checked {{ background: #ffffff; color: {INK}; }}
#header, #statusbar {{ background: #f7f8f9; }}
#header {{ border-bottom: 1px solid {LINE}; }}
#statusbar {{ border-top: 1px solid {LINE}; }}
#statusbar QLabel {{ color: {INK2}; font-size: 12px; }}
#panel {{ background: {PANEL}; }}
#work {{ background: {WORK}; }}
#sectionHeader {{ font-weight: 600; font-size: 14px; padding: 12px 16px; border-bottom: 1px solid {LINE}; }}
QLabel#muted {{ color: {INK3}; font-size: 12px; }}
QLabel#h1 {{ font-size: 22px; font-weight: 600; }}
QLabel#h2 {{ font-size: 16px; font-weight: 600; }}
QPushButton {{ background: #ffffff; border: 1px solid {LINE2}; border-radius: 3px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {INK2}; }}
QPushButton:disabled {{ color: #9aa1a9; background: #f1f2f4; }}
QPushButton#primary {{ background: {INK}; color: #ffffff; border-color: {INK}; }}
QPushButton#primary:hover {{ background: #2c3238; }}
QPushButton#big {{ font-size: 18px; font-weight: 600; min-height: 48px; background: {INK}; color: #ffffff;
    border-color: {INK}; }}
QPushButton#big:hover {{ background: #2c3238; }}
QPushButton#big:disabled {{ background: #9aa1a9; border-color: #9aa1a9; }}
QPushButton#bigHold {{ font-size: 18px; font-weight: 600; min-height: 48px; border: 2px solid {INK}; }}
QPushButton#danger {{ color: {ERR}; }}
QPushButton#seg {{ border-radius: 0; padding: 5px 12px; margin: 0; }}
QPushButton#seg:checked {{ background: {INK}; color: #ffffff; border-color: {INK}; }}
QLineEdit, QComboBox, QSpinBox {{ background: #ffffff; border: 1px solid {LINE2}; border-radius: 3px;
    padding: 5px 8px; min-height: 20px; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{ border-color: {REGION['id']}; }}
QTableView {{ background: #ffffff; border: none; gridline-color: {LINE}; selection-background-color: #e8eef9;
    selection-color: {INK}; }}
QHeaderView::section {{ background: #f7f8f9; color: {INK3}; border: none; border-bottom: 1px solid {LINE};
    padding: 6px 10px; font-size: 12px; }}
QScrollArea {{ border: none; background: transparent; }}
QToolTip {{ background: {INK}; color: #ffffff; border: none; padding: 4px 8px; }}
"""


def apply_theme(app: QApplication) -> None:
    app.setFont(ui_font(14))
    app.setStyleSheet(QSS)
