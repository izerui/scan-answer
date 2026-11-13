"""考试页：全宽考试表，点行原位展开（考试详情 + 批次子表）；考试、批次操作在各自行尾；新建考试在右侧抽屉里。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

import threading

from PySide6.QtCore import QMetaMethod, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

from ...app.library import (Exam, ExamInfo, batch_totals, template_info, create_exam, list_batches, list_exams,
                            list_templates, output_root_problem, template_problem)
from ...app.settings import AppSettings, default_output_root
from .. import theme
from ..background import Latest
from ..widgets.common import ElidedLabel, Swatch, hline, set_empty_text, setup_table
from ..widgets.exam_drawer import Drawer
from ..widgets.exam_table import ROW_H, CompositionDelegate, ExamTable  # noqa: F401 — CompositionDelegate 供外部沿用
from ..widgets.guide import HelpButton


def open_in_file_manager(path: Path) -> None:
    """在系统文件管理器里打开目录。启动外部程序可能要等一会儿（Windows 的 startfile 会等资源管理器响应），放在后台线程。"""
    def run() -> None:
        try:
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606 — 打开资源管理器
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError:
            pass  # 目录已被删除或没有文件管理器：不影响程序

    threading.Thread(target=run, daemon=True, name="open-dir").start()


CONTINUE_TIP = "在扫描页打开这个批次，接着扫描"
FINALIZED_TIP = "该批次已结束，继续扫描时可选择追加到该批次或新建批次"
BUSY_TIP = "当前批次还在进行中，请等这次导入 / 扫描完成，或在扫描页点“停止导入剩余文件 / 停止进纸”后再切换。"

SCAN_TEXT = "扫描新批次"
EXAM_EMPTY = "还没有考试。点右上角“新建考试”，填写考试名称、选择模板后点“创建并进入扫描”。"
BATCH_EMPTY = "这场考试还没有批次。点本行右侧的“扫描新批次”开始扫描第一批。"
SCAN_TIP = "在这场考试下新建一个批次，进入扫描页开始扫描（也可双击考试）"
OPEN_DIR_TIP = "在文件管理器中打开这场考试的文件夹（所用模板的副本、各批次结果都在里面）"
RES_TIP = "在结果页打开这个批次，核对识别结果（也可双击批次）"
BDIR_TIP = "在文件管理器中打开这个批次的文件夹（原图、识别结果、切图）"
PREPARING_OTHER_TIP = "暂不可用：正在为选中的考试检查模板、准备进入扫描，请稍候"
NEW_TIP = "打开新建考试表单：填写考试信息、选择模板，创建后直接进入扫描页"
EXAM_COLUMN_TIPS = ["考试名称（点行左侧的三角或按 ← / → 收起、展开批次）", "科目", "考点",
                    "这场考试所用的模板（创建时复制的一份，之后改模板不影响它）",
                    "所有批次答卷的状态比例：绿＝成功，黄＝有警告，红＝异常卡，灰＝作废",
                    "所有批次已扫的答卷张数", "识别成功的张数（含有警告的）",
                    "异常卡张数：没扫好或对不上模板、没有识别结果，需要在扫描页处理",
                    "警告条数：识别了，但要人看一眼的问题（存疑、多涂、考号重复等），一张卡可能有多条",
                    "这场考试的操作：打开考试目录、扫描新批次"]
BATCH_COLUMN_TIPS = ["批次编号（也是批次文件夹名）", "批次创建时间", "这一批已扫的答卷张数",
                     "识别成功的张数（含有警告的）", "异常卡张数：没扫好或对不上模板，需要在扫描页处理",
                     "已作废的张数（不参与交付，可在扫描页撤销作废）", "警告条数：识别了，但要人看一眼的问题，一张卡可能有多条",
                     "大模型转写进度：已完成 / 总数；“未开启”表示这批不转写",
                     "未结束＝还能接着扫；已结束＝点过“结束批次”，可以交付",
                     "这一批的操作：继续扫描、查看结果、打开目录"]
OPS_COL = 9  # 两张表的“操作”列
BATCH_ROW_H = 33

# 行尾的文字链接按钮；收起行用更淡的颜色（不藏起来：键盘和不便悬停时也能直接点）
_LINK = ("QPushButton {{ border: 1px solid transparent; background: transparent; color: {ink}; padding: 4px 8px; }}"
         "QPushButton:hover {{ color: $INK; text-decoration: underline; }}"
         "QPushButton:focus {{ border-color: $ACCENT; }}"
         "QPushButton:disabled {{ color: $DISABLED; background: transparent; }}")
LINK_STYLE = _LINK.format(ink="$INK2")
LINK_DIM_STYLE = _LINK.format(ink="$INK3")
DIM_BUTTON_STYLE = ("QPushButton { color: $INK2; border-color: $LINE; background: transparent; }"
                    "QPushButton:hover { color: $INK; border-color: $INK2; }")
SMALL_BUTTON_STYLE = "QPushButton { padding: 2px 10px; font-size: 13px; }"


def _set_role(button: QPushButton, name: str, style: str) -> None:
    """切换按钮的样式角色（主按钮 / 普通 / 淡色），并让新的 objectName 立即生效。"""
    if button.objectName() == name and button.property("_role_style") == style:
        return
    button.setObjectName(name)
    button.setProperty("_role_style", style)
    theme.set_style(button, style)
    button.style().unpolish(button)
    button.style().polish(button)


class ExamOps(QWidget):
    """考试行尾：打开考试目录、扫描新批次。只占行的上沿，展开行下面的部分留给展开区。"""

    def __init__(self) -> None:
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        row = QWidget()
        row.setFixedHeight(ROW_H)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 10, 0)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.open_dir = QPushButton("打开考试目录")
        self.open_dir.setToolTip(OPEN_DIR_TIP)
        self.scan = QPushButton(SCAN_TEXT)
        self.scan.setToolTip(SCAN_TIP)
        lay.addWidget(self.open_dir)
        lay.addWidget(self.scan)
        outer.addWidget(row)
        outer.addStretch(1)
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        """展开的行：“扫描新批次”是主按钮；其它行用淡色。"""
        _set_role(self.open_dir, "", LINK_STYLE if active else LINK_DIM_STYLE)
        _set_role(self.scan, "primary" if active else "", "" if active else DIM_BUTTON_STYLE)


class BatchOps(QWidget):
    """批次行尾：继续扫描、查看结果、打开目录。"""

    def __init__(self, finalized: bool) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(6)
        lay.addStretch(1)
        self.cont = QPushButton("继续扫描")
        self.cont.setToolTip(FINALIZED_TIP if finalized else CONTINUE_TIP)
        self.res = QPushButton("查看结果")
        self.res.setToolTip(RES_TIP)
        self.bdir = QPushButton("打开目录")
        self.bdir.setToolTip(BDIR_TIP)
        for b in (self.cont, self.res):
            theme.set_style(b, SMALL_BUTTON_STYLE)
        theme.set_style(self.bdir, LINK_STYLE)
        for b in (self.cont, self.res, self.bdir):
            lay.addWidget(b)


class ExamPage(QWidget):
    startScan = Signal(object)  # Exam：在该考试下扫描（新批次）
    continueBatch = Signal(object, str)  # Exam, 批次目录
    showResults = Signal(object, str)  # Exam, 批次目录
    openSettings = Signal()  # 抽屉里“到设置页修改”：由主窗口接到设置页

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.exams: list[Exam] = []
        self.batches = []
        # 刷新时在后台读好的各考试批次与模板信息：选中考试时直接用，不再读文件
        self._exam_batches: dict[Path, list] = {}
        self._exam_templates: dict[Path, object] = {}
        self._loader = Latest(self)  # 刷新（读模板、考试、每个批次的 manifest）在后台，只认最后一次
        self._loaded = False  # 已经读过一次（之前表格显示“正在读取…”）
        # 由主窗口设置：扫描是否忙碌、扫描页当前打开的批次目录（批次列表据此标注）
        self.busy_fn: Callable[[], bool] = lambda: False
        self.open_batch_fn: Callable[[], str | None] = lambda: None
        self._operator_default = settings.operator  # 操作员框上次同步的默认值；用户改过就不再覆盖
        self._template_check = Latest(self)  # 选中考试时的模板检查：选中的考试变了，旧检查的结果作废
        self._preparing = False  # 主窗口正在后台检查模板、准备进入扫描
        self._preparing_text = "正在检查模板…"
        self._opening_batch: Path | None = None  # 正在后台打开（继续扫描）的批次
        self._scan_tip = SCAN_TIP  # 选中考试的“扫描新批次”提示（模板不可用时说明原因）
        self._collapsed: Path | None = None  # 用户收起的考试（刷新后保持收起；选别的考试即展开）
        self._creating = Latest(self, wait_for_modal=True)  # 新建考试（复制并校验模板快照）在后台执行
        self._build()
        self.refresh()

    # ---------- 界面 ----------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        main = QWidget()
        main.setObjectName("work")
        ml = QVBoxLayout(main)
        ml.setContentsMargins(28, 22, 28, 14)
        ml.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        h = QLabel("考试")
        h.setObjectName("h1")
        titles.addWidget(h)
        lead = QLabel("每场考试对应一个模板；批次在扫描页创建，扫描中或结束后都可在结果页查看。")
        theme.set_style(lead, "color:$INK2;")
        titles.addWidget(lead)
        top.addLayout(titles, 1)
        self.new_btn = QPushButton("新建考试")
        self.new_btn.setToolTip(NEW_TIP)
        self.new_btn.clicked.connect(self.open_drawer)
        top.addWidget(self.new_btn, 0, Qt.AlignmentFlag.AlignBottom)
        ml.addLayout(top)
        # 情境提示：还没有考试且输出根目录是默认值时，提醒先到设置页确认结果存哪
        self.root_hint = QLabel("")
        self.root_hint.setWordWrap(True)
        theme.set_style(self.root_hint, "color:$INK3; font-size:13px; background:$NOTICE; border-radius:5px;"
                                        " padding:8px 10px;")
        self.root_hint.setVisible(False)
        ml.addWidget(self.root_hint)

        self.exam_table = ExamTable(0, 10)
        self._setup_table(self.exam_table, ["考试", "科目", "考点", "模板", "答卷状态", "已扫", "成功", "异常卡", "警告",
                                            "操作"],
                          stretch=(0,), widths={1: 110, 2: 120, 3: 130, 4: 110, 5: 64, 6: 64, 7: 84, 8: 84, 9: 236},
                          numeric=(5, 6, 7, 8, 9), row_height=ROW_H)
        self._comp_delegate = CompositionDelegate(self.exam_table)
        self.exam_table.setItemDelegateForColumn(4, self._comp_delegate)
        self.exam_table.setToolTip("所有考试。点一行展开它的批次；双击直接扫描新批次")
        _header_tips(self.exam_table, EXAM_COLUMN_TIPS)
        set_empty_text(self.exam_table, EXAM_EMPTY)
        self.exam_table.itemSelectionChanged.connect(self._exam_selected)
        self.exam_table.itemDoubleClicked.connect(lambda _i: self._scan_selected())
        self.exam_table.toggleRequested.connect(self._toggle)
        theme.set_style(self.exam_table, "QTableView { border: 1px solid $LINE; border-radius: 4px; }")
        self.exam_table.set_detail_widget(self._build_detail())
        ml.addWidget(self.exam_table, 1)

        foot = QHBoxLayout()
        foot.setSpacing(14)
        self.legend = QWidget()
        lg = QHBoxLayout(self.legend)
        lg.setContentsMargins(0, 0, 0, 0)
        lg.setSpacing(14)
        for token, label in (("OK_FILL", "成功"), ("WARN_FILL", "有警告"), ("ERR", "异常卡"), ("DISABLED", "作废")):
            cell = QHBoxLayout()
            cell.setSpacing(4)
            cell.addWidget(Swatch(token))
            lab = QLabel(label)
            lab.setObjectName("muted")
            cell.addWidget(lab)
            lg.addLayout(cell)
        self.legend.setToolTip("“答卷状态”列比例条的颜色")
        foot.addWidget(self.legend)
        foot.addStretch(1)
        how = QLabel("点行展开批次 · 点三角或按 ← / → 收起、展开 · 双击考试直接扫描新批次 · 双击批次查看结果")
        how.setObjectName("muted")
        foot.addWidget(how)
        ml.addLayout(foot)
        outer.addWidget(main, 1)
        self._build_drawer(main)

    def _build_detail(self) -> QWidget:
        """展开区：考试详情一行 + 批次子表。"""
        d = QFrame()
        d.setObjectName("examDetail")
        theme.set_style(d, "#examDetail { background: $PANEL; border: none; border-left: 3px solid $ACCENT;"
                           " border-bottom: 1px solid $LINE; }")
        self.detail = d
        lay = QVBoxLayout(d)
        lay.setContentsMargins(43, 10, 14, 12)
        lay.setSpacing(8)
        hd = QHBoxLayout()
        hd.setSpacing(8)
        h3 = QLabel("批次")
        theme.set_style(h3, "font-size:14px; font-weight:600; color:$INK;")
        hd.addWidget(h3)
        hd.addWidget(HelpButton("batch"))
        self.d_count = QLabel("")
        theme.set_style(self.d_count, "color:$INK3; font-size:13px;")
        hd.addWidget(self.d_count)
        hd.addSpacing(18)
        self.d_values: dict[str, QLabel] = {}
        for key, label in (("code", "科目代码"), ("room", "考场"), ("operator", "操作员"), ("created", "创建"),
                           ("template", "所用模板")):
            name = QLabel(label)
            theme.set_style(name, "color:$INK3; font-size:13px;")
            value = ElidedLabel("", Qt.TextElideMode.ElideRight) if key == "template" else QLabel("")
            value.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)  # 放得下就不截断
            theme.set_style(value, "color:$INK2; font-size:13px;")
            hd.addWidget(name)
            hd.addWidget(value)
            if key == "template":
                hd.addWidget(HelpButton("exam_template_snapshot"))
            else:
                hd.addSpacing(12)
            self.d_values[key] = value
        hd.addStretch(1)
        lay.addLayout(hd)

        self.batch_table = QTableWidget(0, 10)
        self._setup_table(self.batch_table, ["批次", "开始", "张数", "成功", "异常卡", "作废", "警告", "转写", "状态",
                                             "批次操作"],
                          stretch=(0,), widths={1: 100, 2: 56, 3: 56, 4: 80, 5: 56, 6: 80, 7: 100, 8: 130, 9: 250},
                          numeric=(2, 3, 4, 5, 6, 9), row_height=BATCH_ROW_H)
        self.batch_table.setToolTip("这场考试的批次（一次连续扫描为一批）。双击打开结果页")
        _header_tips(self.batch_table, BATCH_COLUMN_TIPS)
        self.batch_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.batch_table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        theme.set_style(self.batch_table, "QTableView { border: 1px solid $LINE; border-radius: 4px; }")
        self.batch_table.itemDoubleClicked.connect(lambda _i: self._batch_action(self.showResults))
        lay.addWidget(self.batch_table)
        return d

    def _build_drawer(self, behind: QWidget) -> None:
        self.drawer = Drawer(self, behind)
        self.drawer_scrim = self.drawer.scrim
        dl = QVBoxLayout(self.drawer)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(0)
        hd = QHBoxLayout()
        hd.setContentsMargins(22, 18, 22, 14)
        titles = QVBoxLayout()
        titles.setSpacing(4)
        h2 = QLabel("新建考试")
        theme.set_style(h2, "font-size:18px; font-weight:600;")
        titles.addWidget(h2)
        sub = QLabel("填好后创建，并直接进入扫描页开始第一批。")
        sub.setObjectName("muted")
        titles.addWidget(sub)
        hd.addLayout(titles, 1)
        self.drawer_close_btn = QPushButton("关闭")
        self.drawer_close_btn.setToolTip("关闭，不创建（填写的内容保留）")
        theme.set_style(self.drawer_close_btn, LINK_STYLE)
        self.drawer_close_btn.clicked.connect(self.close_drawer)
        hd.addWidget(self.drawer_close_btn, 0, Qt.AlignmentFlag.AlignTop)
        dl.addLayout(hd)
        dl.addWidget(hline())

        body = QVBoxLayout()
        body.setContentsMargins(22, 18, 22, 18)
        body.setSpacing(14)
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
            self.f_subject: ("选填。如“英语一”；同名考试靠科目区分，也写入结果", "如：英语一"),
            self.f_code: ("选填。科目代码，如“201”；写入结果，目录命名可用 {code}", "如：201"),
            self.f_site: ("选填。考点名称；写入结果，目录命名可用 {site}", "考点名称"),
            self.f_room: ("选填。考场号；写入结果，目录命名可用 {room}", "考场号"),
            self.f_operator: ("扫描人员姓名，写入结果。默认值在“设置”页改", "扫描人员姓名"),
        }
        for w, (tip, placeholder) in tips.items():
            w.setToolTip(tip)
            w.setPlaceholderText(placeholder)
            # 回车跳到下一项，不直接创建：避免只填了名称就误建考试
            w.returnPressed.connect(self.focusNextChild)
        self.f_name.textChanged.connect(lambda _t: self._update_create_tip())
        body.addWidget(_field("考试名称", self.f_name, required=True))
        for pair in ((("科目", self.f_subject), ("科目代码", self.f_code)), (("考点", self.f_site), ("考场", self.f_room))):
            grid = QGridLayout()
            grid.setHorizontalSpacing(12)
            for col, (label, w) in enumerate(pair):
                grid.addWidget(_field(label, w, note="选填"), 0, col)
            body.addLayout(grid)
        body.addWidget(_field("操作员", self.f_operator, note="默认值在“设置”页改"))
        tpl_row = QWidget()
        tl = QHBoxLayout(tpl_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        tl.addWidget(self.f_template, 1)
        tl.addWidget(HelpButton("exam_template_snapshot"))
        tpl = _field("模板", tpl_row, required=True)
        note = QLabel("创建时会把模板复制一份给这场考试用，之后再改模板不影响它")
        note.setObjectName("muted")
        tpl.layout().addWidget(note)
        body.addWidget(tpl)
        out = QFrame()
        out.setObjectName("outBox")
        theme.set_style(out, "#outBox { background: $WORK; border: 1px solid $LINE; border-radius: 6px; }")
        out.setToolTip("新考试会建在输出根目录下；要换位置到“设置”页改")
        ol = QHBoxLayout(out)
        ol.setContentsMargins(12, 8, 8, 8)
        ol.setSpacing(4)
        self.out_hint = ElidedLabel("")
        theme.set_style(self.out_hint, "color:$INK2; font-size:13px;")
        ol.addWidget(self.out_hint, 1)
        self.settings_link = QPushButton("到设置页修改")
        self.settings_link.setToolTip("到“设置”页修改输出根目录（新考试建在它下面）")
        theme.set_style(self.settings_link, _LINK.format(ink="$ACCENT"))
        self.settings_link.clicked.connect(self._goto_settings)
        ol.addWidget(self.settings_link)
        body.addWidget(out)
        body.addStretch(1)
        dl.addLayout(body, 1)
        dl.addWidget(hline())

        ft = QHBoxLayout()
        ft.setContentsMargins(22, 14, 22, 14)
        ft.setSpacing(8)
        enter = QLabel("回车跳到下一项，不会直接创建")
        enter.setObjectName("muted")
        ft.addWidget(enter, 1)
        self.drawer_cancel_btn = QPushButton("取消")
        self.drawer_cancel_btn.setToolTip("关闭表单，不创建（填写的内容保留）")
        self.drawer_cancel_btn.clicked.connect(self.close_drawer)
        ft.addWidget(self.drawer_cancel_btn)
        self.create_btn = QPushButton("创建并进入扫描")
        self.create_btn.setObjectName("primary")
        self.create_btn.clicked.connect(self._create)
        ft.addWidget(self.create_btn)
        dl.addLayout(ft)

    @staticmethod
    def _setup_table(t: QTableWidget, headers: list[str], stretch: tuple[int, ...], widths: dict[int, int],
                     numeric: tuple[int, ...], row_height: int) -> None:
        t.setHorizontalHeaderLabels(headers)
        setup_table(t, row_height=row_height)
        t.setTabKeyNavigation(False)  # Tab 离开表格，走到行尾按钮和其它控件
        t.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = t.horizontalHeader()
        for column in stretch:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Stretch)
        for column, width in widths.items():
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            t.setColumnWidth(column, width)
        for column in numeric:
            t.horizontalHeaderItem(column).setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _update_template_tip(self) -> None:
        if self.f_template.count():
            self.f_template.setToolTip("这场考试用的答题卡模板。创建时会复制一份固定下来，之后再改模板不影响这场考试")
        else:
            self.f_template.setToolTip("没有可用的模板：先到“模板”页新建模板，或点“从文件夹添加模板…”添加已有模板")

    def _update_create_tip(self) -> None:
        """创建按钮不禁用（点了会提示缺什么），悬停说明随表单状态告诉用户还差什么。"""
        if not self.f_template.count():
            tip = "还不能创建：没有可用的模板，先到“模板”页新建模板"
        elif not self.f_name.text().strip():
            tip = "还不能创建：先填写考试名称"
        else:
            tip = "按填写的信息新建考试（复制一份所选模板给这场考试用），并直接进入扫描页开始第一批"
        self.create_btn.setToolTip(tip)

    # ---------- 抽屉 ----------
    def open_drawer(self) -> None:
        connected = self.isSignalConnected(QMetaMethod.fromSignal(self.openSettings))
        self.settings_link.setVisible(connected)  # 没接到设置页时不显示链接
        self.drawer.open_drawer()
        self.f_name.setFocus()

    def close_drawer(self) -> None:
        self.drawer.close_drawer()

    def _goto_settings(self) -> None:
        self.close_drawer()
        self.openSettings.emit()

    # ---------- 行尾操作 ----------
    def exam_ops(self, row: int) -> ExamOps | None:
        w = self.exam_table.cellWidget(row, OPS_COL) if 0 <= row < self.exam_table.rowCount() else None
        return w if isinstance(w, ExamOps) else None

    def batch_ops(self, row: int) -> BatchOps | None:
        w = self.batch_table.cellWidget(row, OPS_COL) if 0 <= row < self.batch_table.rowCount() else None
        return w if isinstance(w, BatchOps) else None

    # 选中考试 / 批次那一行的按钮（没有行时为 None）
    @property
    def scan_btn(self) -> QPushButton | None:
        ops = self.exam_ops(self.exam_table.currentRow())
        return ops.scan if ops else None

    @property
    def open_dir_btn(self) -> QPushButton | None:
        ops = self.exam_ops(self.exam_table.currentRow())
        return ops.open_dir if ops else None

    @property
    def cont_btn(self) -> QPushButton | None:
        ops = self.batch_ops(self.batch_table.currentRow())
        return ops.cont if ops else None

    @property
    def res_btn(self) -> QPushButton | None:
        ops = self.batch_ops(self.batch_table.currentRow())
        return ops.res if ops else None

    @property
    def bdir_btn(self) -> QPushButton | None:
        ops = self.batch_ops(self.batch_table.currentRow())
        return ops.bdir if ops else None

    def _exam_row_action(self, row: int, action: Callable[[], None]) -> None:
        """行尾按钮：先选中（展开）那一行，再执行与上方按钮相同的动作。"""
        if self.exam_table.currentRow() != row:
            self.exam_table.selectRow(row)
        action()

    def _batch_row_action(self, row: int, action: Callable[[], None]) -> None:
        if self.batch_table.currentRow() != row:
            self.batch_table.selectRow(row)
        action()

    def _update_exam_ops(self) -> None:
        """展开行的“扫描新批次”是主按钮；没有考试时“新建考试”是主按钮。准备进入扫描期间都不可点。"""
        current, expanded = self.exam_table.currentRow(), self.exam_table.expanded_row()
        for i in range(self.exam_table.rowCount()):
            ops = self.exam_ops(i)
            if ops is None:
                continue
            ops.set_active(i == expanded)
            ops.scan.setEnabled(not self._preparing)
            ops.scan.setText(self._preparing_text if self._preparing and i == current else SCAN_TEXT)
            if i == current:
                ops.scan.setToolTip(self._scan_tip)
            else:
                ops.scan.setToolTip(PREPARING_OTHER_TIP if self._preparing else SCAN_TIP)
        _set_role(self.new_btn, "" if self.exams else "primary", "")

    # ---------- 数据 ----------
    def refresh(self, selected_template: str | Path | None = None, select_exam: Path | None = None) -> None:
        """在后台重读模板、考试和批次（每个批次都要读 manifest，考试多时较慢），读完再填表；
        保留原来选中的考试与批次（给了 select_exam 时选它），找不到时才选第一行。连续刷新只认最后一次。"""
        selected_template = selected_template or self.f_template.currentData()
        dirs, root = list(self.settings.template_dirs), self.settings.output_root
        if not self._loaded:
            set_empty_text(self.exam_table, "正在读取考试…")

        def work() -> tuple:
            # 目录都解析成真实路径：界面上比较考试、批次、模板时直接比，不再解析路径
            templates = list_templates(dirs)
            for t in templates:
                t.directory = t.directory.resolve()
            problem = output_root_problem(root)
            exams = list_exams(root)
            for e in exams:
                e.directory = e.directory.resolve()
            batches = {e.directory: list_batches(e.directory) for e in exams}
            for items in batches.values():
                for b in items:
                    b.directory = b.directory.resolve()
            infos = {e.directory: template_info(e.template_dir) for e in exams}
            chosen = Path(selected_template).resolve() if selected_template else None
            select = Path(select_exam).resolve() if select_exam else None
            default_root = Path(root).expanduser().resolve() == default_output_root().resolve()
            return chosen, select, default_root, templates, problem, exams, batches, infos

        self._loader.submit(work, lambda data: self._show_refreshed(*data))

    def _show_refreshed(self, selected_template: Path | None, select_exam: Path | None, default_root: bool,
                        templates: list, problem: str | None,
                        exams: list[Exam], batches: dict[Path, list], infos: dict[Path, object]) -> None:
        """后台读好的数据填进表格（目录都已解析成真实路径）。"""
        self._loaded = True
        self.f_template.clear()
        for t in templates:
            self.f_template.addItem(f"{t.name}（v{t.version}，{'双面' if t.faces == 2 else '单面'}，{t.questions} 题）",
                                    str(t.directory))
            if selected_template is not None and t.directory == selected_template:
                self.f_template.setCurrentIndex(self.f_template.count() - 1)
        # 默认操作员在设置里改过：用户没手动改操作员框时跟着更新
        if self.f_operator.text().strip() == self._operator_default.strip():
            self.f_operator.setText(self.settings.operator)
        self._operator_default = self.settings.operator
        self.out_hint.setText(f"输出到：{self.settings.output_root}")
        self._update_template_tip()
        self._update_create_tip()
        prev = self.selected()
        prev_dir = select_exam or (prev.directory if prev else None)
        prev_batch = self._selected_batch_dir()
        set_empty_text(self.exam_table, problem + "。可到“设置”页确认或修改输出根目录。" if problem else EXAM_EMPTY)
        self.exams = exams
        self._exam_batches, self._exam_templates = batches, infos
        self.root_hint.setText(f"提示：结果默认存到“{self.settings.output_root}”。要换位置，先到“设置”页改输出根目录再新建考试。")
        self.root_hint.setVisible(not self.exams and default_root)
        self.legend.setVisible(bool(self.exams))
        self.exam_table.blockSignals(True)  # 重建期间不逐行触发批次刷新，最后统一刷新一次
        try:
            self.exam_table.set_expanded(-1)
            _drop_ops(self.exam_table)
            self.exam_table.setRowCount(len(self.exams))
            for i, e in enumerate(self.exams):
                tot = batch_totals(batches.get(e.directory, []))
                vals = [e.info.name, e.info.subject, e.info.site, e.template_label,
                        "",  # 构成列由 delegate 绘制
                        str(tot["total"]), str(tot["ok"]), str(tot["physical"]), str(tot["warnings"]), ""]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j < 4:
                        it.setToolTip(v)
                    if j == 4:
                        # 构成条数据存入 UserRole，tooltip 显示各项数量
                        it.setData(Qt.ItemDataRole.UserRole, tot)
                        it.setToolTip(f"成功 {tot['ok']}　警告 {tot['warnings']} 条　异常卡 {tot['physical']}　作废 {tot.get('voided', 0)}")
                    if 5 <= j <= 8:
                        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if j == 7 and tot["physical"]:
                        theme.set_item_colors(it, foreground="ERR")
                    self.exam_table.setItem(i, j, it)
                ops = ExamOps()
                ops.scan.clicked.connect(lambda _c=False, r=i: self._exam_row_action(r, self._scan_selected))
                ops.open_dir.clicked.connect(lambda _c=False, r=i: self._exam_row_action(r, self._open_exam_dir))
                self.exam_table.setCellWidget(i, OPS_COL, ops)
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
        if e is None or self._collapsed is None or not _same(e.directory, self._collapsed):
            self._collapsed = None  # 选了别的考试：展开它
        self.batches = self._exam_batches.get(e.directory, []) if e else []
        set_empty_text(self.batch_table, BATCH_EMPTY if e else "选中一场考试后，这里列出它的批次（每次连续扫描为一批）。")
        open_dir = self.open_batch_fn()
        busy = self.busy_fn()
        self.batch_table.blockSignals(True)
        try:
            _drop_ops(self.batch_table)
            self.batch_table.setRowCount(len(self.batches))
            for i, b in enumerate(self.batches):
                c = b.counts
                p = b.ocr_progress
                ocr = f"{p['done']} / {p['total']}" + (f"（失败 {p['failed']}）" if p.get("failed") else "") if p else (
                    "等待中" if b.ocr else "未开启")
                state = b.state
                is_open = bool(open_dir) and _same(b.directory, Path(open_dir))
                if is_open:
                    state += "（扫描中）" if busy else "（已打开）"  # 扫描页当前打开的批次
                vals = [b.batch_id, b.created[5:16].replace("T", " "), str(c.get("total", 0)), str(c.get("ok", 0)),
                        str(c.get("physical", 0)), str(c.get("voided", 0)), str(c.get("warnings", 0)), ocr, state, ""]
                for j, v in enumerate(vals):
                    it = QTableWidgetItem(v)
                    if j in (0, 7, 8):
                        it.setToolTip(v)
                    if 2 <= j <= 6:
                        it.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                    if j == 4 and c.get("physical", 0):
                        theme.set_item_colors(it, foreground="ERR")
                    if j == 8 and is_open:
                        theme.set_item_colors(it, foreground="ACCENT")
                    self.batch_table.setItem(i, j, it)
                ops = BatchOps(b.finalized)
                ops.cont.clicked.connect(
                    lambda _c=False, r=i: self._batch_row_action(r, lambda: self._batch_action(self.continueBatch)))
                ops.res.clicked.connect(
                    lambda _c=False, r=i: self._batch_row_action(r, lambda: self._batch_action(self.showResults)))
                ops.bdir.clicked.connect(lambda _c=False, r=i: self._batch_row_action(r, self._open_batch_dir))
                self.batch_table.setCellWidget(i, OPS_COL, ops)
            self._update_batch_ops()
            if self.batches:
                rows = [i for i, b in enumerate(self.batches)
                        if keep_batch is not None and _same(b.directory, keep_batch)]
                self.batch_table.selectRow(rows[0] if rows else 0)
        finally:
            self.batch_table.blockSignals(False)
        self._fill_detail(e)
        self._scan_tip = SCAN_TIP
        if e is None:
            self._template_check.cancel()
        else:
            # 模板不可用时按钮仍可点（点了会说明原因），悬停先提示。检查要解码模板底图（每场考试约 0.1–0.3 秒），
            # 放到后台线程，避免点选考试时界面卡顿；结果回来前按可用提示，进入扫描时主窗口还会再检查一次
            self._template_check.submit(lambda: template_problem(e), self._apply_template_check)
        self.exam_table.set_expanded(self.exam_table.currentRow() if e is not None and self._collapsed is None else -1)
        self._update_exam_ops()

    def _fill_detail(self, e: Exam | None) -> None:
        """展开区：考试详情与批次子表高度（子表不滚动，整页随考试表滚动）。"""
        if e is not None:
            info = e.info
            self.d_count.setText(f"{len(self.batches)} 批")
            tpl = self._exam_templates.get(e.directory)
            label = (f"{tpl.name}（v{tpl.version}，{'双面' if tpl.faces == 2 else '单面'}，{tpl.questions} 题）"
                     if tpl else e.template_label)
            for key, value in (("code", info.subject_code), ("room", info.room), ("operator", info.operator),
                               ("created", e.created[:10]), ("template", label)):
                self.d_values[key].setText(value or "—")
        bt = self.batch_table
        bt.horizontalHeader().ensurePolished()
        body = bt.rowCount() * BATCH_ROW_H if bt.rowCount() else 80  # 没有批次：留出空状态说明的位置
        bt.setFixedHeight(bt.horizontalHeader().sizeHint().height() + body + 2 * bt.frameWidth() + 2)

    def _toggle(self, row: int) -> None:
        """点当前行的三角或按 ← / →：收起或展开它。"""
        e = self.selected()
        if e is None or row != self.exam_table.currentRow():
            return
        if self.exam_table.expanded_row() == row:
            self._collapsed = e.directory
            self.exam_table.set_expanded(-1)
        else:
            self._collapsed = None
            self.exam_table.set_expanded(row)
        self._update_exam_ops()

    def _apply_template_check(self, problem: str | None) -> None:
        self._scan_tip = f"模板不可用，暂时不能新建批次：{problem}" if problem else SCAN_TIP
        if self.scan_btn is not None:
            self.scan_btn.setToolTip(self._scan_tip)

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
            QMessageBox.information(self, "提示", "没有可用的模板：请先到“模板”页新建模板，或点“从文件夹添加模板…”添加已有模板。")
            return
        subject = self.f_subject.text().strip()
        if any(e.info.name == name and e.info.subject == subject for e in self.exams):
            label = name + (f"（{subject}）" if subject else "")
            if QMessageBox.question(self, "同名考试", f"已有同名考试“{label}”，仍要新建一场吗？"
                                                      "继续扫描已有考试请在考试列表里选中它。"
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
            self.close_drawer()
            self.refresh(select_exam=exam.directory)  # 列表在后台重读，读完选中刚建的考试
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

    def set_preparing(self, preparing: bool, text: str = "正在检查模板…") -> None:
        """主窗口在后台检查模板、关闭上一批期间：“扫描新批次”显示进度并暂不可点，避免重复进入。"""
        self._preparing, self._preparing_text = preparing, text
        self._update_exam_ops()

    def set_opening(self, batch_dir: Path | None) -> None:
        """主窗口在后台打开批次（继续扫描）期间：该批次的“继续扫描”显示“正在打开…”，各批次的“继续扫描”暂不可点。"""
        self._opening_batch = batch_dir
        self._update_batch_ops()

    def _update_batch_ops(self) -> None:
        for i, b in enumerate(self.batches):
            ops = self.batch_table.cellWidget(i, OPS_COL)
            if isinstance(ops, BatchOps):
                opening = self._opening_batch is not None and _same(b.directory, self._opening_batch)
                ops.cont.setEnabled(self._opening_batch is None)
                ops.cont.setText("正在打开…" if opening else "继续扫描")

    def _reset_form(self) -> None:
        """创建成功后清空表单（模板保留），避免再点一次重复创建。"""
        for w in (self.f_name, self.f_subject, self.f_code, self.f_site, self.f_room):
            w.clear()
        self.f_operator.setText(self.settings.operator)
        self._operator_default = self.settings.operator


def _field(label: str, widget: QWidget, *, required: bool = False, note: str = "") -> QWidget:
    """抽屉表单的一项：上面是标签（必填加红星，选填等小字说明），下面是输入控件。"""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    head = QHBoxLayout()
    head.setSpacing(6)
    lab = QLabel(label)
    theme.set_style(lab, "color:$INK2; font-size:13px;")
    head.addWidget(lab)
    if required:
        star = QLabel("*")
        theme.set_style(star, "color:$ERR; font-size:13px;")
        head.addWidget(star)
    if note:
        n = QLabel(note)
        n.setObjectName("muted")
        head.addWidget(n)
    head.addStretch(1)
    lay.addLayout(head)
    lay.addWidget(widget)
    return w


def _drop_ops(table: QTableWidget) -> None:
    """重建前先撤下旧的行尾按钮：Qt 只是稍后删除它们，期间还会停在旧位置上显示。"""
    for row in range(table.rowCount()):
        w = table.cellWidget(row, OPS_COL)
        if w is not None:
            table.removeCellWidget(row, OPS_COL)
            w.hide()


def _header_tips(table: QTableWidget, tips: list[str]) -> None:
    """表头悬停说明各列含义。"""
    for column, tip in enumerate(tips):
        item = table.horizontalHeaderItem(column)
        if item is not None:
            item.setToolTip(tip)


def _same(a: Path, b: Path) -> bool:
    """同一个目录。两边都是后台解析好的真实路径（考试页刷新时解析、控制器装批次时解析），直接比较，不读磁盘。"""
    return Path(a) == Path(b)
