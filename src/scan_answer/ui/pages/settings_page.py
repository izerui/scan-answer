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


DROPOUT_OPTIONS = [("none", "无"), ("red", "滤红"), ("green", "滤绿"), ("blue", "滤蓝")]
CROP_OPTIONS = [("auto", "自动"), ("template", "按模板"), ("driver", "驱动")]
LABEL_WIDTH = 104  # 各分区共用的标签列宽，保证字段左边缘对齐


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
    def _heading(lay: QVBoxLayout, title: str) -> None:
        h = QLabel(title)
        theme.set_style(h, "font-size:16px; font-weight:600; padding:20px 0 8px 0; border-bottom:1px solid $LINE;")
        lay.addWidget(h)

    def _section(self, lay: QVBoxLayout, title: str) -> QFormLayout:
        self._heading(lay, title)
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
        self.color = Segmented([("gray", "灰度"), ("color", "彩色")], "gray")
        self.duplex = Segmented([("1", "双面"), ("0", "单面")], "1")
        self.double_feed = Toggle(True)
        f.addRow("分辨率 DPI", self._with_note(self.dpi, "300 足够识别；600 仅用于对手写清晰度要求高的场景"))
        f.addRow("色彩", self._row(self.color))
        f.addRow("进纸", self._row(self.duplex))
        f.addRow("双张检测", self._with_note(self.double_feed, "检测到双张即停机，列入物理异常"))
        self.dropout = Segmented(DROPOUT_OPTIONS, "none")
        f.addRow("颜色滤除", self._with_note(self.dropout, "去掉卡面上该颜色的印刷（底纹、选项框）；彩色扫描时由软件滤色"))
        self.crop = Segmented(CROP_OPTIONS, "auto")
        f.addRow("裁边", self._with_note(self.crop, "自动：扫描仪检测纸边；按模板：按模板纸张尺寸；驱动：用驱动默认设置"))
        self.workers = QSpinBox()
        self.workers.setRange(0, 16)
        f.addRow("识别进程数", self._with_note(self.workers, "0 表示在界面进程内顺序识别；一般取 CPU 核数减 1，最多 4"))

        f = self._section(lay, "大模型转写")
        self.ocr_on = Toggle(False)
        f.addRow("启用", self._with_note(self.ocr_on, "可选。未启用时主观题只切图；启用后扫描时在后台转写，不影响扫描速度"))
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        show = QPushButton("显示")
        show.setCheckable(True)
        show.toggled.connect(lambda on: self.api_key.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        self.model = QLineEdit()
        self.model.setPlaceholderText("qwen-vl-max")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        f.addRow("base_url", self.base_url)
        f.addRow("api_key", self._fill_row(self.api_key, show))
        f.addRow("模型", self.model)
        f.addRow("并发数", self._row(self.concurrency))
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self.test_connection)
        self.test_result = QLabel("")
        f.addRow("连接", self._row(self.test_btn, self.test_result))

        f = self._section(lay, "输出")
        self.output_root = QLineEdit()
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        f.addRow("输出根目录", self._fill_row(self.output_root, browse))
        self.operator = QLineEdit()
        f.addRow("默认操作员", self.operator)
        self.exam_pattern = QLineEdit()
        self.exam_pattern.setMinimumWidth(320)
        self.exam_pattern.setPlaceholderText(DEFAULT_EXAM_PATTERN)
        f.addRow("考试目录命名", self._with_note(self.exam_pattern, "可用：{name} {subject} {code} {site} {room} "
                                                                   "{operator} {date} {time}"))
        self.student_pattern = QLineEdit()
        self.student_pattern.setMinimumWidth(320)
        f.addRow("学生目录命名", self._with_note(self.student_pattern, "序号之后的部分，可用：{exam_no} {barcode}；"
                                                                      "留空则只用序号"))
        self.naming_sample = QLabel("")
        self.naming_sample.setObjectName("muted")
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
        self.save_btn.clicked.connect(self.save)
        btns.addWidget(self.save_btn)
        self.save_status = QLabel("")
        self.save_status.setObjectName("muted")
        btns.addWidget(self.save_status)
        btns.addStretch(1)
        outer.addWidget(bar)

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
        self.save_status.setText("")
        s = self.settings
        self.dpi.set_value(str(s.scan.dpi))
        self.color.set_value(s.scan.color)
        self.duplex.set_value("1" if s.scan.duplex else "0")
        self.double_feed.setChecked(s.scan.double_feed)
        self.workers.setValue(s.workers)
        o = s.ocr or {}
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
        s = copy.deepcopy(self.settings)
        try:
            s.set_ocr_config(self.form_ocr_config() if self.ocr_on.isChecked() else None)
        except ValueError as e:
            QMessageBox.warning(self, "转写配置有误", str(e))
            return False
        s.scan.dpi = int(self.dpi.value())
        s.scan.color = self.color.value()
        s.scan.duplex = self.duplex.value() == "1"
        s.scan.double_feed = self.double_feed.isChecked()
        s.scan.dropout = self.dropout.value()
        s.scan.crop = self.crop.value()
        bad = (unknown_fields(self.exam_pattern.text(), EXAM_FIELDS)
               + unknown_fields(self.student_pattern.text(), STUDENT_FIELDS))
        if bad:
            QMessageBox.warning(self, "命名规则有误", "无法识别的占位符：" + "、".join(bad))
            return False
        s.exam_dir_pattern = self.exam_pattern.text().strip() or DEFAULT_EXAM_PATTERN
        s.student_dir_pattern = self.student_pattern.text().strip()
        s.workers = self.workers.value()
        s.output_root = self.output_root.text().strip() or s.output_root
        s.operator = self.operator.text().strip()
        try:
            save_settings(s)
        except OSError as error:
            QMessageBox.warning(self, "保存失败", str(error))
            return False
        self.settings.__dict__.update(s.__dict__)
        self.saved.emit()
        self.save_status.setText(f"已保存（{datetime.now():%H:%M:%S}）")
        return True

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
        self.test_result.setText(msg)
        theme.set_style(self.test_result, "color:$SUCCESS;" if ok else "color:$ERR;")
