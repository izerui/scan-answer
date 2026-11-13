"""设置页：顶部常驻保存条 + 左侧分节目录（输出、扫描默认参数、大模型转写、区域色码），右侧一次显示一节。"""
from __future__ import annotations

import copy
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QSpinBox, QStackedWidget, QVBoxLayout, QWidget)

from ...app.settings import AppSettings, save_settings
from ...ocr.client import OcrClient
from ...ocr.config import OcrConfig
from ...output.naming import DEFAULT_EXAM_PATTERN, EXAM_FIELDS, STUDENT_FIELDS, sample_names, unknown_fields
from .. import theme
from ..help_text import CROP_OPTIONS, DOUBLE_FEED_TIP, DROPOUT_OPTIONS, DUPLEX_OPTIONS, SCAN_PARAM_LABELS
from ..widgets.common import RegionLegend, Segmented, Toggle
from ..widgets.guide import with_help
from ..widgets.settings_sections import FieldRow, NavItem, SectionPage, chips, dot_label, mono


TEST_TIP = "用上面填的地址、密钥、模型发一次请求，检查能不能连上（不用先保存）"
DIRTY_TEXT = "有未保存的修改，点“保存设置”后才生效"
# 分节：键 -> (目录标题, 目录标签, 节标题旁标签, 节说明, “?”概念)
SECTIONS = {
    "out": ("输出", "必设", "首次必设", "所有考试和结果存放的位置，一定要确认。命名规则只影响之后新建的考试和批次。", None),
    "scan": ("扫描默认参数", "用扫描仪时", "只在用扫描仪时需要",
             "从文件导入时用不到这些（“同时识别几张”除外）。彩色印刷的卡可用“去掉彩色印刷”。"
             "扫描页“修改扫描参数…”里改过的参数，开始扫描时也会更新到这里。", None),
    "ocr": ("大模型转写", "可选", "可选", "填好地址、密钥、模型后点“测试连接”。关闭转写不会丢掉下面填的配置。", "ocr"),
    "legend": ("区域色码", "参考", "只读参考", "模板页、扫描页和结果页卡面上的识别标记（彩色框）都用这些颜色。", None),
}
# 各节的字段（顺序即未保存列表里的顺序）及其名称
SECTION_FIELDS = {
    "out": ["output_root", "operator", "exam_pattern", "student_pattern"],
    "scan": ["dpi", "color", "duplex", "double_feed", "dropout", "crop", "workers"],
    "ocr": ["ocr_on", "base_url", "api_key", "model", "concurrency"],
    "legend": [],
}
FIELD_LABELS = {
    "output_root": "输出根目录", "operator": "默认操作员", "exam_pattern": "考试目录命名", "student_pattern": "学生目录命名",
    **{k: SCAN_PARAM_LABELS[k] for k in ("dpi", "color", "duplex", "double_feed", "dropout", "crop")},
    "workers": "同时识别几张",
    "ocr_on": "启用转写", "base_url": "接口地址", "api_key": "密钥", "model": "模型", "concurrency": "同时转写几项",
}
DIRTY_LIST_MAX = 4  # 保存条里最多列出几项，其余在悬停说明里


def _tip(widget: QWidget, text: str, options: dict[str, str] | None = None) -> None:
    """设置悬停说明；分段按钮的每个选项也带上说明（options 为各选项的补充说明）。"""
    widget.setToolTip(text)
    if isinstance(widget, Segmented):
        for key, b in widget.buttons.items():
            extra = (options or {}).get(key)
            b.setToolTip(f"{b.text()}：{extra}" if extra else text)


class _PingSignals(QObject):
    done = Signal(bool, str)


class SettingsPage(QWidget):
    saved = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self._ping = _PingSignals()
        self._ping.done.connect(self._ping_done)
        self._build()
        self.load()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._save_bar())
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)
        self.field_rows: dict[str, FieldRow] = {}
        self.nav_items: dict[str, NavItem] = {}
        self.section_pages: dict[str, SectionPage] = {}
        self.sections = QStackedWidget()
        self._nav_group = QButtonGroup(self)
        nav = QWidget()
        nav.setObjectName("settingsToc")
        nav.setFixedWidth(264)
        nav.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        theme.set_style(nav, "#settingsToc { background:$PANEL; border-right:1px solid $LINE; }")
        nl = QVBoxLayout(nav)
        nl.setContentsMargins(10, 12, 10, 12)
        nl.setSpacing(4)
        for key, (title, tag, head_tag, lead, concept) in SECTIONS.items():
            item = NavItem(title, tag, must=key == "out")
            item.clicked.connect(lambda _c=False, k=key: self.show_section(k))
            self._nav_group.addButton(item)
            self.nav_items[key] = item
            nl.addWidget(item)
            page = SectionPage(title, head_tag, lead, concept, must=key == "out")
            self.section_pages[key] = page
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            holder = QWidget()
            holder.setObjectName("work")
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.addWidget(page, 1)
            hl.addStretch(0)
            scroll.setWidget(holder)
            self.sections.addWidget(scroll)
        nl.addStretch(1)
        foot = QLabel("扫描页会记住上次用的扫描参数。\n没保存就离开本页时，会询问保存、放弃还是留下。")
        foot.setWordWrap(True)
        theme.set_style(foot, "color:$INK3; font-size:12px; padding:10px 12px; border-top:1px solid $LINE;")
        nl.addWidget(foot)
        body.addWidget(nav)
        body.addWidget(self.sections, 1)
        self._build_output(self.section_pages["out"])
        self._build_scan(self.section_pages["scan"])
        self._build_ocr(self.section_pages["ocr"])
        self._build_legend(self.section_pages["legend"])
        for page in self.section_pages.values():
            page.finish()
        self.show_section("out")
        # 改了表单就在保存条列出未保存的项
        self._loading = False
        for seg in (self.dpi, self.color, self.duplex, self.dropout, self.crop):
            seg.changed.connect(lambda _k: self._refresh_dirty())
        for tg in (self.double_feed, self.ocr_on):
            tg.toggled.connect(lambda _on: self._refresh_dirty())
        for sp in (self.workers, self.concurrency):
            sp.valueChanged.connect(lambda _v: self._refresh_dirty())
        for ed in (self.base_url, self.api_key, self.model, self.output_root, self.operator,
                   self.exam_pattern, self.student_pattern):
            ed.textChanged.connect(lambda _t: self._refresh_dirty())

    def _save_bar(self) -> QWidget:
        """顶部常驻条：页标题、说明、未保存的项、保存设置。"""
        bar = QWidget()
        bar.setObjectName("settingsBar")
        bar.setMinimumHeight(52)
        theme.set_style(bar, "#settingsBar { background: $HEADER; border-bottom: 1px solid $LINE; }")
        btns = QHBoxLayout(bar)
        btns.setContentsMargins(24, 8, 24, 8)
        btns.setSpacing(12)
        title = QLabel("设置")
        theme.set_style(title, "font-size:18px; font-weight:600;")
        btns.addWidget(title)
        sub = QLabel("首次使用时设置一次，改完点“保存设置”才生效")
        theme.set_style(sub, "color:$INK3; font-size:13px;")
        btns.addWidget(sub)
        btns.addStretch(1)
        self.dirty_chip = QWidget()
        self.dirty_chip.setObjectName("dirtyChip")
        self.dirty_chip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        chip = QHBoxLayout(self.dirty_chip)
        chip.setContentsMargins(10, 4, 10, 4)
        chip.setSpacing(8)
        self.dirty_dot = dot_label(DIRTY_TEXT)
        chip.addWidget(self.dirty_dot)
        self.save_status = QLabel("")
        chip.addWidget(self.save_status)
        btns.addWidget(self.dirty_chip)
        self._show_status("")
        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("primary")
        self.save_btn.setToolTip("保存本页所有修改并立即生效；没保存就离开设置页时会询问")
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.save_btn)
        return bar

    def _row(self, page: SectionPage, key: str, control: QWidget, note: str | QWidget = "",
             label: str | None = None, sub: str = "") -> FieldRow:
        row = FieldRow(label or FIELD_LABELS[key], control, note, sub)
        self.field_rows[key] = row
        page.add(row)
        return row

    @staticmethod
    def _line(*ws: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(8)
        for x in ws:
            h.addWidget(x)
        return w

    def _build_output(self, page: SectionPage) -> None:
        self.output_root = QLineEdit()
        self.output_root.setFixedWidth(460)
        _tip(self.output_root, "所有考试和结果存放的文件夹。改了之后考试页列出的是新目录里的考试，旧目录里的不会搬过来")
        self.browse_btn = QPushButton("浏览")
        self.browse_btn.setToolTip("选择输出根目录")
        self.browse_btn.clicked.connect(self._browse)
        self._row(page, "output_root", self._line(self.output_root, self.browse_btn), "旧目录里的考试不会搬过来")
        self.operator = QLineEdit()
        self.operator.setFixedWidth(220)
        _tip(self.operator, "新建考试时“操作员”一栏默认填这个名字，写入结果")
        self._row(page, "operator", self.operator, "新建考试时“操作员”默认填这个名字")
        page.sub_heading("目录命名")
        self.exam_pattern = mono(QLineEdit())
        self.exam_pattern.setFixedWidth(340)
        self.exam_pattern.setPlaceholderText(DEFAULT_EXAM_PATTERN)
        _tip(self.exam_pattern, "新建考试时考试文件夹的名字。{name} 等占位符会换成考试信息；留空用默认规则")
        self._row(page, "exam_pattern", with_help(self.exam_pattern, "dir_patterns"),
                  chips(list(EXAM_FIELDS), "可用："))
        self.student_pattern = mono(QLineEdit())
        self.student_pattern.setFixedWidth(340)
        _tip(self.student_pattern, "每个学生结果文件夹名里序号之后的部分，如 {exam_no}＝考号；留空只用序号")
        self._row(page, "student_pattern", with_help(self.student_pattern, "dir_patterns"),
                  "序号之后的部分，可用：{exam_no} {barcode}；留空则只用序号")
        self.naming_sample = mono(QLabel(""))
        self.naming_sample.setWordWrap(True)
        self.naming_sample.setToolTip("按上面两条规则，用示例考试信息生成的目录名")
        theme.set_style(self.naming_sample, "color:$INK2; background:$CHASSIS; border:1px solid $LINE;"
                                            " border-radius:5px; padding:8px 12px; font-size:13px;")
        page.add(FieldRow("示例", self.naming_sample, wide=True))
        self.exam_pattern.textChanged.connect(self._update_naming_sample)
        self.student_pattern.textChanged.connect(self._update_naming_sample)

    def _build_scan(self, page: SectionPage) -> None:
        self.dpi = Segmented([(str(d), str(d)) for d in (200, 300, 400, 600)], "300")
        _tip(self.dpi, "扫描仪的扫描精度，越高越清楚，但越慢、文件越大。只在用扫描仪时生效",
             {"200": "最快，填涂识别够用", "300": "推荐，识别和切图都够清楚",
              "400": "手写稍清楚一些", "600": "最清楚但很慢，只在对手写清晰度要求高时用"})
        self.color = Segmented([("gray", "灰度"), ("color", "彩色")], "gray")
        _tip(self.color, "扫描的色彩模式。只在用扫描仪时生效",
             {"gray": "推荐，文件小、识别快", "color": "保留颜色；扫描仪去不干净彩色印刷时改用彩色，由软件去色"})
        self.duplex = Segmented(DUPLEX_OPTIONS, "1")
        _tip(self.duplex, "扫描仪一次扫纸的一面还是两面。只在用扫描仪时生效",
             {"1": "正反面都扫（双面答题卡选这个）", "0": "只扫正面"})
        self.double_feed = Toggle(True)
        _tip(self.double_feed, DOUBLE_FEED_TIP)
        self._row(page, "dpi", self.dpi, "300 足够识别；600 仅用于对手写清晰度要求高的场景")
        self._row(page, "color", self.color, "灰度文件小、识别快")
        self._row(page, "duplex", self.duplex, "双面答题卡选“双面”")
        self._row(page, "double_feed", self.double_feed, "检测到两张纸粘在一起进纸时自动暂停并提示")
        self.dropout = Segmented(DROPOUT_OPTIONS, "none")
        _tip(self.dropout, "去掉卡面上某种颜色的印刷（底纹、选项框），只留下涂黑。黑色印刷的卡选“不去掉”",
             {"none": "黑色印刷的卡选这个", "red": "去掉卡面上的红色印刷", "green": "去掉卡面上的绿色印刷",
              "blue": "去掉卡面上的蓝色印刷"})
        self._row(page, "dropout", with_help(self.dropout, "dropout"),
                  "去掉卡面上该颜色的印刷（底纹、选项框），只留下涂黑")
        self.crop = Segmented(CROP_OPTIONS, "auto")
        _tip(self.crop, "扫描时怎么裁掉纸张以外的边。只在用扫描仪时生效",
             {"auto": "推荐：扫描仪自动找纸边", "template": "按模板的纸张尺寸裁",
              "driver": "不额外设置，用扫描仪驱动里的默认裁切"})
        self._row(page, "crop", with_help(self.crop, "crop"),
                  "推荐“自动找纸边”；不准时改用“按模板尺寸”")
        page.sub_heading("识别（导入文件时也用）")
        self.workers = QSpinBox()
        self.workers.setRange(0, 16)
        self.workers.setMinimumWidth(80)
        _tip(self.workers, "同时识别几张卡。一般保持默认；电脑变卡就调小，设为 0 时一张一张识别（最慢、最省内存）")
        self._row(page, "workers", with_help(self.workers, "workers"),
                  "一般保持默认。电脑变卡就调小，设为 0 时一张一张识别（最慢、最省内存）")

    def _build_ocr(self, page: SectionPage) -> None:
        self.ocr_on = Toggle(False)
        _tip(self.ocr_on, "打开后，扫描时在后台用大模型把手写内容转成文字。关闭也不会丢掉下面填的配置")
        self._row(page, "ocr_on", self.ocr_on, "可选。未启用时主观题只切图；启用后扫描时在后台转写，不影响扫描速度",
                  label="启用")
        self.base_url = mono(QLineEdit())
        self.base_url.setFixedWidth(520)
        self.base_url.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = mono(QLineEdit())
        self.api_key.setFixedWidth(456)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_key_btn = QPushButton("显示")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setToolTip("按下显示密钥明文，再点一次隐藏")
        self.show_key_btn.toggled.connect(lambda on: self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.model = mono(QLineEdit())
        self.model.setFixedWidth(260)
        self.model.setPlaceholderText("qwen-vl-max")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        self.concurrency.setMinimumWidth(80)
        _tip(self.base_url, "大模型服务的接口地址（OpenAI 兼容格式），向服务商获取")
        _tip(self.api_key, "大模型服务的密钥，向服务商获取；只保存在本机设置里")
        _tip(self.model, "要用的模型名，需支持看图（视觉模型），如 qwen-vl-max")
        _tip(self.concurrency, "同时转写几项（同时发给大模型的请求数）。越大越快，但可能超出服务商的限流")
        self._row(page, "base_url", self.base_url, "向服务商获取，OpenAI 兼容格式")
        self._row(page, "api_key", self._line(self.api_key, self.show_key_btn), "只保存在本机设置里")
        self._row(page, "model", self.model, "需支持看图的视觉模型")
        self._row(page, "concurrency", self.concurrency, "越大越快，但可能超出服务商的限流")
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setToolTip(TEST_TIP)
        self.test_btn.clicked.connect(self.test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        self.test_result.setMaximumWidth(320)
        for w in (self.base_url, self.api_key, self.model):
            w.textChanged.connect(self._clear_test_result)  # 配置改了，旧的测试结果不再可信
        page.add(FieldRow("连接", self._line(self.test_btn, self.test_result),
                          "不用先保存；改了地址、密钥或模型后旧结果自动清除"))

    @staticmethod
    def _build_legend(page: SectionPage) -> None:
        legend = RegionLegend(extra=[
            ("ERR", "异常卡（定位失败、缺面等）"),
            ("WARN", "有警告（多涂、空白等）"),
        ])
        legend.layout().setContentsMargins(10, 16, 0, 4)
        legend.setMaximumWidth(520)
        page.add(legend)

    # ---------- 分节 ----------
    def show_section(self, key: str) -> None:
        self.sections.setCurrentIndex(list(SECTIONS).index(key))
        self.nav_items[key].setChecked(True)

    def current_section(self) -> str:
        return list(SECTIONS)[self.sections.currentIndex()]

    # ---------- 读写 ----------
    def load(self) -> None:
        """从当前设置重新填表（每次进入设置页都调用：扫描页会记住扫描参数并写设置）。"""
        self._loading = True
        s = self.settings
        self.dpi.set_value(str(s.scan.dpi))
        self.color.set_value(s.scan.color)
        self.duplex.set_value("1" if s.scan.duplex else "0")
        self.double_feed.setChecked(s.scan.double_feed)
        self.workers.setValue(s.workers)
        o = s.ocr_form_values()  # 转写关闭时显示保留的连接配置
        self.ocr_on.setChecked(bool(s.ocr))
        self.base_url.setText(o.get("base_url", ""))
        self.api_key.setText(o.get("api_key", ""))
        self.model.setText(o.get("model", ""))
        self.concurrency.setValue(int(o.get("concurrency", 4)))
        self.output_root.setText(s.output_root)
        self.operator.setText(s.operator)
        self.dropout.set_value(s.scan.dropout)
        self.crop.set_value(s.scan.crop)
        self.exam_pattern.setText(s.exam_dir_pattern)
        self.student_pattern.setText(s.student_dir_pattern)
        self._update_naming_sample()
        self._loaded = self._form_values()
        self._loading = False
        self._show_status("")
        self._refresh_dirty()

    def _show_status(self, text: str, dirty: bool = False, tip: str = "") -> None:
        """保存条右侧：有未保存的项时是带圆点的黄色提示，否则是保存结果（或空）。"""
        self._status_dirty = dirty
        self.save_status.setText(text)
        self.save_status.setToolTip(tip)
        self.dirty_chip.setToolTip(tip)
        self.dirty_dot.setVisible(dirty)
        theme.set_style(self.save_status, "color:$WARN; font-size:13px;" if dirty else "color:$INK3; font-size:13px;")
        theme.set_style(self.dirty_chip, "#dirtyChip { background:$WARN_BG; border:1px solid $WARN; border-radius:5px; }"
                        if dirty else "#dirtyChip { background:transparent; border:none; }")

    def _refresh_dirty(self) -> None:
        """按“表单当前值 vs 上次载入/保存的值”刷新：保存条列出的项、目录圆点与摘要、各字段的“原为…”。"""
        if getattr(self, "_loading", True) or not hasattr(self, "_loaded"):
            return
        values = self._form_values()
        changed = {k for k, v in values.items() if self._loaded.get(k) != v}
        names = [FIELD_LABELS[k] for keys in SECTION_FIELDS.values() for k in keys if k in changed]
        if names:
            shown = "、".join(names[:DIRTY_LIST_MAX]) + ("等" if len(names) > DIRTY_LIST_MAX else "")
            self._show_status(f"{len(names)} 项未保存：{shown}", dirty=True,
                              tip=f"{DIRTY_TEXT}；没保存就离开设置页时会询问。\n未保存的项：{'、'.join(names)}")
        elif self._status_dirty:
            self._show_status("")
        for key, row in self.field_rows.items():
            row.set_changed(key in changed, self._display(key, self._loaded.get(key)))
        for key, item in self.nav_items.items():
            item.set_dirty(any(k in changed for k in SECTION_FIELDS[key]))
        self._update_summaries(values, changed)

    def _display(self, key: str, value) -> str:
        """字段取值的显示文字（“原为 …”用）：分段按钮用选项名，开关用开/关，密钥不显示明文。"""
        if key in ("dpi", "color", "duplex", "dropout", "crop"):
            seg = getattr(self, key)
            return seg.buttons[value].text() if value in seg.buttons else str(value)
        if isinstance(value, bool):
            return "开" if value else "关"
        if value in ("", None):
            return "空"
        if key == "api_key":
            return "另一个密钥（不显示明文）"
        return str(value)

    def _update_summaries(self, v: dict, changed: set[str]) -> None:
        root = v["output_root"].strip() or "未设置"
        # “已修改”放在前面：路径太长被省略时标记仍看得见
        self.nav_items["out"].set_summary(("已修改 · " if "output_root" in changed else "") + root)
        self.nav_items["scan"].set_summary(
            f"{v['dpi']} DPI · {self._display('color', v['color'])} · {self._display('duplex', v['duplex'])}"
            f" · 同时识别 {v['workers']} 张")
        model = v["model"].strip()
        if v["ocr_on"]:
            ocr = f"已启用 · {model or '未填模型'} · 同时转写 {v['concurrency']} 项"
        else:
            ocr = f"未启用 · 已保留 {model} 的配置" if model else "未启用"
        self.nav_items["ocr"].set_summary(ocr)
        self.nav_items["legend"].set_summary("各类区域和异常在界面上的颜色")

    def _form_values(self) -> dict:
        """表单当前值；与 load() 时的快照比较，得到用户实际改动的项。"""
        return {"dpi": self.dpi.value(), "color": self.color.value(), "duplex": self.duplex.value(),
                "double_feed": self.double_feed.isChecked(), "dropout": self.dropout.value(),
                "crop": self.crop.value(), "workers": self.workers.value(), "ocr_on": self.ocr_on.isChecked(),
                "base_url": self.base_url.text(), "api_key": self.api_key.text(), "model": self.model.text(),
                "concurrency": self.concurrency.value(), "output_root": self.output_root.text(),
                "operator": self.operator.text(), "exam_pattern": self.exam_pattern.text(),
                "student_pattern": self.student_pattern.text()}

    def changed_keys(self) -> set[str]:
        return {k for k, v in self._form_values().items() if self._loaded.get(k) != v}

    def is_dirty(self) -> bool:
        return bool(self.changed_keys())

    def can_leave(self) -> bool:
        """离开设置页前：有未保存的修改时询问保存 / 放弃 / 取消。返回 False 表示留在设置页。"""
        if not self.is_dirty():
            return True
        b = QMessageBox.StandardButton
        answer = QMessageBox.question(self, "设置未保存", "设置有未保存的修改，是否保存？",
                                      b.Save | b.Discard | b.Cancel, b.Save)
        if answer in (b.Save, b.Yes):
            return self.save()
        if answer in (b.Discard, b.No):
            self.load()
            return True
        return False

    def _update_naming_sample(self) -> None:
        bad = (unknown_fields(self.exam_pattern.text(), EXAM_FIELDS)
               + unknown_fields(self.student_pattern.text(), STUDENT_FIELDS))
        if bad:
            self.naming_sample.setText("无法识别的占位符：" + "、".join(bad))
            return
        exam, student = sample_names(self.exam_pattern.text() or DEFAULT_EXAM_PATTERN, self.student_pattern.text())
        self.naming_sample.setText(f"{exam} / batch_…/students/{student}")

    def form_ocr_config(self) -> OcrConfig:
        cfg = OcrConfig(self.base_url.text().strip(), self.api_key.text().strip(), self.model.text().strip(),
                        concurrency=self.concurrency.value())
        cfg.validate()
        return cfg

    def _bad_ocr_field(self) -> str:
        """转写配置校验失败时出错的字段（与 OcrConfig.validate 的检查顺序一致）。"""
        if not self.base_url.text().strip().startswith(("http://", "https://")):
            return "base_url"
        if not self.model.text().strip():
            return "model"
        return "concurrency"

    def _focus_field(self, key: str) -> None:
        """保存失败后切到出错字段所在的分节，滚到该字段并把焦点放上去。"""
        section = next(k for k, keys in SECTION_FIELDS.items() if key in keys)
        self.show_section(section)
        scroll = self.sections.currentWidget()
        if isinstance(scroll, QScrollArea):
            scroll.ensureWidgetVisible(self.field_rows[key])
        getattr(self, key).setFocus(Qt.FocusReason.OtherFocusReason)

    def save(self) -> bool:
        """只写回表单里实际改动的项：其余项沿用当前设置（如扫描页记住的扫描参数），不被进入设置页时的旧值覆盖。"""
        changed = self.changed_keys()
        s = copy.deepcopy(self.settings)
        if changed & {"ocr_on", "base_url", "api_key", "model", "concurrency"}:
            if self.ocr_on.isChecked():
                try:
                    s.set_ocr_config(self.form_ocr_config())
                except ValueError as e:
                    QMessageBox.warning(self, "转写配置有误", str(e))
                    self._focus_field(self._bad_ocr_field())
                    return False
            else:
                # 关闭转写只记录“未启用”，保留 base_url、api_key、模型，重新启用时不用再填
                keep = {**s.ocr_form_values(), "base_url": self.base_url.text().strip(),
                        "api_key": self.api_key.text().strip(), "model": self.model.text().strip(),
                        "concurrency": self.concurrency.value()}
                s.set_ocr_config(None, keep=keep)
        if "dpi" in changed:
            s.scan.dpi = int(self.dpi.value())
        if "color" in changed:
            s.scan.color = self.color.value()
        if "duplex" in changed:
            s.scan.duplex = self.duplex.value() == "1"
        if "double_feed" in changed:
            s.scan.double_feed = self.double_feed.isChecked()
        if "dropout" in changed:
            s.scan.dropout = self.dropout.value()
        if "crop" in changed:
            s.scan.crop = self.crop.value()
        bad_exam = unknown_fields(self.exam_pattern.text(), EXAM_FIELDS)
        bad = bad_exam + unknown_fields(self.student_pattern.text(), STUDENT_FIELDS)
        if bad:
            QMessageBox.warning(self, "命名规则有误", "无法识别的占位符：" + "、".join(bad))
            self._focus_field("exam_pattern" if bad_exam else "student_pattern")
            return False
        if not self.output_root.text().strip():
            QMessageBox.warning(self, "输出根目录有误", "输出根目录不能为空，已恢复为当前设置。")
            self.output_root.setText(self.settings.output_root)
            self._focus_field("output_root")
            return False
        if "exam_pattern" in changed:
            s.exam_dir_pattern = self.exam_pattern.text().strip() or DEFAULT_EXAM_PATTERN
        if "student_pattern" in changed:
            s.student_dir_pattern = self.student_pattern.text().strip()
        if "workers" in changed:
            s.workers = self.workers.value()
        if "output_root" in changed:
            s.output_root = self.output_root.text().strip()
        if "operator" in changed:
            s.operator = self.operator.text().strip()
        try:
            save_settings(s)
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return False
        self.settings.__dict__.update(s.__dict__)
        self.load()  # 表单与实际保存的值一致（如命名规则留空时的默认值）
        self.saved.emit()
        self._show_status(f"已保存并生效（{datetime.now():%H:%M:%S}）")
        return True

    def _clear_test_result(self) -> None:
        if self.test_btn.isEnabled():
            self.test_result.setText("")
            self.test_result.setToolTip("")

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择输出根目录", self.output_root.text())
        if d:
            self.output_root.setText(d)

    # ---------- 测试连接（后台线程，不卡界面） ----------
    def test_connection(self) -> None:
        try:
            cfg = self.form_ocr_config()
        except ValueError as e:
            self._ping_done(False, str(e))
            return
        self.test_btn.setEnabled(False)
        self.test_btn.setToolTip("正在测试连接，请稍候…")
        theme.set_style(self.test_result, "color:$INK2;")
        self.test_result.setText("连接中…")

        def run() -> None:
            try:
                model = OcrClient(cfg).ping()
                self._ping.done.emit(True, f"连接正常，模型 {model}")
            except Exception as e:  # noqa: BLE001 — 任何失败都显示给用户
                self._ping.done.emit(False, f"连接失败：{type(e).__name__}: {e}")

        threading.Thread(target=run, daemon=True).start()

    def _ping_done(self, ok: bool, msg: str) -> None:
        self.test_btn.setEnabled(True)
        self.test_btn.setToolTip(TEST_TIP)
        self.test_result.setText(msg)
        self.test_result.setToolTip(msg)
        theme.set_style(self.test_result, "color:$SUCCESS;" if ok else "color:$ERR;")
