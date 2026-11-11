"""考试页：考试列表、所选考试的批次列表、新建考试表单。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QStyledItemDelegate, QStyleOptionViewItem,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...app.library import (Exam, ExamInfo, create_exam, exam_totals, list_batches, list_exams, list_templates,
                            output_root_problem, template_problem)
from ...app.settings import AppSettings, default_output_root
from .. import theme
from ..background import Latest
from ..widgets.common import ElidedLabel, form_layout, section_header, set_empty_text, setup_table
from ..widgets.guide import HelpButton, with_help


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


CONTINUE_TIP = "在扫描页打开选中的批次，接着扫描"
FINALIZED_TIP = "该批次已结算，继续扫描时可选择追加到该批次或新建批次"
BUSY_TIP = "当前批次还在进行中，请等待本轮完成或在扫描页点“结束本轮”后再切换。"

NO_EXAM_TIP = "先在上面的考试列表里选中一场考试"
NO_BATCH_TIP = "这场考试还没有批次，先点“进入扫描（新批次）”"
EXAM_EMPTY = "还没有考试。在右侧填写考试名称、选择模板，点“创建并进入扫描”。"
SCAN_TIP = "在选中的考试下新建一个批次，进入扫描页开始扫描（也可双击考试）"
OPEN_DIR_TIP = "在文件管理器中打开这场考试的文件夹（模板快照、各批次结果都在里面）"
RES_TIP = "在结果页打开选中的批次，核对识别结果（也可双击批次）"
BDIR_TIP = "在文件管理器中打开选中批次的文件夹（原图、识别结果、切图）"
EXAM_COLUMN_TIPS = ["考试名称", "科目", "考点", "这场考试固定使用的模板（创建时的快照）",
                    "所有批次答卷的构成：绿＝成功，黄＝有数据警告，红＝物理异常，灰＝作废",
                    "所有批次已扫的答卷张数", "识别成功的张数（含有警告的）",
                    "没扫好或对不上模板、没有识别结果的张数，需要在扫描页处理",
                    "识别了但需要核对的张数（存疑、多涂、考号重复等）"]
BATCH_COLUMN_TIPS = ["批次编号（也是批次文件夹名）", "批次创建时间", "这一批已扫的答卷张数",
                     "识别成功的张数（含有警告的）", "没扫好或对不上模板的张数，需要在扫描页处理",
                     "已作废的张数（不参与交付，可在扫描页恢复）", "识别了但需要核对的张数",
                     "大模型转写进度：已完成 / 总数；“未开启”表示这批不转写",
                     "进行中＝还能接着扫；已结算＝已结束批次，可以交付"]


class ExamPage(QWidget):
    startScan = Signal(object)  # Exam：在该考试下扫描（新批次）
    continueBatch = Signal(object, str)  # Exam, 批次目录
    showResults = Signal(object, str)  # Exam, 批次目录

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.exams: list[Exam] = []
        self.batches = []
        # 由主窗口设置：扫描是否忙碌、扫描页当前打开的批次目录（批次列表据此标注）
        self.busy_fn: Callable[[], bool] = lambda: False
        self.open_batch_fn: Callable[[], str | None] = lambda: None
        self._operator_default = settings.operator  # 操作员框上次同步的默认值；用户改过就不再覆盖
        self._template_check = Latest(self)  # 选中考试时的模板检查：选中的考试变了，旧检查的结果作废
        self._preparing = False  # 主窗口正在后台检查模板、准备进入扫描
        self._creating = Latest(self, wait_for_modal=True)  # 新建考试（复制并校验模板快照）在后台执行
        self._build()
        self.refresh()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        root = QHBoxLayout()
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        outer.addLayout(root, 1)
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
        lead = QLabel("每场考试对应一个模板；批次在扫描页创建，扫描中或结束后都可在结果页查看。")
        theme.set_style(lead, "color:$INK2;")
        head.addWidget(lead, 1)
        self.scan_btn = QPushButton("进入扫描（新批次）")
        self.scan_btn.setObjectName("primary")
        self.scan_btn.setToolTip(SCAN_TIP)
        self.scan_btn.clicked.connect(self._scan_selected)
        self.open_dir_btn = QPushButton("打开考试目录")
        self.open_dir_btn.setToolTip(OPEN_DIR_TIP)
        self.open_dir_btn.clicked.connect(self._open_exam_dir)
        head.addWidget(self.open_dir_btn)
        head.addWidget(self.scan_btn)
        ml.addLayout(head)
        # 情境提示：还没有考试且输出根目录是默认值时，提醒先到设置页确认结果存哪
        self.root_hint = QLabel("")
        self.root_hint.setObjectName("muted")
        self.root_hint.setWordWrap(True)
        self.root_hint.setVisible(False)
        ml.addWidget(self.root_hint)
        self.exam_table = self._table(["考试", "科目", "考点", "模板", "构成", "已扫", "成功", "物理异常", "数据警告"],
                                      stretch=(0, 1, 2, 3), widths={4: 120, 5: 64, 6: 64, 7: 84, 8: 84})
        self._comp_delegate = CompositionDelegate(self.exam_table)
        self.exam_table.setItemDelegateForColumn(4, self._comp_delegate)
        self.exam_table.setToolTip("所有考试。选中一场，下方列出它的批次；双击直接进入扫描（新批次）")
        _header_tips(self.exam_table, EXAM_COLUMN_TIPS)
        set_empty_text(self.exam_table, EXAM_EMPTY)
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
        bhead.addWidget(HelpButton("batch"))
        bhead.addStretch(1)
        self.cont_btn = QPushButton("继续扫描")
        self.cont_btn.setToolTip(CONTINUE_TIP)
        self.cont_btn.clicked.connect(lambda: self._batch_action(self.continueBatch))
        self.res_btn = QPushButton("查看结果")
        self.res_btn.setToolTip(RES_TIP)
        self.res_btn.clicked.connect(lambda: self._batch_action(self.showResults))
        self.bdir_btn = QPushButton("打开批次目录")
        self.bdir_btn.setToolTip(BDIR_TIP)
        self.bdir_btn.clicked.connect(self._open_batch_dir)
        for b in (self.bdir_btn, self.cont_btn, self.res_btn):
            bhead.addWidget(b)
        ml.addLayout(bhead)
        self.batch_table = self._table(["批次", "开始", "张数", "成功", "物理异常", "作废", "数据警告", "转写", "状态"],
                                       stretch=(0,), widths={1: 112, 2: 64, 3: 64, 4: 84, 5: 64, 6: 84, 7: 110, 8: 80})
        self.batch_table.setToolTip("选中考试的批次（一次连续扫描为一批）。双击打开结果页")
        _header_tips(self.batch_table, BATCH_COLUMN_TIPS)
        self.batch_table.itemDoubleClicked.connect(lambda _i: self._batch_action(self.showResults))
        self.batch_table.itemSelectionChanged.connect(self._batch_selected)
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
        self.template_chosen = False  # 用户是否手动选过模板（模板页保存后不覆盖用户的选择）
        self.f_template.activated.connect(lambda _i: setattr(self, "template_chosen", True))
        tips = {
            self.f_name: ("必填。考试的名字，如“2026 考研模拟一”；显示在考试列表里，也用于考试目录名", "必填，如：2026 考研模拟一"),
            self.f_subject: ("选填。如“英语一”；同名考试靠科目区分，也写入结果", "选填，如：英语一"),
            self.f_code: ("选填。科目代码，如“201”；写入结果，目录命名可用 {code}", "选填，如：201"),
            self.f_site: ("选填。考点名称；写入结果，目录命名可用 {site}", "选填"),
            self.f_room: ("选填。考场号；写入结果，目录命名可用 {room}", "选填"),
            self.f_operator: ("扫描人员姓名，写入结果。默认值在“设置”页改", "扫描人员姓名"),
        }
        for w, (tip, placeholder) in tips.items():
            w.setToolTip(tip)
            w.setPlaceholderText(placeholder)
        self.f_name.textChanged.connect(lambda _t: self._update_create_tip())
        for label, w in (("考试名称", self.f_name), ("科目", self.f_subject), ("科目代码", self.f_code),
                         ("考点", self.f_site), ("考场", self.f_room), ("操作员", self.f_operator),
                         ("模板", self.f_template)):
            form.addRow(label, with_help(w, "exam_template_snapshot") if w is self.f_template else w)
            if isinstance(w, QLineEdit):
                # 回车跳到下一项，不直接创建：避免只填了名称就误建考试
                w.returnPressed.connect(self.focusNextChild)
        sl.addLayout(form)
        self.out_hint = ElidedLabel("")
        self.out_hint.setObjectName("muted")
        self.out_hint.setContentsMargins(16, 0, 16, 12)
        self.out_hint.setToolTip("新考试会建在输出根目录下；要换位置到“设置”页改")
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

    def _update_template_tip(self) -> None:
        if self.f_template.count():
            self.f_template.setToolTip("这场考试用的答题卡模板。创建时会复制一份固定下来，之后再改模板不影响这场考试")
        else:
            self.f_template.setToolTip("没有可用的模板：先到“模板”页新建模板，或在设置里添加模板目录")

    def _update_create_tip(self) -> None:
        """创建按钮不禁用（点了会提示缺什么），悬停说明随表单状态告诉用户还差什么。"""
        if not self.f_template.count():
            tip = "还不能创建：没有可用的模板，先到“模板”页新建模板"
        elif not self.f_name.text().strip():
            tip = "还不能创建：先填写考试名称"
        else:
            tip = "按填写的信息新建考试（固定所选模板的快照），并直接进入扫描页开始第一批"
        self.create_btn.setToolTip(tip)

    # ---------- 数据 ----------
    def refresh(self, selected_template: str | Path | None = None) -> None:
        """重读模板、考试和批次；保留原来选中的考试与批次，找不到时才选第一行。"""
        selected_template = selected_template or self.f_template.currentData()
        self.f_template.clear()
        for t in list_templates(self.settings.template_dirs):
            self.f_template.addItem(f"{t.name}（v{t.version}，{'双面' if t.faces == 2 else '单面'}，{t.questions} 题）",
                                    str(t.directory))
            if selected_template and t.directory.resolve() == Path(selected_template).resolve():
                self.f_template.setCurrentIndex(self.f_template.count() - 1)
        # 默认操作员在设置里改过：用户没手动改操作员框时跟着更新
        if self.f_operator.text().strip() == self._operator_default.strip():
            self.f_operator.setText(self.settings.operator)
        self._operator_default = self.settings.operator
        self.out_hint.setText(f"输出到：{self.settings.output_root}")
        self._update_template_tip()
        self._update_create_tip()
        prev = self.selected()
        prev_dir = prev.directory if prev else None
        prev_batch = self._selected_batch_dir()
        problem = output_root_problem(self.settings.output_root)
        set_empty_text(self.exam_table, problem + "。可到“设置”页确认或修改输出根目录。" if problem else EXAM_EMPTY)
        self.exams = list_exams(self.settings.output_root)
        default_root = _same(Path(self.settings.output_root).expanduser(), default_output_root())
        self.root_hint.setText(f"提示：结果默认存到“{self.settings.output_root}”。要换位置，先到“设置”页改输出根目录再新建考试。")
        self.root_hint.setVisible(not self.exams and default_root)
        self.exam_table.blockSignals(True)  # 重建期间不逐行触发批次刷新，最后统一刷新一次
        try:
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
                rows = [i for i, e in enumerate(self.exams) if prev_dir is not None and _same(e.directory, prev_dir)]
                self.exam_table.selectRow(rows[0] if rows else 0)
        finally:
            self.exam_table.blockSignals(False)
        self._exam_selected(keep_batch=prev_batch)

    def template_saved(self, directory: str | Path | None) -> None:
        """模板页保存了模板：刷新列表；用户没选过模板时才默认选中刚保存的。"""
        self.refresh(selected_template=None if self.template_chosen else directory)

    def selected(self) -> Exam | None:
        i = self.exam_table.currentRow()
        return self.exams[i] if 0 <= i < len(self.exams) else None

    def _selected_batch_dir(self) -> Path | None:
        i = self.batch_table.currentRow()
        return self.batches[i].directory if 0 <= i < len(self.batches) else None

    def _exam_selected(self, keep_batch: Path | None = None) -> None:
        keep_batch = keep_batch or self._selected_batch_dir()
        e = self.selected()
        self.batches = list_batches(e.directory) if e else []
        set_empty_text(self.batch_table, "这场考试还没有批次。点上方“进入扫描（新批次）”开始扫描第一批。" if e
                       else "选中一场考试后，这里列出它的批次（每次连续扫描为一批）。")
        open_dir = self.open_batch_fn()
        busy = self.busy_fn()
        self.batch_table.blockSignals(True)
        try:
            self.batch_table.setRowCount(len(self.batches))
            for i, b in enumerate(self.batches):
                c = b.counts
                p = b.ocr_progress
                ocr = f"{p['done']} / {p['total']}" + (f"（失败 {p['failed']}）" if p.get("failed") else "") if p else (
                    "等待中" if b.ocr else "未开启")
                state = b.state
                if open_dir and _same(b.directory, Path(open_dir)):
                    state += "（扫描中）" if busy else "（已打开）"  # 扫描页当前打开的批次
                vals = [b.batch_id, b.created[5:16].replace("T", " "), str(c.get("total", 0)), str(c.get("ok", 0)),
                        str(c.get("physical", 0)), str(c.get("voided", 0)), str(c.get("warnings", 0)), ocr, state]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j in (0, 7, 8):
                        it.setToolTip(v)
                    self.batch_table.setItem(i, j, it)
            if self.batches:
                rows = [i for i, b in enumerate(self.batches)
                        if keep_batch is not None and _same(b.directory, keep_batch)]
                self.batch_table.selectRow(rows[0] if rows else 0)
        finally:
            self.batch_table.blockSignals(False)
        self.scan_btn.setEnabled(e is not None and not self._preparing)
        self.open_dir_btn.setEnabled(e is not None)
        for w in (self.cont_btn, self.res_btn, self.bdir_btn):
            w.setEnabled(bool(self.batches))
        if e is None:
            self._template_check.cancel()
            self.scan_btn.setToolTip(NO_EXAM_TIP + "，再进入扫描")
            self.open_dir_btn.setToolTip(NO_EXAM_TIP)
        else:
            # 模板不可用时按钮仍可点（点了会说明原因），悬停先提示。检查要解码模板底图（每场考试约 0.1–0.3 秒），
            # 放到后台线程，避免点选考试时界面卡顿；结果回来前按可用提示，进入扫描时主窗口还会再检查一次
            self.scan_btn.setToolTip(SCAN_TIP)
            self.open_dir_btn.setToolTip(OPEN_DIR_TIP)
            self._template_check.submit(lambda: template_problem(e), self._apply_template_check)
        self._batch_selected()

    def _apply_template_check(self, problem: str | None) -> None:
        self.scan_btn.setToolTip(f"模板不可用，暂时不能新建批次：{problem}" if problem else SCAN_TIP)

    def _batch_selected(self) -> None:
        """已结算的批次：继续扫描时扫描页会让用户选择追加或新建批次，提示与之一致。"""
        i = self.batch_table.currentRow()
        if not self.batches:
            why = NO_EXAM_TIP if self.selected() is None else NO_BATCH_TIP
            for b in (self.cont_btn, self.res_btn, self.bdir_btn):
                b.setToolTip(f"暂不可用：{why}")
            return
        finalized = 0 <= i < len(self.batches) and self.batches[i].finalized
        self.cont_btn.setToolTip(FINALIZED_TIP if finalized else CONTINUE_TIP)
        self.res_btn.setToolTip(RES_TIP)
        self.bdir_btn.setToolTip(BDIR_TIP)

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
        if self.busy_fn():
            # 忙碌时创建了也进不了扫描页：先不在磁盘上创建考试
            QMessageBox.information(self, "提示", BUSY_TIP)
            return
        name = self.f_name.text().strip()
        tdir = self.f_template.currentData()
        if not name:
            QMessageBox.information(self, "提示", "请填写考试名称。")
            return
        if not tdir:
            QMessageBox.information(self, "提示", "没有可用的模板，请先在“模板”页添加模板目录。")
            return
        subject = self.f_subject.text().strip()
        if any(e.info.name == name and e.info.subject == subject for e in self.exams):
            label = name + (f"（{subject}）" if subject else "")
            if QMessageBox.question(self, "同名考试", f"已有同名考试“{label}”，仍要新建一场吗？"
                                                      "继续扫描已有考试请在左侧选中它。"
                                    ) != QMessageBox.StandardButton.Yes:
                return
        info = ExamInfo(name=name, subject=subject, subject_code=self.f_code.text().strip(),
                        site=self.f_site.text().strip(), room=self.f_room.text().strip(),
                        operator=self.f_operator.text().strip())
        root, pattern = self.settings.output_root, self.settings.exam_dir_pattern
        # 复制并校验模板快照要解码底图，放到后台；期间表单与按钮不可用，避免重复创建或改了又被清空
        self._set_form_enabled(False)
        self.create_btn.setText("正在创建…")

        def created(exam: Exam) -> None:
            self._create_finished()
            self._reset_form()
            self.refresh()
            self._select_exam(exam.directory)
            if not self.isHidden():  # 创建期间用户切到了别的页面：只建好考试，不把人拉去扫描页
                self.startScan.emit(exam)

        def failed(e: BaseException) -> None:
            self._create_finished()
            detail = str(e) if isinstance(e, (OSError, ValueError)) else f"{type(e).__name__}: {e}"
            QMessageBox.warning(self, "提示", f"创建考试失败：{detail}")

        self._creating.submit(lambda: create_exam(root, info, tdir, pattern), created, failed)

    def _create_finished(self) -> None:
        self._set_form_enabled(True)
        self.create_btn.setText("创建并进入扫描")

    def _set_form_enabled(self, enabled: bool) -> None:
        for w in (self.f_name, self.f_subject, self.f_code, self.f_site, self.f_room, self.f_operator,
                  self.f_template, self.create_btn):
            w.setEnabled(enabled)

    def set_preparing(self, preparing: bool) -> None:
        """主窗口在后台检查模板期间：“进入扫描”显示进度并暂不可点，避免重复进入。"""
        self._preparing = preparing
        self.scan_btn.setText("正在检查模板…" if preparing else "进入扫描（新批次）")
        self.scan_btn.setEnabled(not preparing and self.selected() is not None)

    def _reset_form(self) -> None:
        """创建成功后清空表单（模板保留），避免再点一次重复创建。"""
        for w in (self.f_name, self.f_subject, self.f_code, self.f_site, self.f_room):
            w.clear()
        self.f_operator.setText(self.settings.operator)
        self._operator_default = self.settings.operator

    def _select_exam(self, directory: Path) -> None:
        for i, e in enumerate(self.exams):
            if _same(e.directory, directory):
                self.exam_table.selectRow(i)
                return


def _header_tips(table: QTableWidget, tips: list[str]) -> None:
    """表头悬停说明各列含义。"""
    for column, tip in enumerate(tips):
        item = table.horizontalHeaderItem(column)
        if item is not None:
            item.setToolTip(tip)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a == b
