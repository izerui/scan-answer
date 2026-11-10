"""设置页：扫描默认参数、识别进程数、大模型转写（含测试连接）、输出目录。"""
from __future__ import annotations

import copy
import threading
from datetime import datetime

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QLayout, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ...app.settings import AppSettings, save_settings
from ...ocr.client import OcrClient
from ...ocr.config import OcrConfig
from ...output.naming import DEFAULT_EXAM_PATTERN, EXAM_FIELDS, STUDENT_FIELDS, sample_names, unknown_fields
from .. import theme
from ..widgets.common import RegionLegend, Segmented, Toggle, form_layout
from ..widgets.guide import HelpButton, with_help


DROPOUT_OPTIONS = [("none", "无"), ("red", "滤红"), ("green", "滤绿"), ("blue", "滤蓝")]
CROP_OPTIONS = [("auto", "自动"), ("template", "按模板"), ("driver", "驱动")]
LABEL_WIDTH = 104  # 各分区共用的标签列宽，保证字段左边缘对齐
TEST_TIP = "用上面填的地址、密钥、模型发一次请求，检查能不能连上（不用先保存）"
DIRTY_TEXT = "有未保存的修改，点“保存设置”后才生效"


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

    @staticmethod
    def _heading(lay: QVBoxLayout, title: str, concept: str | None = None) -> None:
        h = QLabel(title)
        if concept is None:
            theme.set_style(h, "font-size:16px; font-weight:600; padding:20px 0 8px 0; border-bottom:1px solid $LINE;")
            lay.addWidget(h)
            return
        # 带“?”的标题：标题和按钮放一行，下边线画在整行上
        row = QWidget()
        row.setObjectName("settingsHeading")
        theme.set_style(row, "#settingsHeading { border-bottom:1px solid $LINE; }")
        theme.set_style(h, "font-size:16px; font-weight:600;")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 20, 0, 8)
        hl.setSpacing(6)
        hl.addWidget(h)
        hl.addWidget(HelpButton(concept))
        hl.addStretch(1)
        lay.addWidget(row)

    def _section(self, lay: QVBoxLayout, title: str, concept: str | None = None) -> QFormLayout:
        self._heading(lay, title, concept)
        f = form_layout(margins=(0, 14, 0, 4))
        f.setVerticalSpacing(12)
        f.setHorizontalSpacing(16)
        lay.addLayout(f)
        self._forms.append(f)
        return f

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self._forms: list[QFormLayout] = []
        body = QWidget()
        body.setObjectName("work")
        frame = QHBoxLayout(body)
        frame.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        content.setMaximumWidth(980)
        frame.addWidget(content, 1)
        frame.addStretch(0)
        lay = QVBoxLayout(content)
        lay.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        lay.setContentsMargins(36, 8, 36, 28)
        f = self._section(lay, "扫描默认参数")
        self.dpi = Segmented([(str(d), str(d)) for d in (200, 300, 400, 600)], "300")
        _tip(self.dpi, "扫描仪的扫描精度，越高越清楚，但越慢、文件越大。只在用扫描仪时生效",
             {"200": "最快，填涂识别够用", "300": "推荐，识别和切图都够清楚",
              "400": "手写稍清楚一些", "600": "最清楚但很慢，只在对手写清晰度要求高时用"})
        self.color = Segmented([("gray", "灰度"), ("color", "彩色")], "gray")
        _tip(self.color, "扫描的色彩模式。只在用扫描仪时生效",
             {"gray": "推荐，文件小、识别快", "color": "保留颜色；彩色印刷的卡要软件滤色时用"})
        self.duplex = Segmented([("1", "双面"), ("0", "单面")], "1")
        _tip(self.duplex, "扫描仪一次扫纸的一面还是两面。只在用扫描仪时生效",
             {"1": "正反面都扫（双面答题卡选这个）", "0": "只扫正面"})
        self.double_feed = Toggle(True)
        _tip(self.double_feed, "扫描仪一次带进两张纸时立即停机，这张列入物理异常，避免漏扫。建议开着")
        f.addRow("分辨率 DPI", self._with_note(self.dpi, "300 足够识别；600 仅用于对手写清晰度要求高的场景"))
        f.addRow("色彩", self._row(self.color))
        f.addRow("进纸", self._row(self.duplex))
        f.addRow("双张检测", self._with_note(self.double_feed, "检测到双张即停机，列入物理异常"))
        self.dropout = Segmented(DROPOUT_OPTIONS, "none")
        _tip(self.dropout, "去掉卡面上某种颜色的印刷（底纹、选项框），让涂黑更突出。黑色印刷的卡选“无”",
             {"none": "不滤色（黑色印刷的卡）", "red": "去掉红色印刷", "green": "去掉绿色印刷", "blue": "去掉蓝色印刷"})
        f.addRow("颜色滤除", self._with_note(with_help(self.dropout, "dropout"),
                                         "去掉卡面上该颜色的印刷（底纹、选项框）；彩色扫描时由软件滤色"))
        self.crop = Segmented(CROP_OPTIONS, "auto")
        _tip(self.crop, "扫描时怎么裁掉纸张以外的边。只在用扫描仪时生效",
             {"auto": "扫描仪自动检测纸边（推荐）", "template": "按模板的纸张尺寸裁",
              "driver": "用扫描仪驱动里的默认设置"})
        f.addRow("裁边", self._with_note(with_help(self.crop, "crop"),
                                       "自动：扫描仪检测纸边；按模板：按模板纸张尺寸；驱动：用驱动默认设置"))
        self.workers = QSpinBox()
        self.workers.setRange(0, 16)
        _tip(self.workers, "同时识别几张卡。0＝在界面进程里一张张识别；一般取 CPU 核数减 1，最多 4，太大反而占满内存")
        f.addRow("识别进程数", self._with_note(with_help(self.workers, "workers"),
                                          "0 表示在界面进程内顺序识别；一般取 CPU 核数减 1，最多 4"))

        f = self._section(lay, "大模型转写", "ocr")
        self.ocr_on = Toggle(False)
        _tip(self.ocr_on, "打开后，扫描时在后台用大模型把手写内容转成文字。关闭也不会丢掉下面填的配置")
        f.addRow("启用", self._with_note(self.ocr_on, "可选。未启用时主观题只切图；启用后扫描时在后台转写，不影响扫描速度"))
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        show = QPushButton("显示")
        show.setCheckable(True)
        show.setToolTip("按下显示密钥明文，再点一次隐藏")
        show.toggled.connect(lambda on: self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.model = QLineEdit()
        self.model.setPlaceholderText("qwen-vl-max")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        _tip(self.base_url, "大模型服务的接口地址（OpenAI 兼容格式），向服务商获取")
        _tip(self.api_key, "大模型服务的密钥，向服务商获取；只保存在本机设置里")
        _tip(self.model, "要用的模型名，需支持看图（视觉模型），如 qwen-vl-max")
        _tip(self.concurrency, "同时发给大模型的请求数。越大越快，但可能超出服务商的限流")
        f.addRow("base_url", self.base_url)
        f.addRow("api_key", self._fill_row(self.api_key, show))
        f.addRow("模型", self.model)
        f.addRow("并发数", self._row(self.concurrency))
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setToolTip(TEST_TIP)
        self.test_btn.clicked.connect(self.test_connection)
        self.test_result = QLabel("")
        self.test_result.setWordWrap(True)
        for w in (self.base_url, self.api_key, self.model):
            w.textChanged.connect(self._clear_test_result)  # 配置改了，旧的测试结果不再可信
        f.addRow("连接", self._row(self.test_btn, self.test_result))

        f = self._section(lay, "输出")
        self.output_root = QLineEdit()
        _tip(self.output_root, "所有考试和结果存放的文件夹。改了之后考试页列出的是新目录里的考试，旧目录里的不会搬过来")
        browse = QPushButton("浏览")
        browse.setToolTip("选择输出根目录")
        browse.clicked.connect(self._browse)
        f.addRow("输出根目录", self._fill_row(self.output_root, browse))
        self.operator = QLineEdit()
        _tip(self.operator, "新建考试时“操作员”一栏默认填这个名字，写入结果")
        f.addRow("默认操作员", self.operator)
        self.exam_pattern = QLineEdit()
        self.exam_pattern.setMinimumWidth(320)
        self.exam_pattern.setPlaceholderText(DEFAULT_EXAM_PATTERN)
        _tip(self.exam_pattern, "新建考试时考试文件夹的名字。{name} 等占位符会换成考试信息；留空用默认规则")
        f.addRow("考试目录命名", self._with_note(with_help(self.exam_pattern, "dir_patterns"),
                                           "可用：{name} {subject} {code} {site} {room} {operator} {date} {time}"))
        self.student_pattern = QLineEdit()
        self.student_pattern.setMinimumWidth(320)
        _tip(self.student_pattern, "每个学生结果文件夹名里序号之后的部分，如 {exam_no}＝考号；留空只用序号")
        f.addRow("学生目录命名", self._with_note(with_help(self.student_pattern, "dir_patterns"),
                                           "序号之后的部分，可用：{exam_no} {barcode}；留空则只用序号"))
        self.naming_sample = QLabel("")
        self.naming_sample.setObjectName("muted")
        self.naming_sample.setToolTip("按上面两条规则，用示例考试信息生成的目录名")
        f.addRow("示例", self.naming_sample)
        self.exam_pattern.textChanged.connect(self._update_naming_sample)
        self.student_pattern.textChanged.connect(self._update_naming_sample)

        # 区域色码（只读参考）
        self._heading(lay, "区域色码")
        legend = RegionLegend(extra=[
            ("ERR", "物理异常（定位失败、缺面等）"),
            ("WARN", "数据警告（多涂、空白等）"),
        ])
        legend.layout().setContentsMargins(0, 14, 0, 4)
        legend.setMaximumWidth(520)
        lay.addWidget(legend)

        lay.addStretch(1)
        for form in self._forms:
            for row in range(form.rowCount()):
                item = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
                if item is not None and item.widget() is not None:
                    item.widget().setMinimumWidth(LABEL_WIDTH)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        # 保存按钮固定在底部，不用滚到页尾才能找到
        bar = QWidget()
        bar.setObjectName("settingsBar")
        theme.set_style(bar, "#settingsBar { background: $HEADER; border-top: 1px solid $LINE; }")
        btns = QHBoxLayout(bar)
        btns.setContentsMargins(36, 10, 36, 10)
        btns.setSpacing(12)
        self.save_btn = QPushButton("保存设置")
        self.save_btn.setObjectName("primary")
        self.save_btn.setToolTip("保存本页所有修改并立即生效；没保存就离开设置页时会询问")
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.save_btn)
        self.save_status = QLabel("")
        self.save_status.setObjectName("muted")
        btns.addWidget(self.save_status)
        btns.addStretch(1)
        outer.addWidget(bar)
        # 改了表单就在保存栏提示“未保存”
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

    @staticmethod
    def _row(*ws: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        for x in ws:
            h.addWidget(x)
        h.addStretch(1)
        return w

    @staticmethod
    def _fill_row(edit: QWidget, *buttons: QWidget) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        for b in buttons:
            h.addWidget(b)
        return w

    @staticmethod
    def _with_note(widget: QWidget, note: str) -> QWidget:
        lab = QLabel(note)
        lab.setObjectName("muted")
        lab.setWordWrap(True)
        lab.setMinimumWidth(240)
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(16)
        h.addWidget(widget)
        h.addWidget(lab, 1)
        return w

    # ---------- 读写 ----------
    def load(self) -> None:
        """从当前设置重新填表（每次进入设置页都调用：扫描页会记住扫描参数并写设置）。"""
        self._loading = True
        self.save_status.setText("")
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

    def _refresh_dirty(self) -> None:
        if getattr(self, "_loading", True) or not hasattr(self, "_loaded"):
            return
        if self.is_dirty():
            self.save_status.setText(DIRTY_TEXT)
        elif self.save_status.text() == DIRTY_TEXT:
            self.save_status.setText("")

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
        bad = (unknown_fields(self.exam_pattern.text(), EXAM_FIELDS)
               + unknown_fields(self.student_pattern.text(), STUDENT_FIELDS))
        if bad:
            QMessageBox.warning(self, "命名规则有误", "无法识别的占位符：" + "、".join(bad))
            return False
        if not self.output_root.text().strip():
            QMessageBox.warning(self, "输出根目录有误", "输出根目录不能为空，已恢复为当前设置。")
            self.output_root.setText(self.settings.output_root)
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
        self.save_status.setText(f"已保存并生效（{datetime.now():%H:%M:%S}）")
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
