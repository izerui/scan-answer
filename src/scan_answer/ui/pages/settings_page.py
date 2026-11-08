"""设置页：扫描默认参数、识别进程数、大模型转写（含测试连接）、输出目录。"""
from __future__ import annotations

import copy
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QVBoxLayout, QWidget)

from ...app.settings import AppSettings, save_settings
from ...ocr.client import OcrClient
from ...ocr.config import OcrConfig
from ...output.naming import DEFAULT_EXAM_PATTERN, EXAM_FIELDS, STUDENT_FIELDS, sample_names, unknown_fields
from .. import theme
from ..widgets.common import RegionLegend, Segmented, Toggle


DROPOUT_OPTIONS = [("none", "无"), ("red", "滤红"), ("green", "滤绿"), ("blue", "滤蓝")]
CROP_OPTIONS = [("auto", "自动"), ("template", "按模板"), ("driver", "驱动")]


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

    def _section(self, lay: QVBoxLayout, title: str) -> QFormLayout:
        h = QLabel(title)
        theme.set_style(h, "font-size:16px; font-weight:600; padding:18px 0 8px 0; border-bottom:1px solid $INK;")
        lay.addWidget(h)
        f = QFormLayout()
        f.setVerticalSpacing(12)
        f.setHorizontalSpacing(40)
        f.setContentsMargins(0, 12, 0, 0)
        f.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        lay.addLayout(f)
        return f

    def _build(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        body = QWidget()
        body.setObjectName("work")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(32, 8, 32, 24)
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
        for e in (self.base_url, self.api_key):
            e.setMinimumWidth(520 if e is self.base_url else 420)
        self.model = QLineEdit()
        self.model.setMinimumWidth(240)
        self.model.setPlaceholderText("qwen-vl-max")
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        f.addRow("base_url", self.base_url)
        f.addRow("api_key", self._row(self.api_key, show))
        f.addRow("模型", self.model)
        f.addRow("并发数", self._row(self.concurrency))
        self.test_btn = QPushButton("测试连接")
        self.test_btn.clicked.connect(self.test_connection)
        self.test_result = QLabel("")
        f.addRow("连接", self._row(self.test_btn, self.test_result))

        f = self._section(lay, "输出")
        self.output_root = QLineEdit()
        self.output_root.setMinimumWidth(520)
        browse = QPushButton("浏览")
        browse.clicked.connect(self._browse)
        f.addRow("输出根目录", self._row(self.output_root, browse))
        self.operator = QLineEdit()
        self.operator.setMinimumWidth(240)
        f.addRow("默认操作员", self.operator)
        self.exam_pattern = QLineEdit()
        self.exam_pattern.setMinimumWidth(360)
        self.exam_pattern.setPlaceholderText(DEFAULT_EXAM_PATTERN)
        f.addRow("考试目录命名", self._with_note(self.exam_pattern, "可用：{name} {subject} {code} {site} {room} "
                                                                   "{operator} {date} {time}"))
        self.student_pattern = QLineEdit()
        self.student_pattern.setMinimumWidth(240)
        f.addRow("学生目录命名", self._with_note(self.student_pattern, "序号之后的部分，可用：{exam_no} {barcode}；"
                                                                      "留空则只用序号"))
        self.naming_sample = QLabel("")
        self.naming_sample.setObjectName("muted")
        f.addRow("示例", self.naming_sample)
        self.exam_pattern.textChanged.connect(self._update_naming_sample)
        self.student_pattern.textChanged.connect(self._update_naming_sample)

        # 区域色码（只读参考）
        h = QLabel("区域色码")
        theme.set_style(h, "font-size:16px; font-weight:600; padding:18px 0 8px 0; border-bottom:1px solid $INK;")
        lay.addWidget(h)
        legend = RegionLegend(extra=[
            ("ERR", "物理异常（定位失败、缺面等）"),
            ("WARN", "数据警告（多涂、空白等）"),
        ])
        lay.addWidget(legend)

        lay.addStretch(1)
        btns = QHBoxLayout()
        save = QPushButton("保存设置")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        btns.addWidget(save)
        btns.addStretch(1)
        lay.addLayout(btns)
        outer.addWidget(body)

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
    def _with_note(widget: QWidget, note: str) -> QWidget:
        lab = QLabel(note)
        lab.setObjectName("muted")
        return SettingsPage._row(widget, lab)

    # ---------- 读写 ----------
    def load(self) -> None:
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
