"""结果页：“核对答卷”与“交付”两种模式。

核对：左侧学生列表（筛选、搜索），中间卡面叠层 / 证据与悬浮翻页条，右侧客观题答案表、主观题切图与转写。
交付：检查交付、阻断项与说明项清单、生成交付目录；右栏批次与转写统计。
整批“转写剩余内容 / 不再转写剩余项…”在核对模式右栏；交付清单里的处理按钮代点它们。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractSlider, QApplication, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSplitter,
                               QStackedWidget, QTableView, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
                               QVBoxLayout, QWidget)

from ...app.results import (PHYSICAL_TEXT, StudentRow, face_overlays, load_batch_template, load_rows, ocr_counts_in,
                            warning_text)
from ...app.settings import AppSettings
from ...ocr.queue import OcrQueue, enable_ocr, fail_unclaimed, reset_failed, skip_pending
from ...output.paths import resolve_path
from ...pipeline.paths import resolve_result_path
from ...template.model import Template, subjective_item_key
from .. import theme
from ..background import Latest
from ..widgets.card_view import CardView, Overlay
from ..widgets.common import ElidedLabel, Segmented, set_empty_text, setup_table
from ..widgets.evidence_preview import EvidencePreview
from ..widgets.guide import HelpButton
from ..widgets.results_parts import BatchSummary, ModeSwitch, OverlayLegend

class _RetrySignals(QObject):
    """后台重试线程 → 界面线程的信号桥。"""
    done = Signal(str, bool, str)  # (owning result or batch, success, message)
    progress = Signal(str, int, int, int)  # (owning result or batch, done, failed, total)


FILL_TIP = ("涂黑程度：0～1，越大越黑；程序按整批情况划分已涂和未涂。"
            "“已涂”的选项计入答案，“未涂”的不计；只列出 0.15 以上的选项")


def fill_text(values: dict, chosen) -> str:
    """答案表“涂黑程度”一格：只列 0.15 以上的选项，每个后面标“已涂 / 未涂”（是否计入答案）。"""
    chosen = set(chosen or [])
    return " / ".join(f"{k} {v:.2f} {'已涂' if k in chosen else '未涂'}" for k, v in values.items() if v >= 0.15)


STATUS_TEXT = {"ok": "正常", "blank": "未答", "multiple": "多涂", "uncertain": "存疑", "partial": "部分", "failed": "失败"}
OVERLAY_COLOR = {"answer": theme.REGION["obj"], "bad": theme.PAPER_ERR, "exam": theme.REGION["id"], "exam_bad": theme.PAPER_ERR,
                 "omr": theme.REGION["id"], "choice": theme.REGION["sub"]}


FILTER_TEXT = {"all": "全部", "warn": "有警告", "err": "异常卡"}
FILTER_TIPS = {"all": "显示这一批的全部答卷",
               "warn": "只看识别了但要核对的卡：存疑、多涂、考号重复、转写失败或等待转写等",
               "err": "只看异常卡（没扫好、对不上模板，没有识别结果）、已作废或结果不可用的卡"}
NO_OCR_TIP = "还没配置大模型转写：到“设置”页填好地址、密钥、模型并保存后才能用"
COUNTS_PENDING = "正在读取各份答卷的转写状态，请稍候…"
NO_BATCH_TIP = "先打开一个批次：在“考试”页选中批次点“查看结果”"
TABLE_NO_BATCH = "尚未打开批次。\n在“考试”页选中批次点“查看结果”，或在扫描页点击拼图格子。"
TABLE_NO_ROWS = "这个批次还没有识别结果。\n到扫描页导入或扫描答卷；回到结果页会自动更新。"
TABLE_FILTERED_EMPTY = "没有符合条件的学生。\n换个筛选，或清空搜索框。"
SUBJ_NO_BATCH = "还没打开批次"
SUBJ_NO_STUDENT = "在左侧选择学生后，这里显示信息栏和主观题的切图与转写文字"
SUBJ_NONE = "这张卡没有信息栏和主观题"
SUBJ_NO_RESULT = "没有识别结果，也就没有切图和转写"


def skip_confirm_text(pending: int) -> str:
    """“不再转写剩余项…”确认框：写明后果（交付结果里没有文字）和以后怎么补。"""
    return (f"本批还有 {pending} 项在等待转写。确定不再转写这 {pending} 项吗？\n\n"
            "交付结果里这些题只有切图、没有文字；交付不再被它们挡住，交付记录会逐项注明“文字转写已放弃”，"
            "转写状态显示为“已放弃转写”。\n\n"
            "以后配好大模型，仍可用“转写剩余内容”再转写。")


LEGEND_FULL_WIDTH = 640  # 中栏比这窄时，识别标记图例改用短说明


class ResultsPage(QWidget):
    batchChanged = Signal(str)
    ocrChanged = Signal(str)
    statusMessage = Signal(str)  # 操作结果提示（转写开始/结束、刷新、导出等），可接到主窗口状态栏

    def __init__(self, settings: AppSettings | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self._active_batch_fn: Callable[[], str | None] | None = None  # 返回当前正在扫描的批次目录
        self.batch_dir: Path | None = None
        self.rows: list[StudentRow] = []
        self.tpl: Template | None = None
        self.result: dict | None = None
        self.current: StudentRow | None = None
        self._warning_texts: list[str] | None = None
        self.face = "front"
        self._retry_owner: str | None = None  # 正在进行的转写任务（结果文件或批次路径）；同一时间只跑一个
        self._data_sig: object = None  # 上次读取的批次数据指纹，用来判断刷新时数据是否变化；None 表示还没完整读过
        self._rows_loader = Latest(self)  # 后台读取各份 result.json（份数多时较慢），只认最后一次
        self._open_stamps: object = None  # 打开批次时各文件的修改时间与大小：首次完整读取时据此判断数据是否已变
        self._counts_pending = False  # 打开批次后，各份答卷的转写状态还在后台统计
        self._retry = _RetrySignals()
        self._retry.done.connect(self._retry_done)
        self._retry.progress.connect(self._retry_progress)
        self._build()
        self._clear_detail()
        self._update_retry_controls()

    # ---------- 布局 ----------
    def _build(self) -> None:
        """顶栏（批次、刷新、模式切换、上次检查）+ “下一步”提示条 + 两种模式：核对答卷 / 交付。"""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_top())
        outer.addWidget(self._build_hint_bar())
        self.mode_stack = QStackedWidget()
        self.check_view = self._build_check()
        self.deliver_view = self._build_deliver()
        self.mode_stack.addWidget(self.check_view)
        self.mode_stack.addWidget(self.deliver_view)
        outer.addWidget(self.mode_stack, 1)
        # 卡面（QGraphicsView）等滚动控件会先吃掉 PgUp/PgDn，窗口级快捷键收不到：在这些控件上直接拦截
        for w in (self.table, self.card, self.answers, self.evidence.view):
            w.installEventFilter(self)

    def _build_top(self) -> QWidget:
        top = QWidget()
        top.setObjectName("resultsTop")
        top.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(top, "#resultsTop { background:$HEADER; border-bottom:1px solid $LINE; }")
        top.setFixedHeight(60)
        grid = QGridLayout(top)
        grid.setContentsMargins(16, 0, 16, 0)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(2, 1)
        left = QHBoxLayout()
        left.setSpacing(12)
        # 两行放进同一容器：最大宽度按两行里较宽的一行算（两个标签各设最大宽度时，
        # 竖排布局取较小值，批次名会被卡到副标题的宽度）
        self.batch_crumb = QWidget()
        crumb = QVBoxLayout(self.batch_crumb)
        crumb.setContentsMargins(0, 0, 0, 0)
        crumb.setSpacing(0)
        self.batch_title = ElidedLabel("未打开批次", Qt.TextElideMode.ElideRight)
        self.batch_title.setObjectName("h2")
        crumb.addWidget(self.batch_title)
        self.batch_sub = ElidedLabel("", Qt.TextElideMode.ElideRight)
        self.batch_sub.setObjectName("muted")
        crumb.addWidget(self.batch_sub)
        for label in (self.batch_title, self.batch_sub):
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        left.addWidget(self.batch_crumb)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setToolTip(f"不能刷新：{NO_BATCH_TIP}")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(lambda: self.refresh(announce=True))
        left.addWidget(self.refresh_btn)
        left.addStretch(1)
        grid.addLayout(left, 0, 0)
        self.mode_switch = ModeSwitch()
        self.mode_buttons = self.mode_switch.buttons
        self.mode_badge = self.mode_switch.badge
        self.mode_switch.changed.connect(self.set_mode)
        grid.addWidget(self.mode_switch, 0, 1, Qt.AlignmentFlag.AlignCenter)
        self.last_check = QLabel("")
        self.last_check.setObjectName("muted")
        grid.addWidget(self.last_check, 0, 2, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return top

    def _build_hint_bar(self) -> QWidget:
        """“下一步”提示条：两种模式都显示；也用来显示刚才操作的结果。"""
        self.hint_bar = QWidget()
        self.hint_bar.setObjectName("hintBar")
        self.hint_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(self.hint_bar, "#hintBar { background:$WARN_BG; border-bottom:1px solid $LINE; }")
        row = QHBoxLayout(self.hint_bar)
        row.setContentsMargins(16, 7, 16, 7)
        row.setSpacing(8)
        self.hint_head = QLabel("下一步")
        theme.set_style(self.hint_head, "color:$WARN; font-weight:600; font-size:13px;")
        row.addWidget(self.hint_head, 0, Qt.AlignmentFlag.AlignTop)
        # 操作提示行：下一步提示、转写进度与结果、刷新后选中项变化等（没有内容时隐藏）
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.set_style(self.notice, "font-size:13px;")
        self.notice.hide()
        self.notice.setToolTip("下一步该做什么，以及刚做完的操作的结果")
        self._notice_is_hint = True  # 提示行当前显示的是“下一步”提示（而不是操作结果）
        row.addWidget(self.notice, 1)
        self.hint_bar.hide()
        return self.hint_bar

    def _build_check(self) -> QWidget:
        """核对模式：左栏学生列表，中间卡面 / 证据与悬浮翻页条，右栏客观题答案与信息栏 / 主观题。"""
        view = QWidget()
        root = QHBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_students())
        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self._build_center())
        self.side = self._build_side()
        split.addWidget(self.side)
        for i in range(split.count()):  # 拖动分隔条不能把卡面或右栏（含整批转写按钮）收没
            split.setCollapsible(i, False)
        split.setStretchFactor(0, 1)
        split.setSizes([680, 404])
        root.addWidget(split, 1)
        return view

    def _build_students(self) -> QWidget:
        left = QWidget()
        left.setObjectName("panel")
        left.setFixedWidth(296)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 16, 8)
        t = QLabel("学生")
        t.setObjectName("h2")
        head.addWidget(t)
        head.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("muted")
        head.addWidget(self.count_label)
        ll.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText("序号、考号或原图文件名")
        self.search.setToolTip("按序号（如 0012）、考号或原图文件名筛选，边输边过滤；清空后显示全部")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._refill)
        sw = QHBoxLayout()
        sw.setContentsMargins(16, 0, 16, 8)
        sw.addWidget(self.search)
        ll.addLayout(sw)
        self.filter = Segmented([("all", "全部"), ("warn", "有警告"), ("err", "异常卡")], "all")
        self.filter.changed.connect(lambda _k: self._refill())
        for key, btn in self.filter.buttons.items():
            btn.setToolTip(FILTER_TIPS[key])
        fw = QHBoxLayout()
        fw.setContentsMargins(16, 0, 16, 10)
        fw.setSpacing(6)
        fw.addWidget(self.filter)
        self.filter_help = HelpButton("filters")
        fw.addWidget(self.filter_help)
        fw.addStretch(1)
        ll.addLayout(fw)
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["序号", "考号", "警告"])
        self.table = QTableView()
        self.table.setModel(self.model)
        setup_table(self.table, stretch=1)
        self.table.setColumnWidth(0, 76)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        set_empty_text(self.table, TABLE_NO_BATCH)
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _p: self._select_row(cur.row()))
        ll.addWidget(self.table, 1)
        return left

    def _build_center(self) -> QWidget:
        center = QWidget()
        center.setObjectName("work")
        grid = QGridLayout(center)
        grid.setContentsMargins(0, 0, 0, 0)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(20, 0, 20, 0)
        cl.setSpacing(0)
        stu = QHBoxLayout()
        stu.setContentsMargins(0, 12, 0, 8)
        stu.setSpacing(12)
        self.title = QLabel("未选择批次")
        self.title.setObjectName("h2")
        stu.addWidget(self.title)
        self.info = ElidedLabel("", Qt.TextElideMode.ElideRight)
        theme.set_style(self.info, "color:$INK2; font-size:13px;")
        stu.addWidget(self.info, 1)
        cl.addLayout(stu)
        cardw = QWidget()
        cw = QVBoxLayout(cardw)
        cw.setContentsMargins(0, 12, 0, 0)
        cw.setSpacing(10)
        self.card = CardView(placeholder="在左侧选择学生查看卡面")
        cw.addWidget(self.card, 1)
        self.anomalies = QLabel("")
        self.anomalies.setWordWrap(True)
        self.anomalies.setTextFormat(Qt.TextFormat.RichText)
        self.anomalies.setStyleSheet("font-size:13px;")
        self.anomalies.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        # 警告很多（几十条）时只占约 5 行高，其余滚动查看，卡面始终占主要高度
        self.anomaly_scroll = QScrollArea()
        self.anomaly_scroll.setObjectName("anomalyScroll")
        self.anomaly_scroll.setWidgetResizable(True)
        self.anomaly_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.anomaly_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.anomaly_scroll.setWidget(self.anomalies)
        self.anomalies.setAutoFillBackground(False)  # setWidget 会打开自动填充，露出窗口底色
        self.anomaly_scroll.viewport().setAutoFillBackground(False)
        self.anomaly_scroll.setStyleSheet("#anomalyScroll { background: transparent; }")
        self.anomaly_scroll.setToolTip("这份答卷的警告；条目多时滚动查看全部")
        cw.addWidget(self.anomaly_scroll)
        self.anomalies.installEventFilter(self)  # 文字变化后按内容高度调整（不超过约 5 行）
        self.card_tabs = QTabWidget()
        self.card_tabs.setObjectName("cardTabs")
        self.card_tabs.setDocumentMode(True)
        theme.set_style(self.card_tabs,
                        "#cardTabs::pane { border:none; border-top:1px solid $LINE; }"
                        "#cardTabs > QTabBar::tab { background:transparent; border:none; color:$INK2;"
                        " padding:6px 14px; border-bottom:2px solid transparent; }"
                        "#cardTabs > QTabBar::tab:selected { color:$INK; border-bottom:2px solid $ACCENT; }")
        self.card_tabs.addTab(cardw, "卡面")
        self.evidence = EvidencePreview(empty_text="在左侧选择学生后，这里显示整页图、各题切图和完整的识别明细")
        self.card_tabs.addTab(self.evidence, "切图与明细")
        self.card_tabs.setTabToolTip(0, "整页卡面和识别标记（彩色框）")
        self.card_tabs.setTabToolTip(1, "逐张看切图（考号、各题）和这份答卷的完整识别明细（原始数据）")
        self.legend = OverlayLegend([(OVERLAY_COLOR["answer"], "已涂选项", "已涂"),
                                     (OVERLAY_COLOR["exam"], "考号等填涂项", "考号等"),
                                     (OVERLAY_COLOR["choice"], "选做题号", "选做"),
                                     (OVERLAY_COLOR["bad"], "要核对的题", "要核对")])
        self.legend.setContentsMargins(0, 0, 4, 6)
        self.card_tabs.setCornerWidget(self.legend, Qt.Corner.TopRightCorner)
        self.card_tabs.tabBar().setUsesScrollButtons(False)
        self._center = center
        center.installEventFilter(self)  # 中栏变窄时图例改用短说明，免得挤掉标签页
        cl.addWidget(self.card_tabs, 1)
        bar = self._build_float_bar()
        self.float_bar = bar
        cl.addSpacing(bar.sizeHint().height())  # 悬浮条下面不压住异常说明
        grid.addWidget(content, 0, 0)
        grid.addWidget(bar, 0, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        return center

    def _build_float_bar(self) -> QWidget:
        """悬浮在卡面下方的看卡条：上一份 / 第 k / n 份 / 下一份、正面 / 背面。"""
        holder = QWidget()  # 透明外框，只用来留出离底边的距离
        hl = QVBoxLayout(holder)
        hl.setContentsMargins(0, 8, 0, 14)
        self.float_bar = QWidget()
        self.float_bar.setObjectName("floatBar")
        self.float_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(self.float_bar, "#floatBar { background:$WORK; border:1px solid $LINE2; border-radius:8px; }")
        hl.addWidget(self.float_bar)
        row = QHBoxLayout(self.float_bar)
        row.setContentsMargins(8, 5, 8, 5)
        row.setSpacing(6)
        self.prev_btn = QPushButton("上一份")
        self.next_btn = QPushButton("下一份")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(1))
        self._nav_keys = {}
        for btn, key, d in ((self.prev_btn, Qt.Key.Key_PageUp, -1), (self.next_btn, Qt.Key.Key_PageDown, 1)):
            self._nav_keys[btn] = QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda d=d: self._page_key(d))
        self.pos_label = QLabel("—")
        self.pos_label.setMinimumWidth(92)
        self.pos_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        theme.set_style(self.pos_label, "color:$INK2; font-size:13px;")
        row.addWidget(self.prev_btn)
        row.addWidget(self.pos_label)
        row.addWidget(self.next_btn)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        theme.set_style(sep, "color:$LINE;")
        row.addWidget(sep)
        self.face_seg = Segmented([("front", "正面"), ("back", "背面")], "front")
        self.face_seg.changed.connect(self._set_face)
        self.face_seg.buttons["front"].setToolTip("看正面扫描图和识别标记")
        self.face_seg.buttons["back"].setToolTip("看背面扫描图和识别标记")
        row.addWidget(self.face_seg)
        return holder

    def _build_side(self) -> QWidget:
        side = QWidget()
        side.setObjectName("panel")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        oh = QHBoxLayout()
        oh.setContentsMargins(16, 10, 16, 10)
        t = QLabel("客观题答案")
        t.setStyleSheet("font-weight:600;")
        oh.addWidget(t)
        oh.addWidget(HelpButton("fill"))  # “涂黑程度”列的数字是什么意思
        oh.addStretch(1)
        self.answers_note = QLabel("")
        self.answers_note.setObjectName("muted")
        oh.addWidget(self.answers_note)
        sl.addLayout(oh)
        self.answers = QTableWidget(0, 4)
        self.answers.setHorizontalHeaderLabels(["题号", "答案", "状态", "涂黑程度"])
        self.answers.horizontalHeaderItem(3).setToolTip(FILL_TIP)
        setup_table(self.answers, stretch=3, row_height=30)
        for column, width in enumerate((68, 80, 64)):
            self.answers.setColumnWidth(column, width)
        self._answers_empty = set_empty_text(self.answers, "")
        sl.addWidget(self.answers, 1)
        shw = QWidget()
        shw.setObjectName("subjHead")
        theme.set_style(shw, "#subjHead { border-top:1px solid $LINE; }")
        shl = QHBoxLayout(shw)
        shl.setContentsMargins(16, 10, 16, 10)
        shl.setSpacing(6)
        sh = QLabel("信息栏与主观题")
        sh.setStyleSheet("font-weight:600;")
        shl.addWidget(sh)
        shl.addWidget(HelpButton("ocr"))
        shl.addStretch(1)
        sl.addWidget(shw)
        sl.addWidget(self._build_batch_ocr())
        self.subj_box = QVBoxLayout()
        self.subj_box.setContentsMargins(16, 0, 16, 12)
        holder = QWidget()
        holder.setLayout(self.subj_box)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(holder)
        holder.setAutoFillBackground(False)  # setWidget 会打开自动填充，露出窗口底色
        sl.addWidget(sa, 1)
        return side

    def _build_batch_ocr(self) -> QWidget:
        """核对模式右栏“信息栏与主观题”下的整批转写：失败 / 等待项数 + 转写剩余内容、不再转写剩余项…。
        交付清单里转写类阻断项的处理按钮也代点这两个按钮（见 _fix_buttons）。"""
        box = QWidget()
        box.setObjectName("batchOcr")
        box.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(box, "#batchOcr { border-bottom:1px solid $LINE; }")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(16, 0, 16, 10)
        lay.setSpacing(6)
        line = QHBoxLayout()
        line.setSpacing(8)
        lab = QLabel("整批转写")
        theme.set_style(lab, "color:$INK2; font-size:13px;")
        line.addWidget(lab)
        line.addStretch(1)
        self.batch_ocr_stats = QLabel("—")
        self.batch_ocr_stats.setObjectName("muted")
        self.batch_ocr_stats.setToolTip("这一批转写失败和等待转写的项数（与“交付”右栏的转写统计相同）")
        line.addWidget(self.batch_ocr_stats)
        lay.addLayout(line)
        ops = QHBoxLayout()
        ops.setSpacing(8)
        self.retry_all_btn = QPushButton("转写剩余内容")
        self.retry_all_btn.setEnabled(False)
        self.retry_all_btn.clicked.connect(self._retry_all_failed)
        self.skip_btn = QPushButton("不再转写剩余项…")
        self.skip_btn.setEnabled(False)
        self.skip_btn.clicked.connect(self._skip_pending)
        ops.addWidget(self.retry_all_btn)
        ops.addWidget(self.skip_btn)
        ops.addStretch(1)
        lay.addLayout(ops)
        self.batch_ocr = box
        return box

    def _build_deliver(self) -> QWidget:
        """交付模式：左边整页检查清单（先检查、再生成），右栏批次与转写统计（整批转写按钮在核对模式右栏）。"""
        view = QWidget()
        view.setObjectName("work")
        root = QHBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        flow = QWidget()
        self._delivery_band = QVBoxLayout(flow)
        self._delivery_band.setContentsMargins(32, 20, 32, 20)
        self.delivery_panel = None
        self._attach_delivery_panel()
        self.flow_scroll = QScrollArea()
        self.flow_scroll.setWidgetResizable(True)
        self.flow_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.flow_scroll.setWidget(flow)
        flow.setAutoFillBackground(False)
        root.addWidget(self.flow_scroll, 1)
        self.ocr_help = HelpButton("ocr")
        self.summary = BatchSummary(self.ocr_help, [],
                                    "整批“转写剩余内容 / 不再转写剩余项…”在“核对答卷”右栏的“信息栏与主观题”里；"
                                    "单项重试也在那里。“必须先处理”清单里转写类问题旁的按钮也能直接处理")
        self.summary_note = self.summary.note
        self.summary.setFixedWidth(360)
        root.addWidget(self.summary)
        return view

    def mode(self) -> str:
        return "deliver" if self.mode_stack.currentWidget() is self.deliver_view else "check"

    def set_mode(self, mode: str) -> None:
        """切换“核对答卷 / 交付”模式。"""
        self.mode_switch.set_value(mode)
        self.mode_stack.setCurrentWidget(self.deliver_view if mode == "deliver" else self.check_view)

    def show_student(self, seq: int) -> None:
        """切到核对模式并选中这一份；当前筛选或搜索里看不到它时先清除筛选和搜索。"""
        self.set_mode("check")
        if self.select_seq(seq):
            return
        self.filter.set_value("all")
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._refill()
        if not self.select_seq(seq):
            self._notify(f"#{seq:04d} 不在这一批的列表里，点“刷新”后再试")

    ANOMALY_MAX_LINES = 5

    def _fit_anomalies(self) -> None:
        """警告区高度跟内容走，最多约 ANOMALY_MAX_LINES 行，超出的滚动查看。"""
        lab = self.anomalies
        width = max(1, self.anomaly_scroll.viewport().width())
        need = lab.heightForWidth(width) if lab.text() else 0
        line = lab.fontMetrics().lineSpacing() + 2
        cap = line * self.ANOMALY_MAX_LINES
        self.anomaly_scroll.setFixedHeight(max(0, min(need, cap)))
        self.anomaly_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if need > cap else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def eventFilter(self, obj, event) -> bool:
        if obj is getattr(self, "anomalies", None):
            if event.type() in (QEvent.Type.Resize, QEvent.Type.FontChange, QEvent.Type.StyleChange):
                self._fit_anomalies()
            return False
        if event.type() == QEvent.Type.Resize and obj is self._center:
            self.legend.set_compact(obj.width() < LEGEND_FULL_WIDTH)
            return False
        if (event.type() == QEvent.Type.KeyPress and event.modifiers() == Qt.KeyboardModifier.NoModifier
                and event.key() in (Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)):
            self._step(1 if event.key() == Qt.Key.Key_PageDown else -1)
            return True
        return super().eventFilter(obj, event)

    # ---------- 数据 ----------
    def _attach_delivery_panel(self) -> None:
        if self.delivery_panel is not None:
            return
        from ..widgets.delivery_panel import DeliveryPanel
        self.delivery_panel = DeliveryPanel(self)
        self.delivery_panel.active_batch_fn = lambda: self._active_batch_fn() if self._active_batch_fn else None
        self.delivery_panel.protected_dirs_fn = self._protected_dirs
        notice = getattr(self.delivery_panel, "notice", None)
        if notice is not None:
            notice.connect(self._notify)
        finished = getattr(self.delivery_panel, "finished", None)
        if finished is not None:
            finished.connect(lambda: self._show_hint(force=True))
        for name, slot in (("changed", self._update_mode_badge), ("showStudent", self.show_student)):
            signal = getattr(self.delivery_panel, name, None)
            if signal is not None:
                signal.connect(slot)
        self.delivery_panel.fix_actions_fn = self._fix_buttons
        self._delivery_band.addWidget(self.delivery_panel)
        self._update_mode_badge()

    def _update_mode_badge(self) -> None:
        """模式切换里“交付”的徽标和顶栏“上次检查”：都来自交付检查的实际结论，没检查过就写“还没检查”。"""
        panel = self.delivery_panel
        if panel is None or not hasattr(panel, "badge"):
            return
        self.mode_switch.set_badge(*panel.badge())
        report = panel.last_report
        if report is None:
            self.last_check.setText("")
            self.last_check.setToolTip("")
            return
        when = str(report.checked_at)
        short = when[11:16] if len(when) >= 16 and when[10] == "T" else when
        self.last_check.setText(f"上次检查 {short}" + ("（数据已变，已过期）" if panel.stale else ""))
        self.last_check.setToolTip(f"检查时间：{when}")

    # 阻断项的处理按钮：只用结果页上已有的整批转写动作，状态与说明跟右栏按钮保持一致
    _FIX_ACTIONS = {"ocr_pending": ("retry", "skip"), "ocr_status_invalid": ("retry",)}

    def _fix_buttons(self, code: str) -> list[QPushButton]:
        out = []
        for kind in self._FIX_ACTIONS.get(code, ()):
            source = self.retry_all_btn if kind == "retry" else self.skip_btn
            button = QPushButton(source.text())
            button.setObjectName(f"fix_{kind}")
            button.clicked.connect(source.click)
            out.append(button)
        self._sync_fix_buttons(out)
        return out

    def _sync_fix_buttons(self, buttons: list[QPushButton] | None = None) -> None:
        if buttons is None:
            issues = getattr(self.delivery_panel, "issues", None)
            if issues is None:
                return
            buttons = [b for b in issues.findChildren(QPushButton)
                       if b.objectName() in ("fix_retry", "fix_skip")]
        for button in buttons:
            source = self.retry_all_btn if button.objectName() == "fix_retry" else self.skip_btn
            button.setEnabled(source.isEnabled())
            button.setToolTip(source.toolTip())

    def _update_summary(self) -> None:
        """交付模式右栏的批次构成与转写统计（只用已读到的行数据；转写状态还在统计时不显示假数）。"""
        put = self.summary.set_value
        if self.batch_dir is None:
            for key in self.summary.values:
                put(key, None, "")
            self.batch_ocr_stats.setText("—")
            return
        rows = self.rows
        warn = sum(1 for r in rows if r.status == "ok" and (r.warnings or r.ocr_failed or r.ocr_pending
                                                             or r.duplicate_of))
        put("total", len(rows), "份")
        put("ok", sum(1 for r in rows if r.status == "ok") - warn, "份")
        put("warn", warn, "份")
        put("bad", sum(1 for r in rows if r.status in ("physical", "voided", "broken")), "份")
        for key, attr in (("ocr_failed", "ocr_failed"), ("ocr_pending", "ocr_pending")):
            put(key, None if self._counts_pending else sum(getattr(r, attr) for r in rows), "项", "统计中…")
        if self._counts_pending:
            self.batch_ocr_stats.setText("统计中…")
        else:
            failed, pending = (sum(getattr(r, attr) for r in rows) for attr in ("ocr_failed", "ocr_pending"))
            self.batch_ocr_stats.setText(f"失败 {failed} · 等待 {pending}")

    def _protected_dirs(self) -> list[tuple[Path, str]]:
        """不允许放交付目录的位置：设置中的输出根目录（考试页按目录扫描批次）。"""
        root = getattr(self.settings, "output_root", None) if self.settings else None
        return [(Path(root), "输出根目录")] if root else []

    def _notify(self, text: str) -> None:
        """在页内提示行显示操作结果，并转发给主窗口状态栏；空串表示改回显示“下一步”提示。"""
        if not text:
            self._show_hint(force=True)
            return
        self._notice_is_hint = False
        self.notice.setText(text)
        self.notice.setVisible(True)
        self._sync_hint_bar()
        self.statusMessage.emit(text)

    def _sync_hint_bar(self) -> None:
        """提示条跟随提示行：有内容才显示；显示下一步提示时标“下一步”，显示操作结果时标“操作结果”。"""
        self.hint_head.setText("下一步" if self._notice_is_hint else "操作结果")
        self.hint_bar.setVisible(not self.notice.isHidden())

    def _show_hint(self, force: bool = False) -> None:
        """提示行显示“下一步”提示。正在显示操作结果时不覆盖，除非 force（例如检查交付刚结束）。"""
        if not force and not self._notice_is_hint and self.notice.text():
            return
        hint = self._context_hint()
        self._notice_is_hint = True
        self.notice.setText(hint)
        self.notice.setVisible(bool(hint))
        self._sync_hint_bar()

    def _read_manifest(self) -> dict | None:
        try:
            m = json.loads((self.batch_dir / "manifest.json").read_text(encoding="utf-8"))
            return m if isinstance(m, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _is_active(self) -> bool:
        active = self._active_batch_fn() if self._active_batch_fn else None
        try:
            return bool(active) and self.batch_dir is not None and Path(active).resolve() == self.batch_dir.resolve()
        except (OSError, ValueError):
            return False

    def _context_hint(self) -> str:
        """按批次状态告诉用户下一步做什么（只读状态，不弹窗）。"""
        if self.batch_dir is None:
            return ""
        manifest = self._read_manifest()
        if manifest is not None and (manifest.get("delivery") or {}).get("sealed"):
            return "这是已生成的交付目录（只读）：只能查看和检查，要修改请回原工作批次。"
        if self._is_active():
            return "这一批还在扫描中：到扫描页点“停止导入剩余文件 / 停止进纸”或“结束批次”，停下后才能转写和交付。"
        if not self.rows:
            return "这个批次还没有答卷：先到扫描页导入或扫描。"
        panel = self.delivery_panel
        if getattr(panel, "busy", False):
            return ""
        last_report = getattr(panel, "last_report", None)
        if last_report is not None:
            if getattr(panel, "stale", False):
                return "数据变了：在“交付”里重新点“检查交付”，确认这一批还能不能交付。"
            if last_report.ready:
                return "检查通过：在“交付”里点“生成交付目录…”输出最终结果。"
            first = getattr(panel, "first_blocker", None)
            blocker = first() if first else None
            if blocker:
                label, guide = blocker
                return f"还不能交付，先处理“{label}”：{guide}。处理完在“交付”里重新点“检查交付”。"
        if getattr(panel, "last_export", None) is not None:
            return "交付目录已生成。数据有改动时，重新检查后再生成一份。"
        if manifest is not None and manifest.get("finalized") is not True:
            return ("这一批还没结束：到扫描页点“结束批次”后才能交付"
                    "（扫描页没打开这一批时，先在考试页选中它点“继续扫描”）。")
        bad = sum(1 for r in self.rows if r.status in ("physical", "broken"))
        if bad:
            return f"有 {bad} 份异常卡：在“核对答卷”里点“异常卡”筛选查看，回扫描页重扫或作废；回到结果页会自动更新。"
        if self._counts_pending:
            return COUNTS_PENDING
        pending = sum(r.ocr_pending for r in self.rows)
        if pending:
            return (f"有 {pending} 项等待转写，没完成不能交付：可在“核对答卷”里点“转写剩余内容”，"
                    "不需要文字就点“不再转写剩余项…”。")
        return "核对完后在“交付”里点“检查交付”，看这一批能不能交付。"

    def _mark_delivery_stale(self) -> None:
        mark = getattr(self.delivery_panel, "mark_stale", None)
        if mark is not None:
            mark()

    @staticmethod
    def _stamps(sig: object) -> object:
        """指纹中只取文件的修改时间与大小部分（不含行数据）。"""
        manifest, rows = sig
        return manifest, [stamp for _row, stamp in rows]

    def _signature(self, rows: list[StudentRow], batch_dir: Path | None = None) -> object:
        """manifest 与各 result.json 的修改时间 + 行数据：任何一项变化都说明批次数据变了。"""
        batch_dir = batch_dir or self.batch_dir
        def stamp(path: Path | None):
            try:
                st = path.stat() if path is not None else None
                return (st.st_mtime_ns, st.st_size) if st else None
            except OSError:
                return None
        return (stamp(batch_dir / "manifest.json") if batch_dir else None,
                [(r, stamp(r.result)) for r in rows])

    def open_batch(self, batch_dir: str | Path, select_seq: int | None = None) -> bool:
        bd = Path(batch_dir)
        try:
            tpl = load_batch_template(bd)
            rows = load_rows(bd, ocr=False)  # 先只读 manifest，列表立即可用；转写状态在后台补上
        except Exception as e:  # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "提示", f"无法打开批次：{type(e).__name__}: {e}")
            return False
        self._rows_loader.cancel()
        self.batch_dir = bd
        self.tpl = tpl
        self.rows = rows
        self._data_sig = None
        self._open_stamps = self._stamps(self._signature(rows))
        self._counts_pending = True
        self.set_mode("check")  # 打开批次（含点拼图格子指定某一份）一律从核对答卷开始，选中的那份卡面看得见
        self._show_batch_title()
        self._notify("")
        has_back = "back" in self.tpl.faces
        self.face_seg.buttons["back"].setEnabled(has_back)
        self.face_seg.buttons["back"].setToolTip("看背面扫描图和识别标记" if has_back
                                                 else "这个模板只有正面，没有背面可看")
        if self.face not in self.tpl.faces:
            self.face = "front"
            self.face_seg.set_value("front")
        self.current = self.result = None
        self._update_retry_controls()
        self._attach_delivery_panel()
        if self.delivery_panel is not None:
            self.delivery_panel.set_batch(bd)
        if select_seq is not None:
            # 指定了序号（点拼图格子）：清除筛选和搜索，保证该序号可见
            self.filter.set_value("all")
            self.search.blockSignals(True)
            self.search.clear()
            self.search.blockSignals(False)
        self._refill()
        if select_seq is None or not self.select_seq(select_seq):
            self._clear_detail()
        self._show_hint(force=True)
        self.batchChanged.emit(str(self.batch_dir))
        self._reload_later(None)
        return True

    def _show_batch_title(self) -> None:
        """顶栏：批次目录名；下面一行是考试名称（manifest 里有时）和份数。"""
        if self.batch_dir is None:
            title, sub = "未打开批次", ""
        else:
            exam = (self._read_manifest() or {}).get("exam")
            exam = exam if isinstance(exam, dict) else {}
            name = " ".join(str(exam[k]) for k in ("name", "subject") if exam.get(k))
            title, sub = self.batch_dir.name, " · ".join(x for x in (name, f"{len(self.rows)} 份") if x)
        widest = 0
        for label, text in ((self.batch_title, title), (self.batch_sub, sub)):
            label.ensurePolished()  # 按样式表里的字号算宽度
            label.setText(text)
            widest = max(widest, label.sizeHint().width())
        self.batch_crumb.setMaximumWidth(widest)  # 刷新按钮紧跟在批次名后面；放不下时才省略

    def visible_rows(self) -> list[StudentRow]:
        f = self.filter.value()
        q = self.search.text().strip()
        out = []
        for r in self.rows:
            if f == "warn" and not (r.status == "ok" and (r.warnings or r.ocr_failed or r.ocr_pending
                                                          or r.duplicate_of)):
                continue
            if f == "err" and r.status not in ("physical", "voided", "broken"):
                continue
            if q and q not in f"{r.seq:04d}" and q not in (r.exam_no or "") and q not in r.source:
                continue
            out.append(r)
        return out

    def _refill(self) -> None:
        self._clear_detail()
        self.model.removeRows(0, self.model.rowCount())
        rows = self.visible_rows()
        for r in rows:
            self.model.appendRow(self._row_items(r))
        self.count_label.setText(f"{len(rows)} / {len(self.rows)} 份")
        set_empty_text(self.table, TABLE_NO_BATCH if self.batch_dir is None
                       else TABLE_FILTERED_EMPTY if self.rows else TABLE_NO_ROWS)
        self._update_nav()

    @staticmethod
    def _row_items(r: StudentRow) -> list[QStandardItem]:
        """列表一行的三列（新建行和原地更新共用）。"""
        reason = PHYSICAL_TEXT.get(r.physical or "", r.physical or "")
        exam = r.exam_no or {"physical": f"异常卡 {reason}".strip(), "voided": "已作废",
                             "broken": "结果不可用"}.get(r.status, "—")
        items = [QStandardItem(f"#{r.seq:04d}"), QStandardItem(exam), QStandardItem(ResultsPage._row_warning(r))]
        if r.status != "ok":
            for it in items:
                theme.set_item_colors(it, foreground="ERR" if r.status in ("physical", "broken") else "INK3")
        elif r.duplicate_of:
            theme.set_item_colors(items[1], foreground="ERR")
            theme.set_item_colors(items[2], foreground="ERR")
        elif r.warnings or r.ocr_failed or r.ocr_pending:
            theme.set_item_colors(items[2], foreground="WARN")
        items[0].setData(r.seq, Qt.ItemDataRole.UserRole)
        return items

    @staticmethod
    def _row_warning(row: StudentRow) -> str:
        if row.status != "ok":
            return ""
        parts = [str(row.warnings)] if row.warnings else []
        if row.duplicate_of:
            parts.insert(0, "考号重复")
        if row.ocr_failed:
            parts.append(f"转写失败 {row.ocr_failed}")
        if row.ocr_pending:
            parts.append(f"待转写 {row.ocr_pending}")
        return " / ".join(parts)

    # ---------- 刷新（主窗口切到结果页、设置或扫描状态变化时调用） ----------
    def refresh(self, announce: bool = False) -> None:
        """重新读取当前批次；保持筛选、搜索和选中的学生。没有打开批次时什么也不做。"""
        if self.batch_dir is None:
            return
        before = None if self._notice_is_hint else self.notice.text()

        def applied(_changed: bool) -> None:
            self._update_retry_controls()
            if announce and (self._notice_is_hint or self.notice.text() == before):
                self._notify("已刷新")

        self._reload_later(None, force=announce, then=applied)

    def on_settings_changed(self) -> None:
        """设置保存后（例如配置了大模型转写）重新计算重试按钮。"""
        self._update_buttons()

    def on_scan_state_changed(self) -> None:
        """扫描开始/结束后重新计算按钮（正在扫描的批次不能重试转写、不能交付）。"""
        update = getattr(self.delivery_panel, "update_controls", None)
        if update is not None:
            update()
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._update_retry_controls()
        if self.result is not None:
            self._fill_subjective(self.result)  # 单项转写按钮的可用状态

    def _reload_later(self, changed: Path | None, force: bool = False,
                      then: Callable[[bool], None] | None = None) -> None:
        """在后台重新读取行数据（每份 result.json 都要读，份数多时较慢），读完在界面线程更新；只认最后一次。"""
        batch = self.batch_dir
        if batch is None:
            return

        def work() -> tuple[list[StudentRow], object]:
            rows = load_rows(batch)
            return rows, self._signature(rows, batch)

        def done(loaded: tuple[list[StudentRow], object]) -> None:
            if self.batch_dir != batch:
                return
            was_pending, self._counts_pending = self._counts_pending, False
            ok = self._apply_rows(*loaded, changed, force)
            if was_pending:
                self._update_retry_controls()  # 转写状态统计好了：按钮与提示按实际重算
            if then is not None:
                then(ok)

        def failed(e: BaseException) -> None:
            if self.batch_dir == batch:
                self._counts_pending = False
                self._update_retry_controls()
                self._notify(f"无法重新读取批次：{e}")

        self._rows_loader.submit(work, done, failed)

    def _reload(self, changed: Path | None, force: bool = False) -> bool:
        """立即重新读取行数据（用于操作前需要最新数据的场合）；被动刷新用 _reload_later。"""
        self._rows_loader.cancel()  # 后台还没回来的旧读取作废，免得晚到的结果盖掉这次
        try:
            rows = load_rows(self.batch_dir)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._notify(f"无法重新读取批次：{e}")
            if self._counts_pending:
                self._reload_later(None)  # 打开批次时的统计被上面作废了：重新在后台统计，免得计数停在 0
            return False
        self._counts_pending = False
        return self._apply_rows(rows, self._signature(rows), changed, force)

    def _apply_rows(self, rows: list[StudentRow], sig: object, changed: Path | None, force: bool = False) -> bool:
        """用新读到的行数据更新界面。changed 为某份 result.json 时只在它是当前学生时重画右侧；None 表示整批。

        数据没变且不是强制刷新时不重画；当前学生不再符合筛选时选中相邻一份并给出提示，而不是清空。"""
        # 打开批次后第一次完整读取：补上转写状态。文件与打开时相同就不算数据变化（交付结论不过期）
        initial = self._data_sig is None
        unchanged_since_open = initial and self._stamps(sig) == self._open_stamps
        if not initial and sig == self._data_sig and not force and changed is None:
            return False
        if sig != self._data_sig:
            self._data_sig = sig
            if not unchanged_since_open:
                self._mark_delivery_stale()
        selected = self.current.seq if self.current else None
        before = [r.seq for r in self.visible_rows()]
        self.rows = rows
        self._show_batch_title()
        self._update_summary()
        after = [r.seq for r in self.visible_rows()]
        if before != after:
            self._refill()
            if selected is not None and not self.select_seq(selected):
                if after:
                    old = before.index(selected) if selected in before else 0
                    neighbor = after[min(old, len(after) - 1)]
                    self.select_seq(neighbor)
                    self._notify(f"#{selected:04d} 已不在“{FILTER_TEXT.get(self.filter.value(), '')}”列表中"
                                 f"（数据已更新），现在显示 #{neighbor:04d}")
                else:
                    self._notify(f"#{selected:04d} 已不在“{FILTER_TEXT.get(self.filter.value(), '')}”列表中"
                                 "（数据已更新），列表已空")
            return True
        by_seq = self._update_rows_in_place(rows)
        if unchanged_since_open and changed is None:
            # 右侧当前学生的转写状态已在选中时按其 result.json 算好，不重画（免得图片重新加载）
            self._update_retry_controls()
            return True
        if selected is None:
            return True
        self.current = by_seq.get(selected, self.current)
        if changed is None or (self.current.result and self.current.result.resolve() == changed):
            i = self.table.currentIndex().row()
            if i >= 0:
                self._select_row(i)
        elif self.result is not None:
            self._fill_subjective(self.result)
        return True

    def _update_rows_in_place(self, rows: list[StudentRow]) -> dict[int, StudentRow]:
        """原地更新列表各行的文字与颜色（不重建、不改变选中），返回 序号 → 行。"""
        by_seq = {r.seq: r for r in rows}
        for i in range(self.model.rowCount()):
            row = by_seq.get(self.model.item(i, 0).data(Qt.ItemDataRole.UserRole))
            if row is not None:
                for j, item in enumerate(self._row_items(row)):
                    self.model.setItem(i, j, item)
        if self.current is not None:
            self.current = by_seq.get(self.current.seq, self.current)
        return by_seq

    def select_seq(self, seq: int) -> bool:
        for i in range(self.model.rowCount()):
            if self.model.item(i, 0).data(Qt.ItemDataRole.UserRole) == seq:
                self.table.selectionModel().blockSignals(True)
                self.table.selectRow(i)
                self.table.selectionModel().blockSignals(False)
                self._select_row(i)
                return True
        return False

    def _page_key(self, d: int) -> None:
        """PgUp/PgDn 切换上一份/下一份（焦点在列表、卡面、答案表等任何位置都一样）。

        只有焦点在能滚动的长文本（JSON）里时才用于文本翻页，便于阅读；交付模式下翻的是交付清单。"""
        if self.mode() == "deliver":
            step = QAbstractSlider.SliderAction
            self.flow_scroll.verticalScrollBar().triggerAction(
                step.SliderPageStepAdd if d > 0 else step.SliderPageStepSub)
            return
        w = QApplication.focusWidget()
        if isinstance(w, (QPlainTextEdit, QTextEdit)) and self.isAncestorOf(w) and w.verticalScrollBar().maximum() > 0:
            w.verticalScrollBar().triggerAction(QAbstractSlider.SliderAction.SliderPageStepAdd if d > 0
                                                else QAbstractSlider.SliderAction.SliderPageStepSub)
            return
        self._step(d)

    def _step(self, d: int) -> None:
        i = self.table.currentIndex().row() + d
        if 0 <= i < self.model.rowCount():
            self.table.selectRow(i)

    def _update_nav(self) -> None:
        """上一份 / 下一份：到头时变灰，并说明原因。"""
        n, i = self.model.rowCount(), self.table.currentIndex().row()
        common = NO_BATCH_TIP if self.batch_dir is None else "列表里没有学生（换个筛选或清空搜索）" if n == 0 else ""
        prev = common or ("先在左侧选择学生" if i < 0 else "已经是列表里的第一份" if i == 0 else "")
        nxt = common or ("已经是列表里的最后一份" if i >= n - 1 and i >= 0 else "")
        self.pos_label.setText("—" if self.batch_dir is None else f"未选 / {n} 份" if i < 0
                               else f"第 {i + 1} / {n} 份")
        for btn, reason, text in ((self.prev_btn, prev, "看列表里的上一份"),
                                  (self.next_btn, nxt, "看列表里的下一份" if i >= 0 else "从列表第一份开始看")):
            btn.setEnabled(not reason)
            btn.setToolTip(reason or f"{text}（{self._nav_keys[btn]}）")

    def _select_row(self, i: int) -> None:
        if i < 0 or i >= self.model.rowCount():
            self._clear_detail()
            return
        seq = self.model.item(i, 0).data(Qt.ItemDataRole.UserRole)
        row = next(r for r in self.rows if r.seq == seq)
        self.current = row
        self._update_nav()
        self.result = None
        broken = False
        if row.result:
            try:
                root = self.batch_dir.resolve()
                path = resolve_result_path(root, row.result.relative_to(root).as_posix(), row.seq)
                r = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(r, dict):
                    raise ValueError("result.json 顶层不是对象")
                self.result = r
                try:  # 列表的转写状态可能还在后台统计：当前学生按刚读到的内容算，右侧提示立即正确
                    row.ocr_failed, row.ocr_pending = ocr_counts_in(r)
                except (AttributeError, TypeError):
                    pass
            except (OSError, ValueError):
                broken = True
        try:
            self._show(broken)
        except Exception:  # noqa: BLE001 — 结构不对（缺键、类型错）：按损坏处理，不残留上一个学生
            self.result = None
            self._show(True)

    def _clear_detail(self) -> None:
        """清空右侧详情（没有选中学生时）。"""
        self.current = self.result = None
        self.title.setText("未选择批次" if self.batch_dir is None else "未选择学生")
        self.info.setText("")
        self.card.clear()
        self.evidence.set_result(None)
        self.answers.setRowCount(0)
        self.answers_note.setText("")
        self._answers_empty.setText("还没打开批次" if self.batch_dir is None
                                    else "在左侧选择学生，这里显示客观题答案和涂黑程度")
        self._warning_texts = None
        self.anomalies.setText("")
        self._fit_anomalies()
        self._fill_subjective(None)
        self._update_nav()

    def _set_face(self, face: str) -> None:
        self.face = face
        self._show_card()

    # ---------- 显示 ----------
    def _show(self, broken: bool = False) -> None:
        row, r = self.current, self.result
        self.evidence.set_result(None)
        self._update_retry_controls()
        self.title.setText(f"#{row.seq:04d}")
        if r is None:
            reason = PHYSICAL_TEXT.get(row.physical or "", row.physical or "")
            self.info.setText("结果文件损坏" if broken else {"physical": f"异常卡：{reason}　{row.message}",
                                                         "voided": "已作废",
                                                         "broken": "结果文件不可用"}.get(row.status, "识别中"))
            self.card.clear()
            try:
                root = self.batch_dir.resolve()
                raw_dir = resolve_path(root, root, "raw")
                raws = sorted(raw_dir.glob(f"{row.seq:04d}_*"))
                if raws:
                    image = resolve_path(root, root, raws[0].relative_to(root).as_posix())
                    self.card.load_image(image)
            except (ValueError, OSError, RuntimeError):
                self.card.setToolTip("图片路径无效")
            self.answers.setRowCount(0)
            self.answers_note.setText("")
            self._answers_empty.setText(
                "结果文件损坏，读不到识别结果。\n请从备份恢复该答卷目录；“检查交付”会列出具体文件。" if broken else
                {"physical": "异常卡，没有识别结果。\n请在扫描页重扫或作废。",
                 "voided": "已作废，没有识别结果",
                 "broken": row.message}.get(row.status, "没有识别结果"))
            self._warning_texts = None
            self.anomalies.setText("")
            self._fit_anomalies()
            self._fill_subjective(None)
            return
        st = r["student"]
        self.evidence.set_result(r, base=row.result.parent, root=self.batch_dir, sources=[row.source])
        exam = (st.get("exam_no") or {}).get("value") or "—"
        bc = ((st.get("barcodes") or {}).get("student_barcode") or {}).get("value") or "—"
        absent_value = (st.get("absent") or {}).get("value")
        absent = "是" if absent_value is True else "否" if absent_value is False else "未确定"
        name = ((st.get("fields") or {}).get("name") or {}).get("text") or ""
        extra = "".join(f"　{f.get('label') or k} {f.get('value') or '—'}"
                        for k, f in (st.get("omr_fields") or {}).items() if isinstance(f, dict))
        self.info.setText(f"填涂考号 {exam}　条码 {bc}　缺考 {absent}" + (f"　姓名 {name}" if name else "") + extra)
        self._show_card()
        warnings = [warning_text(w) for w in r.get("warnings", [])]
        if row.duplicate_of:
            others = "、".join(f"#{s:04d}" for s in row.duplicate_of)
            warnings.insert(0, f"考号与 {others} 重复：重复扫描的请在扫描页作废多余的一份；不同考生的请核对原图")
        if row.ocr_failed:
            warnings.append(f"转写失败 {row.ocr_failed} 项")
        if row.ocr_pending:
            warnings.append(f"等待转写 {row.ocr_pending} 项（未完成前不能交付）")
        self._warning_texts = warnings
        self.refresh_theme()
        objs = r.get("objective", [])
        chooses = r.get("choose_one", [])
        self._answers_empty.setText("模板中没有客观题")
        review = sum(1 for o in objs if o.get("status") != "ok") + sum(1 for c in chooses if c.get("status") != "ok")
        self.answers_note.setText(f"{review} 题需核对" if review else "")
        self.answers.setRowCount(len(objs) + len(chooses))
        for i, o in enumerate(objs):
            values = {k: v for k, v in o.get("fill", {}).items() if isinstance(v, (int, float))}
            fill = max(values.values(), default=0)
            cells = [o["no"], o["answer"] or "—", STATUS_TEXT.get(o["status"], o["status"]),
                     fill_text(values, o["answer"]) or (f"{fill:.2f}" if values else "未确定")]
            for j, c in enumerate(cells):
                it = QTableWidgetItem(c)
                it.setToolTip(str(c))
                if o["status"] != "ok":
                    theme.set_item_colors(it, background="ERR_BG", foreground="ERR" if j == 2 else None)
                self.answers.setItem(i, j, it)
        for i, c in enumerate(chooses, start=len(objs)):  # 选做组：每组一行，接在客观题后面
            cells = [f"选做：{c.get('label') or c.get('key', '')}", "、".join(c.get("chosen") or []) or "—",
                     STATUS_TEXT.get(c.get("status"), c.get("status") or ""),
                     fill_text({k: v for k, v in (c.get("fill") or {}).items() if isinstance(v, (int, float))},
                               c.get("chosen") or []) or "未确定"]
            for j, text in enumerate(cells):
                it = QTableWidgetItem(text)
                it.setToolTip(text)
                if c.get("status") != "ok":
                    theme.set_item_colors(it, background="ERR_BG", foreground="ERR" if j == 2 else None)
                self.answers.setItem(i, j, it)
        self._fill_subjective(r)

    def refresh_theme(self) -> None:
        """Only the cached warning markup needs regeneration on a theme change."""
        if self._warning_texts is not None:
            self.anomalies.setText("<br>".join(
                f"<span style='color:{theme.ERR}'>■</span> {text}" for text in self._warning_texts)
                or f"<span style='color:{theme.INK3}'>无警告</span>")
            self._fit_anomalies()

    # ---------- 重试转写 ----------
    def _retry_block_reason(self) -> str:
        if self.batch_dir is None:
            return NO_BATCH_TIP
        if self._retry_owner is not None:
            return "正在转写，请等这次完成"
        try:
            manifest = json.loads((self.batch_dir / "manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("delivery") or {}).get("sealed"):
                return "这是已生成的交付目录（只读），要转写请回原工作批次"
        except (OSError, ValueError, AttributeError):
            return "批次信息无法读取，不能转写"
        if self._is_active():
            return "这一批还在扫描中：到扫描页点“停止导入剩余文件 / 停止进纸”或“结束批次”后再转写"
        return ""

    def _update_retry_controls(self) -> None:
        reason = self._retry_block_reason()
        ocr_reason = reason or ("" if self.settings and self.settings.ocr else NO_OCR_TIP) \
            or ("" if self.rows else "这个批次还没有答卷")
        self.retry_all_btn.setEnabled(not ocr_reason)
        self.retry_all_btn.setToolTip(f"不能转写：{ocr_reason}" if ocr_reason else
                                      "转写全部剩余手写内容：包括失败的、等待中的和当初没开转写的（也包括已放弃转写的）")
        pending = sum(r.ocr_pending for r in self.rows)
        skip_reason = reason or (COUNTS_PENDING if self._counts_pending else
                                 "" if pending else "没有等待转写的项")
        self.skip_btn.setEnabled(not skip_reason)
        self.skip_btn.setToolTip(f"现在不能点：{skip_reason}" if skip_reason else
                                 f"不再转写这 {pending} 项“等待转写”：交付结果里这些题只有切图、没有文字，"
                                 "但不再挡住交付；以后仍可用“转写剩余内容”再转写")
        self.refresh_btn.setEnabled(self.batch_dir is not None)
        self.refresh_btn.setToolTip("重新读取这一批的识别与转写结果。从别的页面回到结果页会自动更新；"
                                    "结果页一直开着、数据又有变化（比如后台转写完成）时再点"
                                    if self.batch_dir is not None else f"不能刷新：{NO_BATCH_TIP}")
        self._sync_fix_buttons()
        self._update_summary()
        self._show_hint()

    def _allow_retry(self) -> bool:
        reason = self._retry_block_reason()
        if reason:
            self._update_retry_controls()
            QMessageBox.information(self, "提示", reason)
            return False
        return True

    def _begin_retry(self, owner: str, text: str) -> None:
        """开始转写：记下任务、按钮变灰、显示开始提示。"""
        self._retry_owner = owner
        self._notify(text)
        self._update_buttons()

    def _retry_single(self, kind: str, key: str, label: str = "") -> None:
        """后台线程执行单项转写。"""
        if not self._allow_retry():
            return
        if not self.settings or not self.settings.ocr:
            QMessageBox.information(self, "提示", NO_OCR_TIP)
            return
        if not self.current or not self.current.result:
            return
        try:
            cfg = self.settings.ocr_config()
        except ValueError as e:
            QMessageBox.warning(self, "配置有误", str(e))
            return
        result_path = self.current.result
        label = label or ("信息栏 " + key if kind == "field" else f"第 {key} 题")
        seq = self.current.seq
        q = OcrQueue(cfg)
        try:
            q.enqueue_item(result_path, kind, key)
        except Exception as e:
            q.close(cancel=True)
            QMessageBox.warning(self, "转写失败", str(e))
            return
        owner = str(result_path)
        self._begin_retry(owner, f"正在转写 #{seq:04d} {label}…")

        def run() -> None:
            try:
                q.wait()
                q.close()
                ok = q.total > 0 and q.done == q.total
                if ok:
                    msg = f"#{seq:04d} {label}转写完成"
                elif q.total == 0:
                    msg = f"#{seq:04d} {label}没有可转写的内容（可能已被替换），请刷新后再试"
                elif q.cancelled:
                    msg = (f"#{seq:04d} {label}转写已取消（该批次在扫描页被重新打开或程序正在退出），"
                           "保留为“等待转写”")
                else:
                    msg = f"#{seq:04d} {label}转写失败，原因见该项下方说明"
                self._retry.done.emit(owner, ok, msg)
            except Exception as e:  # noqa: BLE001
                q.close(cancel=True)
                self._retry.done.emit(owner, False, f"#{seq:04d} {label}转写失败：{e}")

        threading.Thread(target=run, daemon=True, name="ocr-retry-wait").start()

    def _retry_progress(self, owner: str, done: int, failed: int, total: int) -> None:
        if owner != self._retry_owner or not total:
            return
        self._notify(f"正在转写：已完成 {done} / {total} 项" + (f"，失败 {failed} 项" if failed else "") + "…")

    def _retry_done(self, owner: str, ok: bool, msg: str) -> None:
        """转写结束：恢复按钮、显示结果；只刷新任务所属的批次和结果。"""
        path = Path(owner).resolve()
        single = path.name == "result.json"
        batch = path.parent.parent.parent if single else path
        if self._retry_owner is not None and Path(self._retry_owner).resolve() == path:
            self._retry_owner = None
        self.ocrChanged.emit(str(batch))
        same = self.batch_dir is not None and self.batch_dir.resolve() == batch
        if msg:
            self._notify(msg if same else f"批次“{batch.name}”：{msg}")
        if same:
            self._reload_later(path if single else None, then=lambda _ok: self._update_buttons())
        self._update_buttons()

    def _retry_all_failed(self) -> None:
        """重试本批全部失败项并补做未开启的项：逐份重置 failed→pending、none→pending，投入临时 OcrQueue。

        某一份 result.json 坏了只跳过这一份并在结束时汇总，不影响其他已投递的项。"""
        if not self.batch_dir or not self.rows:
            return
        if not self._allow_retry():
            return
        if not self.settings or not self.settings.ocr:
            QMessageBox.information(self, "提示", NO_OCR_TIP)
            return
        try:
            cfg = self.settings.ocr_config()
        except ValueError as e:
            QMessageBox.warning(self, "配置有误", str(e))
            return
        owner = str(self.batch_dir.resolve())
        q = OcrQueue(cfg, listener=lambda e: self._retry.progress.emit(owner, e.done, e.failed, e.total))
        skipped: list[str] = []
        for r in self.rows:
            if not r.result or not r.result.exists():
                continue
            try:
                reset_failed(r.result)
                enable_ocr(r.result)
                q.enqueue(r.result)
            except Exception as e:  # noqa: BLE001 — 一份坏文件不影响其他答卷
                skipped.append(f"#{r.seq:04d}（{type(e).__name__}: {e}）")
                try:
                    # 已改成等待但没投递成功的项改回失败，不留下无人处理的等待项
                    fail_unclaimed(r.result, f"未能开始转写：{e}")
                except Exception:  # noqa: BLE001 — 文件读不了时原状态没被改动
                    pass
        problems = (f"{len(skipped)} 份无法处理：" + "、".join(skipped[:5])
                    + (" 等" if len(skipped) > 5 else "")) if skipped else ""
        if q.total == 0:
            q.close()
            self._reload(None)
            self._update_retry_controls()
            text = "没有剩余需要转写的项；正在转写的不会重复发送"
            QMessageBox.information(self, "提示", f"{text}。\n{problems}" if problems else text)
            if problems:
                self._notify(problems)
            return
        self._begin_retry(owner, f"正在转写剩余内容：共 {q.total} 项…" + (f"（{problems}）" if problems else ""))

        def run() -> None:
            try:
                q.wait()
                q.close()
                parts = [f"完成 {q.done}"]
                if q.failed - q.cancelled:
                    parts.append(f"失败 {q.failed - q.cancelled}")
                if q.cancelled:
                    parts.append(f"已取消 {q.cancelled}（该批次在扫描页被重新打开或程序正在退出，保留为“等待转写”）")
                msg = f"转写剩余内容结束：{'，'.join(parts)}，共 {q.total} 项" + (f"；{problems}" if problems else "")
                self._retry.done.emit(owner, q.done == q.total and not skipped, msg)
            except Exception as e:  # noqa: BLE001
                q.close(cancel=True)
                self._retry.done.emit(owner, False, f"转写剩余内容中断：{e}")

        threading.Thread(target=run, daemon=True, name="ocr-batch-wait").start()

    def _skip_pending(self) -> None:
        """把本批“等待转写”的项标为未启用（放弃转写），使交付不再被阻断；写回 result.json 并在交付记录中注明。"""
        if not self.batch_dir or not self._allow_retry():
            return
        self._reload(None)
        pending = sum(r.ocr_pending for r in self.rows)
        if not pending:
            self._update_retry_controls()
            QMessageBox.information(self, "提示", "没有等待转写的项")
            return
        answer = QMessageBox.question(self, "不再转写剩余项", skip_confirm_text(pending))
        if answer != QMessageBox.StandardButton.Yes:
            return
        changed = busy = 0
        errors: list[str] = []
        for r in self.rows:
            if not r.result or not r.ocr_pending:
                continue
            try:
                n, b = skip_pending(r.result, "操作员在结果页放弃转写")
                changed += n
                busy += b
            except Exception as e:  # noqa: BLE001 — 逐份处理，一份失败不影响其他答卷
                errors.append(f"#{r.seq:04d}（{e}）")
        msg = f"已放弃转写 {changed} 项：交付结果里这些题只有切图、没有文字"
        if busy:
            msg += f"；{busy} 项正在转写，未改动"
        if errors:
            msg += f"；{len(errors)} 份无法处理：" + "、".join(errors[:5])
        self._notify(msg)
        self.ocrChanged.emit(str(self.batch_dir.resolve()))
        self._reload(None)
        self._update_buttons()

    def _show_card(self) -> None:
        self.card.clear()
        self.card.setToolTip("")
        r = self.result
        if r is None or self.tpl is None or self.batch_dir is None or not self.current or not self.current.result:
            return
        page = r["pages"].get(self.face)
        if not page:
            return
        try:
            image = resolve_path(self.batch_dir, self.current.result.parent.resolve(), page)
        except (ValueError, OSError, RuntimeError):
            self.card.setToolTip("图片路径无效")
            return
        overlays = face_overlays(self.tpl, r, self.face)
        paper_w = self.tpl.paper[0]

        def shown(ok: bool) -> None:
            # 识别框按整页图的像素尺寸定位，图片显示后再画
            if not ok:
                return
            w, _h = self.card.image_size()
            s = w / paper_w  # 整页图像素 / mm
            self.card.set_overlays([
                Overlay(QRectF(o.rect[0] * s, o.rect[1] * s, o.rect[2] * s, o.rect[3] * s), OVERLAY_COLOR[o.kind],
                        fill_alpha=70 if o.kind in ("answer", "exam", "omr", "choice") else 25, label=o.label)
                for o in overlays])

        # 后台解码，不卡界面；证据预览已显示同一张整页图时直接共用
        self.card.load_image(image, shown, share=self.evidence.view)

    def _fill_subjective(self, r: dict | None) -> None:
        while self.subj_box.count():
            it = self.subj_box.takeAt(0)
            if it.widget():
                it.widget().hide()  # 立即隐藏，免得 deleteLater 生效前和新内容叠在一起
                it.widget().deleteLater()
        if not r:
            self._subj_empty(SUBJ_NO_BATCH if self.batch_dir is None else
                             SUBJ_NO_STUDENT if self.current is None else SUBJ_NO_RESULT)
            return
        has_ocr = bool(self.settings and self.settings.ocr)
        blocked = self._retry_block_reason()
        reasons = {w.get("field"): str(w.get("msg") or "").removeprefix("手写转写失败：")
                   for w in r.get("warnings", []) if isinstance(w, dict) and w.get("code") == "OCR_FAILED"}
        for q in r.get("subjective", []):
            key = subjective_item_key(q)
            label = f"第 {q['no']} 题" + (f"（{q['sub']}）" if q.get("sub") else "")
            self._add_ocr_entry(label, q, "subjective", key, q.get("images", []),
                                reasons.get(f"subjective.{key}"), has_ocr, blocked)
        # 信息栏（姓名等手写字段）也会转写、也会失败：接在主观题后面列出状态，可单项重试
        for key, f in ((r.get("student") or {}).get("fields") or {}).items():
            if not isinstance(f, dict):
                continue
            label = f"信息栏 {f.get('label') or key}"
            self._add_ocr_entry(label, f, "field", key, [f["image"]] if f.get("image") else [],
                                reasons.get(f"student.fields.{key}"), has_ocr, blocked)
        if not self.subj_box.count():
            self._subj_empty(SUBJ_NONE)
            return
        self.subj_box.addStretch(1)

    def _subj_empty(self, text: str) -> None:
        """主观题区的空状态说明。"""
        self.subj_empty = QLabel(text)
        self.subj_empty.setObjectName("muted")
        self.subj_empty.setWordWrap(True)
        self.subj_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subj_empty.setContentsMargins(0, 24, 0, 0)
        self.subj_box.addWidget(self.subj_empty)
        self.subj_box.addStretch(1)

    def _add_ocr_entry(self, title: str, q: dict, kind: str, key: str, images: list[str],
                       legacy_reason: str | None, has_ocr: bool, blocked: str) -> None:
        """一项可转写内容：标题、转写文本或状态（失败时带原因）、查看切图与重试按钮。"""
        t = QLabel(title)
        t.setTextFormat(Qt.TextFormat.PlainText)
        t.setStyleSheet("font-weight:600; margin-top:10px;")
        self.subj_box.addWidget(t)
        ocr_st = q.get("ocr_status")
        reason = q.get("ocr_error") or legacy_reason
        status = {"none": "已放弃转写" if q.get("ocr_skipped") else "未开启转写",
                  "pending": "等待转写",
                  "failed": f"转写失败：{reason}" if reason else "转写失败", "done": ""}.get(ocr_st, "")
        txt = QLabel(q.get("text") or status)
        txt.setTextFormat(Qt.TextFormat.PlainText)
        txt.setWordWrap(True)
        txt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.set_style(txt, "background:$HEADER; border:1px solid $LINE; padding:8px 10px;"
                        + ("color:$INK;" if q.get("text") else "color:$INK3;"))
        self.subj_box.addWidget(txt)
        # 查看切图 + 重试/转写按钮放在一行，按钮保持自身宽度
        row = QWidget()
        acts = QHBoxLayout(row)
        acts.setContentsMargins(0, 0, 0, 0)
        acts.setSpacing(6)
        for i, img in enumerate(images, 1):
            button = QPushButton(f"看切图 {i}" if kind == "subjective" else "看切图")
            button.setToolTip("在“切图与明细”页查看这一块的切图")
            button.clicked.connect(lambda _c=False, path=img: self._show_evidence(path))
            acts.addWidget(button)
        acts.addStretch(1)
        if ocr_st in ("failed", "none", "pending"):
            btn = QPushButton("重试转写" if ocr_st == "failed" else "转写")
            if blocked or not has_ocr:
                btn.setEnabled(False)
                btn.setToolTip(f"不能转写：{blocked or NO_OCR_TIP}")
            else:
                btn.setToolTip("重新转写这一项（失败原因见上方）" if ocr_st == "failed" else "现在就转写这一项")
                btn.clicked.connect(lambda _c=False, k=kind, kk=key, lb=title: self._retry_single(k, kk, lb))
            acts.addWidget(btn)
        self.subj_box.addWidget(row)

    def _show_evidence(self, path: str) -> None:
        self.evidence.select_image(path)
        self.card_tabs.setCurrentWidget(self.evidence)
