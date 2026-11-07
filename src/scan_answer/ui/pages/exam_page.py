"""考试页：考试列表、所选考试的批次列表、新建考试表单。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ...app.library import Exam, ExamInfo, create_exam, exam_totals, list_batches, list_exams, list_templates
from ...app.settings import AppSettings
from .. import theme
from ..widgets.common import section_header


def open_in_file_manager(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # noqa: S606 — 打开资源管理器
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class ExamPage(QWidget):
    startScan = Signal(object)  # Exam：在该考试下扫描（新批次）
    continueBatch = Signal(object, str)  # Exam, 批次目录
    showResults = Signal(object, str)  # Exam, 批次目录

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.exams: list[Exam] = []
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        main = QWidget()
        main.setObjectName("work")
        ml = QVBoxLayout(main)
        ml.setContentsMargins(28, 24, 28, 24)
        ml.setSpacing(8)
        h = QLabel("考试")
        h.setObjectName("h1")
        ml.addWidget(h)
        lead = QLabel("每场考试对应一个模板；批次在扫描页创建，结束批次后可在结果页查看。")
        lead.setStyleSheet(f"color:{theme.INK2};")
        ml.addWidget(lead)
        self.exam_table = self._table(["考试", "科目", "考点", "模板", "已扫", "成功", "物理异常", "数据警告"])
        self.exam_table.itemSelectionChanged.connect(self._exam_selected)
        self.exam_table.itemDoubleClicked.connect(lambda _i: self._scan_selected())
        ml.addWidget(self.exam_table, 3)
        acts = QHBoxLayout()
        self.scan_btn = QPushButton("进入扫描（新批次）")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.clicked.connect(self._scan_selected)
        acts.addWidget(self.scan_btn)
        self.open_dir_btn = QPushButton("打开考试目录")
        self.open_dir_btn.clicked.connect(self._open_exam_dir)
        acts.addWidget(self.open_dir_btn)
        acts.addStretch(1)
        ml.addLayout(acts)
        h2 = QLabel("批次")
        h2.setObjectName("h2")
        h2.setContentsMargins(0, 16, 0, 0)
        ml.addWidget(h2)
        self.batch_table = self._table(["批次", "开始", "张数", "成功", "物理异常", "作废", "数据警告", "转写", "状态"])
        ml.addWidget(self.batch_table, 2)
        bacts = QHBoxLayout()
        self.cont_btn = QPushButton("继续扫描")
        self.cont_btn.clicked.connect(lambda: self._batch_action(self.continueBatch))
        self.res_btn = QPushButton("查看结果")
        self.res_btn.clicked.connect(lambda: self._batch_action(self.showResults))
        self.bdir_btn = QPushButton("打开批次目录")
        self.bdir_btn.clicked.connect(self._open_batch_dir)
        for b in (self.cont_btn, self.res_btn, self.bdir_btn):
            bacts.addWidget(b)
        bacts.addStretch(1)
        ml.addLayout(bacts)
        root.addWidget(main, 1)

        side = QWidget()
        side.setObjectName("panel")
        side.setFixedWidth(360)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(section_header("新建考试"))
        form = QFormLayout()
        form.setContentsMargins(16, 12, 16, 12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        self.f_name = QLineEdit()
        self.f_subject = QLineEdit()
        self.f_code = QLineEdit()
        self.f_site = QLineEdit()
        self.f_room = QLineEdit()
        self.f_operator = QLineEdit(self.settings.operator)
        self.f_template = QComboBox()
        for label, w in (("考试名称", self.f_name), ("科目", self.f_subject), ("科目代码", self.f_code),
                         ("考点", self.f_site), ("考场", self.f_room), ("操作员", self.f_operator),
                         ("模板", self.f_template)):
            form.addRow(label, w)
        sl.addLayout(form)
        self.out_hint = QLabel("")
        self.out_hint.setObjectName("muted")
        self.out_hint.setWordWrap(True)
        self.out_hint.setContentsMargins(16, 0, 16, 8)
        sl.addWidget(self.out_hint)
        cb = QHBoxLayout()
        cb.setContentsMargins(16, 4, 16, 16)
        self.create_btn = QPushButton("创建并进入扫描")
        self.create_btn.setObjectName("primary")
        self.create_btn.clicked.connect(self._create)
        cb.addWidget(self.create_btn)
        cb.addStretch(1)
        sl.addLayout(cb)
        sl.addStretch(1)
        root.addWidget(side)

    @staticmethod
    def _table(headers: list[str]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        t.setShowGrid(False)
        return t

    # ---------- 数据 ----------
    def refresh(self) -> None:
        self.f_template.clear()
        for t in list_templates(self.settings.template_dirs):
            self.f_template.addItem(f"{t.name}（v{t.version}，{'双面' if t.faces == 2 else '单面'}，{t.questions} 题）",
                                    str(t.directory))
        self.out_hint.setText(f"输出到：{self.settings.output_root}")
        self.exams = list_exams(self.settings.output_root)
        self.exam_table.setRowCount(len(self.exams))
        for i, e in enumerate(self.exams):
            tot = exam_totals(e.directory)
            vals = [e.info.name, e.info.subject, e.info.site, e.template_dir.name, str(tot["total"]), str(tot["ok"]),
                    str(tot["physical"]), str(tot["warnings"])]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j == 6 and tot["physical"]:
                    it.setForeground(theme.qcolor(theme.ERR))
                self.exam_table.setItem(i, j, it)
        if self.exams:
            self.exam_table.selectRow(0)
        self._exam_selected()

    def selected(self) -> Exam | None:
        i = self.exam_table.currentRow()
        return self.exams[i] if 0 <= i < len(self.exams) else None

    def _exam_selected(self) -> None:
        e = self.selected()
        self.batches = list_batches(e.directory) if e else []
        self.batch_table.setRowCount(len(self.batches))
        for i, b in enumerate(self.batches):
            c = b.counts
            p = b.ocr_progress
            ocr = f"{p['done']} / {p['total']}" + (f"（失败 {p['failed']}）" if p.get("failed") else "") if p else (
                "等待中" if b.ocr else "未开启")
            vals = [b.batch_id, b.created[5:16].replace("T", " "), str(c.get("total", 0)), str(c.get("ok", 0)),
                    str(c.get("physical", 0)), str(c.get("voided", 0)), str(c.get("warnings", 0)), ocr, b.state]
            for j, v in enumerate(vals):
                self.batch_table.setItem(i, j, QTableWidgetItem(v))
        if self.batches:
            self.batch_table.selectRow(0)
        for w in (self.scan_btn, self.open_dir_btn):
            w.setEnabled(e is not None)
        for w in (self.cont_btn, self.res_btn, self.bdir_btn):
            w.setEnabled(bool(self.batches))

    def _scan_selected(self) -> None:
        e = self.selected()
        if e:
            self.startScan.emit(e)

    def _open_exam_dir(self) -> None:
        e = self.selected()
        if e:
            open_in_file_manager(e.directory)

    def _batch_action(self, sig) -> None:
        e, i = self.selected(), self.batch_table.currentRow()
        if e and 0 <= i < len(self.batches):
            sig.emit(e, str(self.batches[i].directory))

    def _open_batch_dir(self) -> None:
        i = self.batch_table.currentRow()
        if 0 <= i < len(self.batches):
            open_in_file_manager(self.batches[i].directory)

    def _create(self) -> None:
        name = self.f_name.text().strip()
        tdir = self.f_template.currentData()
        if not name:
            QMessageBox.information(self, "提示", "请填写考试名称。")
            return
        if not tdir:
            QMessageBox.information(self, "提示", '没有可用的模板，请先在"模板"页添加模板目录。')
            return
        info = ExamInfo(name=name, subject=self.f_subject.text().strip(), subject_code=self.f_code.text().strip(),
                        site=self.f_site.text().strip(), room=self.f_room.text().strip(),
                        operator=self.f_operator.text().strip())
        exam = create_exam(self.settings.output_root, info, tdir)
        self.refresh()
        self.startScan.emit(exam)
