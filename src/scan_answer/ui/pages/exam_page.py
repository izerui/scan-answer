"""考试页：考试列表、所选考试的批次列表、新建考试表单。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QStyledItemDelegate, QStyleOptionViewItem,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...app.library import Exam, ExamInfo, create_exam, exam_totals, list_batches, list_exams, list_templates
from ...app.settings import AppSettings
from .. import theme
from ..widgets.common import form_layout, section_header, set_empty_text, setup_table


class CompositionDelegate(QStyledItemDelegate):
    """考试表"构成"列：水平比例条——成功/警告/异常/作废。"""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        data = index.data(Qt.ItemDataRole.UserRole)
        if not data or not data.get("total"):
            painter.save()
            painter.setPen(theme.qcolor(theme.INK3))
            painter.drawText(option.rect, Qt.AlignmentFlag.AlignCenter, "—")
            painter.restore()
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(option.rect).adjusted(6, 6, -6, -6)
        total = data["total"]
        # 成功（无警告）= OK_FILL，有数据警告 = WARN_FILL，物理异常 = ERR，作废 = 灰色
        ok_clean = max(0, data.get("ok", 0) - data.get("warnings", 0))
        segments = [
            (ok_clean, theme.OK_FILL),
            (data.get("warnings", 0), theme.WARN_FILL),
            (data.get("physical", 0), theme.ERR),
            (data.get("voided", 0), theme.DISABLED),
        ]
        x = r.left()
        for count, color in segments:
            if count <= 0:
                continue
            seg_w = r.width() * count / total
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(color))
            painter.drawRoundedRect(QRectF(x, r.top(), seg_w, r.height()), 2, 2)
            x += seg_w
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(120, 24)


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
        # 说明 + 考试操作放在表格上方同一行，按钮紧贴它所作用的数据
        head = QHBoxLayout()
        head.setSpacing(8)
        lead = QLabel("每场考试对应一个模板；批次在扫描页创建，结束批次后可在结果页查看。")
        theme.set_style(lead, "color:$INK2;")
        head.addWidget(lead, 1)
        self.scan_btn = QPushButton("进入扫描（新批次）")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setToolTip("在选中的考试下新建批次并进入扫描页（也可双击考试）")
        self.scan_btn.clicked.connect(self._scan_selected)
        self.open_dir_btn = QPushButton("打开考试目录")
        self.open_dir_btn.clicked.connect(self._open_exam_dir)
        head.addWidget(self.open_dir_btn)
        head.addWidget(self.scan_btn)
        ml.addLayout(head)
        self.exam_table = self._table(["考试", "科目", "考点", "模板", "构成", "已扫", "成功", "物理异常", "数据警告"],
                                      stretch=(0, 1, 2, 3), widths={4: 120, 5: 64, 6: 64, 7: 84, 8: 84})
        self._comp_delegate = CompositionDelegate(self.exam_table)
        self.exam_table.setItemDelegateForColumn(4, self._comp_delegate)
        set_empty_text(self.exam_table, "还没有考试。在右侧填写考试信息、选择模板，点“创建并进入扫描”。")
        self.exam_table.itemSelectionChanged.connect(self._exam_selected)
        self.exam_table.itemDoubleClicked.connect(lambda _i: self._scan_selected())
        ml.addSpacing(4)
        ml.addWidget(self.exam_table, 1)
        bhead = QHBoxLayout()
        bhead.setContentsMargins(0, 20, 0, 4)
        bhead.setSpacing(8)
        h2 = QLabel("批次")
        h2.setObjectName("h2")
        bhead.addWidget(h2)
        bhead.addStretch(1)
        self.cont_btn = QPushButton("继续扫描")
        self.cont_btn.clicked.connect(lambda: self._batch_action(self.continueBatch))
        self.res_btn = QPushButton("查看结果")
        self.res_btn.setToolTip("在结果页打开选中的批次（也可双击批次）")
        self.res_btn.clicked.connect(lambda: self._batch_action(self.showResults))
        self.bdir_btn = QPushButton("打开批次目录")
        self.bdir_btn.clicked.connect(self._open_batch_dir)
        for b in (self.bdir_btn, self.cont_btn, self.res_btn):
            bhead.addWidget(b)
        ml.addLayout(bhead)
        self.batch_table = self._table(["批次", "开始", "张数", "成功", "物理异常", "作废", "数据警告", "转写", "状态"],
                                       stretch=(0,), widths={1: 112, 2: 64, 3: 64, 4: 84, 5: 64, 6: 84, 7: 110, 8: 80})
        self.batch_table.itemDoubleClicked.connect(lambda _i: self._batch_action(self.showResults))
        ml.addWidget(self.batch_table, 1)
        root.addWidget(main, 1)

        side = QWidget()
        side.setObjectName("panel")
        side.setFixedWidth(340)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        sl.addWidget(section_header("新建考试"))
        form = form_layout(margins=(16, 16, 16, 12))
        self.f_name = QLineEdit()
        self.f_subject = QLineEdit()
        self.f_code = QLineEdit()
        self.f_site = QLineEdit()
        self.f_room = QLineEdit()
        self.f_operator = QLineEdit(self.settings.operator)
        self.f_template = QComboBox()
        self.f_template.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.f_template.setMinimumContentsLength(8)
        for label, w in (("考试名称", self.f_name), ("科目", self.f_subject), ("科目代码", self.f_code),
                         ("考点", self.f_site), ("考场", self.f_room), ("操作员", self.f_operator),
                         ("模板", self.f_template)):
            form.addRow(label, w)
        sl.addLayout(form)
        self.out_hint = QLabel("")
        self.out_hint.setObjectName("muted")
        self.out_hint.setWordWrap(True)
        self.out_hint.setContentsMargins(16, 0, 16, 12)
        sl.addWidget(self.out_hint)
        cb = QHBoxLayout()
        cb.setContentsMargins(16, 0, 16, 16)
        self.create_btn = QPushButton("创建并进入扫描")
        self.create_btn.setObjectName("primary")
        self.create_btn.clicked.connect(self._create)
        cb.addWidget(self.create_btn, 1)
        sl.addLayout(cb)
        sl.addStretch(1)
        root.addWidget(side)

    @staticmethod
    def _table(headers: list[str], stretch: tuple[int, ...], widths: dict[int, int]) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        setup_table(t)
        header = t.horizontalHeader()
        for column in stretch:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column, width in widths.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(column, width)
        return t

    # ---------- 数据 ----------
    def refresh(self, selected_template: str | Path | None = None) -> None:
        selected_template = selected_template or self.f_template.currentData()
        self.f_template.clear()
        for t in list_templates(self.settings.template_dirs):
            self.f_template.addItem(f"{t.name}（v{t.version}，{'双面' if t.faces == 2 else '单面'}，{t.questions} 题）",
                                    str(t.directory))
            if selected_template and t.directory.resolve() == Path(selected_template).resolve():
                self.f_template.setCurrentIndex(self.f_template.count() - 1)
        self.out_hint.setText(f"输出到：{self.settings.output_root}")
        self.exams = list_exams(self.settings.output_root)
        self.exam_table.setRowCount(len(self.exams))
        for i, e in enumerate(self.exams):
            tot = exam_totals(e.directory)
            vals = [e.info.name, e.info.subject, e.info.site, e.template_label,
                    "",  # 构成列由 delegate 绘制
                    str(tot["total"]), str(tot["ok"]), str(tot["physical"]), str(tot["warnings"])]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j < 4:
                    it.setToolTip(v)
                if j == 4:
                    # 构成条数据存入 UserRole，tooltip 显示各项数量
                    it.setData(Qt.ItemDataRole.UserRole, tot)
                    it.setToolTip(f"成功 {tot['ok']}　警告 {tot['warnings']}　异常 {tot['physical']}　作废 {tot.get('voided', 0)}")
                if j == 7 and tot["physical"]:
                    theme.set_item_colors(it, foreground="ERR")
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
        set_empty_text(self.batch_table, "这场考试还没有批次。点“进入扫描（新批次）”开始扫描。" if e
                       else "选中一场考试后，这里列出它的批次。")
        self.batch_table.setRowCount(len(self.batches))
        for i, b in enumerate(self.batches):
            c = b.counts
            p = b.ocr_progress
            ocr = f"{p['done']} / {p['total']}" + (f"（失败 {p['failed']}）" if p.get("failed") else "") if p else (
                "等待中" if b.ocr else "未开启")
            vals = [b.batch_id, b.created[5:16].replace("T", " "), str(c.get("total", 0)), str(c.get("ok", 0)),
                    str(c.get("physical", 0)), str(c.get("voided", 0)), str(c.get("warnings", 0)), ocr, b.state]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j in (0, 7):
                    it.setToolTip(v)
                self.batch_table.setItem(i, j, it)
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
            QMessageBox.information(self, "提示", "没有可用的模板，请先在“模板”页添加模板目录。")
            return
        info = ExamInfo(name=name, subject=self.f_subject.text().strip(), subject_code=self.f_code.text().strip(),
                        site=self.f_site.text().strip(), room=self.f_room.text().strip(),
                        operator=self.f_operator.text().strip())
        try:
            exam = create_exam(self.settings.output_root, info, tdir, self.settings.exam_dir_pattern)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "提示", f"创建考试失败：{e}")
            return
        self.refresh()
        self.startScan.emit(exam)
