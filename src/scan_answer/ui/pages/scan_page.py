"""扫描页：左栏管批次（异常卡、作废、警告、结束批次），中栏计数 + 批次拼图 + 最近一张，右栏管这次导入 / 扫描（输入源、参数、开始 / 停止）。"""
from __future__ import annotations

import copy
import html

import json
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QEvent, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget)

from ...app.library import Exam, ensure_exam_snapshot
from ...app.results import PHYSICAL_TEXT
from ...app.settings import AppSettings, save_settings
from ...pipeline.events import DeviceNotice, FinalizedEvent, OcrProgress, SheetEvent, SheetSkipped
from ...source.documents import DOCUMENT_FILTER, DocumentSource
from ...source.twain_source import TwainConfig, TwainSource, list_scanners, twain_available
from .. import theme
from ..background import Latest
from ..help_text import (CROP_OPTIONS, DOUBLE_FEED_TIP, DROPOUT_OPTIONS, DUPLEX_OPTIONS, SCAN_PARAM_LABELS,
                         SCAN_PARAMS_SYNC_NOTE)
from ..controller import (BUSY_STATES, FINALIZED, FINISHING, IDLE, PAUSED, RELOCATING, REPROCESSING, RESCANNING,
                          RUNNING, STOPPING, ScanController)
from ..widgets.card_view import CardView
from ..widgets.guide import HelpButton
from ..widgets.common import CounterTile, ElidedLabel, Segmented, Swatch, Toggle, hline
from ..widgets.scan_fold import FoldSection
from ..widgets.mosaic import ERR, OK, QUEUED, VOID, WARN, BatchMosaic, MosaicLegend

STATE_TEXT = {IDLE: "就绪", RUNNING: "识别中", PAUSED: "已暂停", FINISHING: "结束批次中",
              FINALIZED: "已结束", RESCANNING: "重扫中", STOPPING: "正在停止",
              RELOCATING: "手动定位中", REPROCESSING: "重新识别中"}
REWORK_STATES = (RESCANNING, RELOCATING, REPROCESSING)  # 重扫类操作：一次只处理一张卡


ALIGN_TEXT = {"anchors": "定位块", "frames": "外框", "features": "按版面比对", "manual": "手动定位",
              "derived": "由另一面推算"}
RELOCATABLE = {"ALIGN_FAILED", "FACE_MISMATCH", "SKEW_TOO_LARGE"}  # 原图完整、只是没能自动定位
EXC_EMPTY_IDLE = "暂无。定位失败、缺面、空白页、别科的卡（混卡）等需要处理的卡会列在这里，处理完才能结束批次。"
EXC_EMPTY_CLEAR = "没有待处理的异常卡。"
EXC_LIST_MIN_HEIGHT = 200  # 物理异常列表的最小高度（1280×800 下靠伸展系数分到约两张卡以上）
EXC_LIST_STRETCH = 4  # 物理异常列表的伸展系数（折叠行展开时为 1）：有异常时列表优先占高度
FOLDER_EDIT_TIP = "要导入的答卷：图片、PDF 或整个文件夹。点右边的“文件…”“文件夹…”选择，也可以直接粘贴路径"

# 扫描参数每个选项的说明（悬停在分段按钮上显示）
PARAM_TIPS: dict[str, dict[str, str]] = {
    "duplex": {"1": "正反两面都扫：双面答题卡选这个", "0": "只扫正面：单面答题卡选这个"},
    "dpi": {"200": "最快、文件最小；只适合印刷和涂卡都很清楚的卡",
            "300": "推荐：速度和清晰度兼顾",
            "400": "更清晰，扫描稍慢",
            "600": "最清晰，但明显变慢，存储约为 300 DPI 的 4 倍；一般不需要"},
    "color": {"gray": "推荐：文件小、速度快", "color": "保留颜色；扫描仪去不干净彩色印刷时改用彩色，由软件去色"},
    "dropout": {"none": "不去掉：黑色印刷的卡选这个",
                "red": "去掉卡面上的红色印刷（如红色选项框、底纹），只留下涂黑",
                "green": "去掉卡面上的绿色印刷，只留下涂黑",
                "blue": "去掉卡面上的蓝色印刷，只留下涂黑"},
    "crop": {"auto": "推荐：扫描仪自动找纸边，裁掉多余的背景",
             "template": "按模板的纸张尺寸裁；自动找纸边不准时用",
             "driver": "不额外设置，用扫描仪驱动里的默认裁切"},
}
# 扫描仪报双张 / 卡纸时界面上的说明（来自图像源的原文提到旧按钮名，这里按种类换成现在的说法）
DEVICE_TEXT = {
    "double_feed": "检测到两张纸粘在一起进纸，已暂停。已接收的页面保留；取出粘在一起的纸后点“继续”。"
                   "如果左栏出现缺面的异常卡，先点“停止进纸”，按原序号整份重扫，再继续其余答卷。",
    "paper_jam": "扫描仪卡纸，已暂停。取出卡住的纸后点“继续”；如果左栏出现缺面的异常卡，"
                 "先点“停止进纸”，按原序号整份重扫，再继续其余答卷。",
}
# 涂卡质量提示：按提示代码换成白话（原文带涂黑度、阈值数字，放进悬停说明）
QUALITY_TEXT = {
    "LIGHT_MARKS": "这批卡整体涂得偏浅（可能铅笔偏软或扫描偏亮），浅的会标为“存疑”，请到结果页筛选“有警告”核对",
    "LOW_SEPARATION": "涂了和没涂的差别不明显，存疑题可能较多，可检查扫描亮度或“去掉彩色印刷”设置",
    "THRESHOLD_CLAMPED": "卡面底色偏深，可能影响判断，请抽查几份结果",
}
STOP_NOTE = "批次不会结束，可以再点“开始识别”接着{}"
NOT_FOR_FILES = "\n（文件导入时不用设置，只在用扫描仪时生效）"


def _tip(desc: str, reason: str | None = None) -> str:
    """按钮提示：做什么；被禁用时再说明为什么不能点、该先做什么。"""
    return f"{desc}\n现在不能点：{reason}" if reason else desc


# 混卡（WRONG_CARD）是定位成功后卡面标识对不上，手动定位或重新识别都无法修正，故不在其中；
# 确是本科目的卡（例如学生在标识区写画）由操作员点“是本科目的卡，照常识别”后跳过标识检查重新识别，否则作废


class ExceptionItem(QWidget):
    """待处理的异常卡：缩略图 + 原因 + 重扫 / 重新识别 / 手动定位 / 作废。

    raw_ok：raw/ 里有这张卡的完整原图（才提供“重新识别”和“手动定位”）；
    voided：已作废的卡，只提供“重扫”和“撤销作废”（变回异常卡）。"""
    rescanClicked = Signal(int)
    reprocessClicked = Signal(int)
    confirmIdentityClicked = Signal(int)
    relocateClicked = Signal(int)
    voidClicked = Signal(int)
    restoreClicked = Signal(int)

    def __init__(self, seq: int, code: str, message: str, thumb: Path | None, parent: QWidget | None = None,
                 *, raw_ok: bool = True, voided: bool = False):
        super().__init__(parent)
        self.seq = seq
        self.voided = voided
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 10, 16, 10)
        outer.setSpacing(8)
        lay = QHBoxLayout()
        lay.setSpacing(12)
        pic = QLabel()
        pic.setFixedSize(96, 68)
        theme.set_style(pic, "background:$CANVAS; border:1px solid $LINE;")
        if thumb and thumb.exists():
            pm = QPixmap(str(thumb))
            if not pm.isNull():
                pic.setPixmap(pm.scaled(96, 68, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(2)
        no = QLabel(f"#{seq:04d}" + ("　已作废" if voided else ""))
        no.setStyleSheet("font-weight:600;")
        why = QLabel(PHYSICAL_TEXT.get(code, code))
        theme.set_style(why, "color:$INK3; font-weight:500;" if voided else "color:$ERR; font-weight:500;")
        msg = QLabel(message)
        msg.setObjectName("muted")
        msg.setWordWrap(True)
        info.addWidget(no)
        info.addWidget(why)
        info.addWidget(msg)
        info.addStretch(1)
        lay.addLayout(info, 1)
        outer.addLayout(lay)
        acts = QHBoxLayout()
        acts.setSpacing(6)
        more: list[QPushButton] = []  # 重新识别 / 手动定位 / 是本科目的卡：排在重扫、作废之后
        b1 = QPushButton("重扫")
        b1.setToolTip("重新扫描这张卡：扫描仪模式下把它单独放进进纸器扫；文件导入时选它重扫后的图片或 PDF。"
                      "成功后替换这张卡的记录，失败保留原记录")
        b1.clicked.connect(lambda: self.rescanClicked.emit(self.seq))
        acts.addWidget(b1)
        if not voided and code in RELOCATABLE and raw_ok:
            b4 = QPushButton("重新识别")
            b4.setToolTip("不用重扫：用已保存的原图再自动识别一次。失败时保留原记录，可以改用“手动定位”")
            b4.clicked.connect(lambda: self.reprocessClicked.emit(self.seq))
            more.append(b4)
            b3 = QPushButton("手动定位")
            b3.setToolTip("不用重扫：在原图上依次点出定位块（或外框四角），按你点的位置重新识别。"
                          "自动定位反复失败时用")
            b3.clicked.connect(lambda: self.relocateClicked.emit(self.seq))
            more.append(b3)
        if not voided and code == "WRONG_CARD" and raw_ok:
            b5 = QPushButton("是本科目的卡，照常识别")
            b5.setToolTip("核对原图后确认这张就是本科目的卡、不是混卡（例如学生在科目标识区里写画）："
                          "不再比对卡面上的科目名，按本模板重新识别；结果会带“操作员确认”警告")
            b5.clicked.connect(lambda: self.confirmIdentityClicked.emit(self.seq))
            more.append(b5)
            b6 = QPushButton("手动定位")
            b6.setToolTip("确认是本科目的卡、且自动定位不准时：在原图上点出定位块（或外框的角），"
                          "不再比对卡面上的科目名，重新识别")
            b6.clicked.connect(lambda: self.relocateClicked.emit(self.seq))
            more.append(b6)
        if voided:
            b2 = QPushButton("撤销作废")
            b2.setToolTip("撤销作废：这张卡变回异常卡（不是正常卡），结束批次前要重扫、重新识别或再作废")
            b2.clicked.connect(lambda: self.restoreClicked.emit(self.seq))
        else:
            b2 = QPushButton("作废")
            b2.setObjectName("danger")
            b2.setToolTip("不要这张卡（例如空白页、混入的别科答卷）：不输出结果，也不再挡住结束批次；"
                          "之后可以在左栏“已作废”里撤销作废或重扫")
            b2.clicked.connect(lambda: self.voidClicked.emit(self.seq))
        acts.addWidget(b2)
        wide = [b for b in more if b is not None and len(b.text()) > 6]  # 长按钮单独一行，免得挤出左栏
        for b in more:
            if b not in wide:
                acts.addWidget(b)
        acts.addStretch(1)
        if not voided and not raw_ok and (code in RELOCATABLE or code == "WRONG_CARD"):
            note = QLabel("原图不完整，不能重新识别或手动定位，只能重扫或作废。")
            note.setObjectName("muted")
            note.setWordWrap(True)
            outer.addWidget(note)
        outer.addLayout(acts)
        for b in wide:
            line = QHBoxLayout()
            line.addWidget(b)
            line.addStretch(1)
            outer.addLayout(line)
        # 左栏窄：按钮紧凑一些，四个排得下一行
        self.setObjectName("excItem")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        theme.set_style(self, "#excItem { border-bottom:1px solid $LINE; }"
                              " QPushButton { padding:3px 8px; font-size:13px; }")


class ScanPage(QWidget):
    openResult = Signal(str, int)  # 批次目录, 序号

    def __init__(self, controller: ScanController, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctrl = controller
        self.settings = settings
        self._preparing = False  # 正在后台准备新批次（固定模板快照）
        self._closed_note: str | None = None  # 点“另起一批”关掉的批次：在“下一步”行说明去哪找回
        self._finish_ok = False  # finish_result 显示的是“批次已结束”（批次回到未结束时要收起）
        self._batch_prep = Latest(self, wait_for_modal=True)
        self._relocate_loader = Latest(self, wait_for_modal=True)  # 手动定位：后台读模板与原图
        self.exam: Exam | None = None
        self.events: dict[int, SheetEvent] = {}
        self._voided: set[int] = set()
        self.done_times: deque[float] = deque(maxlen=400)
        self.started_at: float | None = None
        self.ocr = OcrProgress(0, 0, 0)
        self._pending_defaults = None
        self._warning_lines: list[str] | None = None
        self._picked: list[str] = []  # “文件…”多选的文件；输入框文字被改动后不再使用
        self._picked_text = ""
        self._run_note: str | None = None  # 本轮的配对提示
        self._skipped: list[int] = []  # 本轮跳过的重复导入（与之相同的已登记序号）
        self._round_count: int | None = None  # 本轮已读入的张数；本页还没开始过一轮时为 None
        self._build()
        c = self.ctrl
        c.sheetQueued.connect(self._on_queued)
        c.sheetDone.connect(self._on_done)
        c.voided.connect(self._on_voided)
        c.unvoided.connect(self._on_unvoided)
        c.sheetSkipped.connect(self._on_skipped)
        c.finalized.connect(self._on_finalized)
        c.thresholdChanged.connect(lambda e: self._show_threshold_issues(e.issues))
        c.ocrProgress.connect(self._on_ocr)
        c.stateChanged.connect(self._on_state)
        c.deviceNotice.connect(self._on_device)
        c.errorOccurred.connect(lambda m: QMessageBox.warning(self, "出错了", m))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._on_source_changed(self.src_seg.value())
        if self._twain_ok:
            self._refresh_scanners()
        self._on_state(IDLE)
        self._refresh_round()
        self.refresh_theme()  # 初始就显示警告区的空状态说明、区域色码

    # ---------- 布局 ----------
    # 左栏管批次（整批的事：另起一批、异常卡、作废、警告、结束批次），中栏看进度，右栏管这次导入 / 扫描（输入、参数、开始/暂停/停止）
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.batch_panel = self._left()
        self.progress_panel = self._middle()
        self.round_panel = self._right()
        root.addWidget(self.batch_panel, 0)
        root.addWidget(self.progress_panel, 1)
        root.addWidget(self.round_panel, 0)

    @staticmethod
    def _column_header(title: str, help: str | None = None) -> tuple[QWidget, QHBoxLayout]:
        """栏头：标题（+“?”），右侧留给调用方放按钮或说明。"""
        w = QWidget()
        w.setObjectName("colHead")
        w.setFixedHeight(48)
        theme.set_style(w, "#colHead { background:$HEADER; border-bottom:1px solid $LINE; }")
        row = QHBoxLayout(w)
        row.setContentsMargins(16, 0, 16, 0)
        row.setSpacing(8)
        t = QLabel(title)
        t.setStyleSheet("font-size:15px; font-weight:600;")
        row.addWidget(t)
        if help:
            row.addWidget(HelpButton(help))
        row.addStretch(1)
        return w, row

    def _left(self) -> QWidget:
        w = QWidget()
        w.setObjectName("panel")
        w.setFixedWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head, row = self._column_header("批次", "batch")
        self.new_btn = QPushButton("另起一批")
        self.new_btn.clicked.connect(self.new_batch)
        row.addWidget(self.new_btn)
        lay.addWidget(head)
        # 当前批次：批次号 + 状态 + 模板与考试
        ident = QWidget()
        ident.setObjectName("batchIdent")
        theme.set_style(ident, "#batchIdent { border-bottom:1px solid $LINE; }")
        il = QVBoxLayout(ident)
        il.setContentsMargins(16, 10, 16, 12)
        il.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.batch_label = ElidedLabel("未开始批次", Qt.TextElideMode.ElideMiddle)
        self.batch_label.setStyleSheet("font-size:15px; font-weight:600;")
        top.addWidget(self.batch_label, 1)
        self.state_chip = QLabel()
        top.addWidget(self.state_chip)
        il.addLayout(top)
        self.meta_label = ElidedLabel("", Qt.TextElideMode.ElideRight)
        theme.set_style(self.meta_label, "color:$INK3; font-size:12px;")
        il.addWidget(self.meta_label)
        lay.addWidget(ident)
        # 异常卡
        self.exc_header = QWidget()
        hl = QHBoxLayout(self.exc_header)
        hl.setContentsMargins(16, 10, 16, 6)
        hl.setSpacing(6)
        self.exc_title = QLabel("异常卡 0")
        self.exc_title.setStyleSheet("font-weight:600;")
        hl.addWidget(self.exc_title)
        hl.addWidget(HelpButton("exceptions"))
        hl.addStretch(1)
        note = QLabel("需处理后才能结束批次")
        note.setObjectName("muted")
        hl.addWidget(note)
        lay.addWidget(self.exc_header)
        self.exc_empty = QLabel(EXC_EMPTY_IDLE)
        self.exc_empty.setObjectName("muted")
        self.exc_empty.setWordWrap(True)
        self.exc_empty.setContentsMargins(16, 6, 16, 0)
        # 没有物理异常时列表隐藏，空状态文字只按文字要高度；折叠行用不完的高度才留在文字下方
        self.exc_empty.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.exc_empty.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        lay.addWidget(self.exc_empty)
        self.exc_box = QVBoxLayout()
        self.exc_box.setContentsMargins(0, 0, 0, 0)
        self.exc_box.setSpacing(0)
        holder = QWidget()
        holder.setLayout(self.exc_box)
        self.exc_box.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(holder)
        holder.setAutoFillBackground(False)  # setWidget 会打开自动填充，露出窗口底色
        # 有异常时至少约两张卡、优先分高度（两个折叠行都展开时由它们让出，内容区自己滚动）；没有异常时隐藏
        sa.setMinimumHeight(EXC_LIST_MIN_HEIGHT)
        lay.addWidget(sa, EXC_LIST_STRETCH)
        self.exc_scroll = sa
        sa.setVisible(False)
        # 已作废的卡：可恢复为异常卡或重扫（折叠行）
        self.void_fold = FoldSection("已作废 0", "可撤销作废或重扫", tip="展开 / 收起已作废的卡：可以撤销作废（变回异常卡），或重扫")
        self.void_title = self.void_fold.title
        self.void_empty = QLabel("点异常卡上的“作废”后会移到这里，之后可以撤销作废或重扫。")
        self.void_empty.setObjectName("muted")
        self.void_empty.setWordWrap(True)
        self.void_empty.setContentsMargins(16, 4, 16, 8)
        self.void_fold.content_layout.addWidget(self.void_empty)
        self.void_box = QVBoxLayout()
        self.void_box.setContentsMargins(0, 0, 0, 0)
        self.void_box.setSpacing(0)
        self.void_fold.content_layout.addLayout(self.void_box)
        self.void_fold.content_layout.addStretch(1)
        self.void_fold.setVisible(False)  # 有批次后才显示（为空时展开是说明）
        lay.addWidget(self.void_fold)
        # 有警告（折叠行）
        self.warn_fold = FoldSection("警告 0", "只标记，不拦截", help="scan_warnings",
                                     tip="展开 / 收起有警告的卡（只标记，不拦截结束批次）")
        self.warn_title, self.warn_note = self.warn_fold.title, self.warn_fold.note
        self.warn_list = QLabel("")
        self.warn_list.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.warn_list.setWordWrap(True)
        self.warn_list.setContentsMargins(16, 4, 16, 8)
        self.warn_list.setStyleSheet("font-size:13px; line-height:150%;")
        self._warning_lines = []
        self.warn_fold.content_layout.addWidget(self.warn_list)
        warn_more = QLabel("具体内容到结果页筛选“有警告”查看")
        warn_more.setObjectName("muted")
        warn_more.setWordWrap(True)
        warn_more.setContentsMargins(16, 0, 16, 8)
        self.warn_fold.content_layout.addWidget(warn_more)
        self.warn_fold.content_layout.addStretch(1)
        lay.addWidget(self.warn_fold)
        # 结束批次：前置检查清单 + 按钮 + 上次结束批次的结果
        foot = QWidget()
        foot.setObjectName("finishPanel")
        theme.set_style(foot, "#finishPanel { background:$NOTICE; border-top:1px solid $LINE; }")
        fl = QVBoxLayout(foot)
        fl.setContentsMargins(16, 12, 16, 14)
        fl.setSpacing(8)
        fh = QHBoxLayout()
        fh.setSpacing(6)
        ft = QLabel("结束批次")
        ft.setStyleSheet("font-weight:600;")
        fh.addWidget(ft)
        fh.addWidget(HelpButton("finalize"))
        fh.addStretch(1)
        fl.addLayout(fh)
        self.finish_checks = QLabel("")
        self.finish_checks.setWordWrap(True)
        self.finish_checks.setTextFormat(Qt.TextFormat.RichText)
        self.finish_checks.setStyleSheet("font-size:13px;")
        fl.addWidget(self.finish_checks)
        self.finish_btn = QPushButton("结束批次")
        self.finish_btn.setMinimumHeight(40)
        self.finish_btn.clicked.connect(self.finalize)
        fl.addWidget(self.finish_btn)
        # 结束批次的结果（白话；阈值等数字只放悬停说明）：放在按钮旁边，不写进“最近一张”的标题
        self.finish_result = QLabel("")
        self.finish_result.setObjectName("finishResult")
        self.finish_result.setWordWrap(True)
        self.finish_result.setVisible(False)
        fl.addWidget(self.finish_result)
        lay.addWidget(foot)
        return w

    def _middle(self) -> QWidget:
        w = QWidget()
        w.setObjectName("work")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 12, 24, 12)
        lay.setSpacing(0)
        # 下一步：按当前状态告诉用户该做什么（不弹窗）
        self.next_step = QLabel("")
        self.next_step.setObjectName("nextStep")
        self.next_step.setWordWrap(True)
        self.next_step.setTextFormat(Qt.TextFormat.RichText)
        theme.set_style(self.next_step, "#nextStep { color:$INK2; font-size:13px; padding:0 0 6px 0; }")
        lay.addWidget(self.next_step)
        self.notice = QLabel("")
        self.notice.setObjectName("deviceNotice")
        self.notice.setWordWrap(True)
        self.notice.setVisible(False)
        lay.addWidget(self.notice)
        tiles = QHBoxLayout()
        tiles.setSpacing(0)
        self.t_scanned = CounterTile("已扫描", note="张，正反面已配对")
        self.t_ok = CounterTile("成功", note="可输出")
        self.t_err = CounterTile("异常卡", "ERR", "ERR", note="待处理")
        self.t_warn = CounterTile("警告", "WARN", "WARN", note="已标记")
        self.t_speed = CounterTile("速度", unit="张/分", note="最近一分钟")
        self.t_ocr = CounterTile("手写转写", "REGION_TEXT_sub", note="未开启")
        for t in (self.t_scanned, self.t_ok, self.t_err, self.t_warn, self.t_speed, self.t_ocr):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)
        lay.addWidget(hline())
        head = QHBoxLayout()
        head.setContentsMargins(0, 12, 0, 10)
        head.setSpacing(12)
        t = QLabel("批次进度")
        t.setStyleSheet("font-weight:600; font-size:15px;")
        head.addWidget(t)
        self.progress_label = QLabel("0 张")
        theme.set_style(self.progress_label, "color:$INK2;")
        head.addWidget(self.progress_label)
        head.addStretch(1)
        self.expand_btn = QPushButton("展开全部")
        self.expand_btn.setToolTip("批次超过 1000 张时拼图默认只显示最近 10 行；点这里展开全部或收起")
        self.expand_btn.setVisible(False)
        self.expand_btn.clicked.connect(self._toggle_mosaic_expand)
        head.addWidget(self.expand_btn)
        head.addWidget(MosaicLegend())
        lay.addLayout(head)
        self.mosaic = BatchMosaic()
        self.mosaic.cellClicked.connect(self._open_cell)
        lay.addWidget(self.mosaic)
        self.mosaic_hint = QLabel("")  # 有卡时是用法说明，没有卡时是空状态（下一步怎么做）
        self.mosaic_hint.setObjectName("muted")
        self.mosaic_hint.setWordWrap(True)
        self.mosaic_hint.setContentsMargins(0, 6, 0, 0)
        lay.addWidget(self.mosaic_hint)
        lay.addSpacing(12)
        lay.addWidget(hline())
        prev = QHBoxLayout()
        prev.setContentsMargins(0, 12, 0, 0)
        left = QVBoxLayout()
        self.prev_title = QLabel("最近一张")
        self.prev_title.setStyleSheet("font-weight:600;")
        left.addWidget(self.prev_title)
        self.preview = CardView(placeholder="开始识别后，这里显示最近一张卡")
        self.preview.setFixedHeight(277)
        self.preview.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.preview.setMinimumWidth(200)
        self.preview.setMaximumWidth(420)
        left.addWidget(self.preview, 1)
        left.addStretch(0)
        prev.addLayout(left, 1)
        prev.addSpacing(20)
        self.readout = QGridLayout()
        self.readout.setVerticalSpacing(10)
        self.readout_values: dict[str, QLabel] = {}
        for i, (key, label, color) in enumerate([("exam", "考号", theme.REGION["id"]), ("bar", "条码", theme.REGION["bar"]),
                                                 ("absent", "缺考", theme.REGION["info"]), ("obj", "客观题", theme.REGION["obj"]),
                                                 ("sub", "主观题", theme.REGION["sub"]), ("align", "对齐方式", theme.REGION["loc"])]):
            self.readout.addWidget(Swatch(color, 8), i, 0)
            k = QLabel(label)
            theme.set_style(k, "color:$INK2;")
            self.readout.addWidget(k, i, 1)
            v = QLabel("—")
            v.setStyleSheet("font-weight:600;")
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.readout.addWidget(v, i, 2)
            self.readout_values[key] = v
        rw = QWidget()
        rw.setLayout(self.readout)
        rw.setContentsMargins(0, 22, 0, 0)  # 与卡面顶边对齐（左边多一行标题）
        prev.addWidget(rw, 1, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(prev)
        # 区域色码：卡面下方一行（窄窗口时自动折行）
        self.region_legend = QLabel()
        self.region_legend.setWordWrap(True)
        self.region_legend.setTextFormat(Qt.TextFormat.RichText)
        self.region_legend.setContentsMargins(0, 10, 0, 0)
        theme.set_style(self.region_legend, "color:$INK2; font-size:12px;")
        lay.addWidget(self.region_legend)
        lay.addStretch(1)
        return w

    def _right(self) -> QWidget:
        w = QWidget()
        w.setObjectName("panel")
        w.setFixedWidth(300)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head, _row = self._column_header("答卷从哪来")
        lay.addWidget(head)
        form = QVBoxLayout()
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(10)
        self._twain_ok, self._twain_reason = twain_available()
        self.src_seg = Segmented([("scanner", "扫描仪"), ("folder", "文件导入")],
                                 "scanner" if self._twain_ok else "folder")
        if not self._twain_ok:
            self.src_seg.buttons["scanner"].setEnabled(False)
            self.src_seg.buttons["scanner"].setToolTip(self._twain_reason)
        self.src_seg.changed.connect(self._on_source_changed)
        self.src_seg.buttons["folder"].setToolTip("从电脑上的图片、PDF 或文件夹导入已经扫好的答卷")
        form.addLayout(self._field("输入源", self.src_seg))
        # 扫描仪
        self.scanner_box = QWidget()
        sl = QHBoxLayout(self.scanner_box)
        sl.setContentsMargins(0, 0, 0, 0)
        self.scanner_combo = QComboBox()
        self.scanner_combo.setMinimumWidth(180)
        self.scanner_combo.setToolTip("选择要用的扫描仪")
        refresh = QPushButton("刷新")
        refresh.setToolTip("重新查找已连接的扫描仪（刚接上或刚开机时点）")
        refresh.clicked.connect(self._refresh_scanners)
        sl.addWidget(self.scanner_combo, 1)
        sl.addWidget(refresh)
        form.addWidget(self.scanner_box)
        # 导入图片
        # 右栏在 800 高的窗口里很挤：只放两行要点，完整规则放悬停说明，避免折行文字被压得上下截断
        self.folder_note = QLabel("双面卡：正反面要挨着放（文件名带 _1 _2，或按名排序每两张一份）。悬停看详细规则")
        self.folder_note.setToolTip("PDF 在各自文件内按页序配对。图片文件名都是“前缀_1、前缀_2”（面号）时按前缀配对，"
                                    "否则按文件名排序每 2 张（单面模板每张）配成一份；缺页会让之后的答卷串卷。")
        self.folder_note.setWordWrap(True)
        self.folder_note.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        theme.set_style(self.folder_note, "background:$NOTICE; color:$INK2; font-size:12px; padding:8px 10px;")
        form.addWidget(self.folder_note)
        self.folder_box = QWidget()
        row = QHBoxLayout(self.folder_box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("点“文件…”或“文件夹…”选择")
        self.folder_edit.setToolTip(FOLDER_EDIT_TIP)
        self.folder_edit.textChanged.connect(self._on_input_text)
        pick = QPushButton("文件夹…")
        pick.setToolTip("选择一个文件夹，导入里面的图片和 PDF")
        pick.clicked.connect(self._pick_folder)
        pick_file = QPushButton("文件…")
        pick_file.setToolTip("选择图片或 PDF，可多选：双面答题卡请同时选中正反面图片")
        pick_file.clicked.connect(self._pick_input_file)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(pick_file)
        row.addWidget(pick)
        form.addWidget(self.folder_box)
        # 扫描参数：一行摘要 +“修改扫描参数…”，全部参数在浮层里（只在用扫描仪时显示）
        self.param_summary = QWidget()
        self.param_summary.setObjectName("paramSummary")
        theme.set_style(self.param_summary, "#paramSummary { background:$WORK; border:1px solid $LINE;"
                                            " border-radius:6px; }")
        ps = QGridLayout(self.param_summary)
        ps.setContentsMargins(10, 8, 10, 8)
        ps.setHorizontalSpacing(8)
        ps.setVerticalSpacing(2)
        self.param_summary_text = QLabel("")  # 扫描面 · DPI · 色彩
        self.param_summary_text.setStyleSheet("font-size:13px;")
        ps.addWidget(self.param_summary_text, 0, 0, 1, 2)
        self.param_summary_sub = QLabel("")  # 双张检测 · 彩色印刷 · 裁掉纸边（占满整行，放不下时折行）
        self.param_summary_sub.setObjectName("muted")
        self.param_summary_sub.setWordWrap(True)
        ps.addWidget(self.param_summary_sub, 1, 0, 1, 2)
        self.param_summary_note = QLabel("600 DPI 扫描明显变慢，存储约为 300 DPI 的 4 倍")  # 选 600 时才显示
        self.param_summary_note.setWordWrap(True)
        theme.set_style(self.param_summary_note, "color:$WARN; font-size:12px;")
        ps.addWidget(self.param_summary_note, 2, 0, 1, 2)
        self.param_btn = QPushButton("修改扫描参数…")
        self.param_btn.setToolTip("查看和修改全部扫描参数（扫描面、分辨率、色彩、双张检测、去掉彩色印刷、裁掉纸边）；"
                                  f"下次开始识别时使用。{SCAN_PARAMS_SYNC_NOTE}")
        self.param_btn.clicked.connect(self._open_params)
        ps.addWidget(self.param_btn, 3, 0, 1, 2, Qt.AlignmentFlag.AlignLeft)  # 按钮名较长：单独一行，摘要不被挤掉
        ps.setColumnStretch(0, 1)
        form.addWidget(self.param_summary)
        self._build_param_popup()
        self.template_label = ElidedLabel("—", Qt.TextElideMode.ElideRight)
        self.template_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addSpacing(4)
        form.addLayout(self._field("模板", self.template_label, fill=True))
        self.out_label = ElidedLabel("—")
        self.out_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.out_label.setStyleSheet("font-size:12px;")
        form.addLayout(self._field("这场考试的文件夹", self.out_label, fill=True))
        lay.addLayout(form)
        lay.addStretch(1)
        btns = QVBoxLayout()
        btns.setContentsMargins(16, 12, 16, 14)
        btns.setSpacing(8)
        r1 = QHBoxLayout()
        self.round_label = QLabel("")
        self.round_label.setObjectName("muted")
        r1.addWidget(self.round_label)
        r1.addStretch(1)
        self.round_speed = QLabel("")
        self.round_speed.setObjectName("muted")
        r1.addWidget(self.round_speed)
        btns.addLayout(r1)
        self.main_btn = QPushButton("开始识别")
        self.main_btn.setObjectName("big")
        self.main_btn.clicked.connect(self._main_clicked)
        btns.addWidget(self.main_btn)
        self.stop_btn = QPushButton("停止导入剩余文件")  # 名字和提示随输入源和状态更新（_refresh_guidance）
        self.stop_btn.clicked.connect(self._stop_round)
        btns.addWidget(self.stop_btn)
        self.stop_note = QLabel(STOP_NOTE.format("导入"))  # 常驻：停止不会结束批次
        self.stop_note.setObjectName("muted")
        self.stop_note.setWordWrap(True)
        theme.set_style(self.stop_note, "font-size:12px;")
        btns.addWidget(self.stop_note)
        r3 = QHBoxLayout()
        r3.setSpacing(4)
        hint = QLabel("空格键 暂停 / 继续")
        hint.setObjectName("muted")
        r3.addWidget(hint)
        r3.addStretch(1)
        diff = QLabel("停止和结束批次的区别")
        diff.setObjectName("muted")
        r3.addWidget(diff)
        r3.addWidget(HelpButton("end_round"))
        btns.addLayout(r3)
        bw = QWidget()
        bw.setObjectName("btnPanel")
        bw.setLayout(btns)
        # 用对象名限定选择器：不带选择器的样式会传给子控件，覆盖按钮自身的样式
        theme.set_style(bw, "#btnPanel { background:$NOTICE; border-top:1px solid $LINE; }")
        lay.addWidget(bw)
        return w

    def _build_param_popup(self) -> None:
        """“修改扫描参数…”弹出的浮层：控件就是原来的参数控件，改动立即生效（下次开始识别时用，并写回设置）。"""
        self.param_pop = QFrame(self, Qt.WindowType.Popup)
        self.param_pop.setObjectName("paramPop")
        theme.set_style(self.param_pop, "#paramPop { background:$WORK; border:1px solid $LINE2; }")
        self.param_pop.setFixedWidth(420)
        self.param_pop.installEventFilter(self)
        form = QVBoxLayout(self.param_pop)
        form.setContentsMargins(14, 12, 14, 12)
        form.setSpacing(8)
        title = QLabel(f"扫描参数（只在用扫描仪时生效；{SCAN_PARAMS_SYNC_NOTE}）")
        title.setObjectName("muted")
        title.setWordWrap(True)
        form.addWidget(title)
        sd = self.settings.scan
        self.duplex = Segmented(DUPLEX_OPTIONS, "1" if sd.duplex else "0")
        self.dpi = Segmented([(str(d), str(d)) for d in (200, 300, 400, 600)], str(sd.dpi))
        self.color = Segmented([("gray", "灰度"), ("color", "彩色")], sd.color)
        self.double_feed = Toggle(sd.double_feed)
        self.dropout = Segmented(DROPOUT_OPTIONS, sd.dropout)
        self.crop = Segmented(CROP_OPTIONS, sd.crop)
        self._scan_params = (self.duplex, self.dpi, self.color, self.double_feed, self.dropout, self.crop)
        self._param_labels: list[QLabel] = []
        for key, wdg, concept in (("duplex", self.duplex, None), ("dpi", self.dpi, None),
                                  ("color", self.color, None), ("double_feed", self.double_feed, None),
                                  ("dropout", self.dropout, "dropout"), ("crop", self.crop, "crop")):
            row = self._field(SCAN_PARAM_LABELS[key], wdg, help=concept)
            self._param_labels.append(row.itemAt(0).widget())
            form.addLayout(row)
        self.dpi_note = QLabel("600 DPI 扫描明显变慢，存储约为 300 DPI 的 4 倍")
        self.dpi_note.setObjectName("muted")
        self.dpi_note.setWordWrap(True)
        self.dpi_note.setVisible(sd.dpi == 600)
        self.dpi.changed.connect(lambda v: self.dpi_note.setVisible(v == "600"))
        form.addWidget(self.dpi_note)
        for seg in (self.duplex, self.dpi, self.color, self.dropout, self.crop):
            seg.group.buttonToggled.connect(lambda *_: self._refresh_param_summary())
        self.double_feed.toggled.connect(lambda *_: self._refresh_param_summary())
        self._refresh_param_summary()

    def _refresh_param_summary(self) -> None:
        dropout = dict(DROPOUT_OPTIONS)[self.dropout.value()]
        crop = dict(CROP_OPTIONS)[self.crop.value()]
        main = (f"{'双面' if self.duplex.value() == '1' else '单面'} · {self.dpi.value()} DPI · "
                f"{'灰度' if self.color.value() == 'gray' else '彩色'}")
        sub = (f"双张检测{'开' if self.double_feed.isChecked() else '关'} · 彩色印刷：{dropout} · "
               f"{SCAN_PARAM_LABELS['crop']}：{crop}")
        self.param_summary_text.setText(main)
        self.param_summary_sub.setText(sub)
        self.param_summary_note.setVisible(self.dpi.value() == "600")

    def _open_params(self) -> None:
        """浮层贴在右栏左侧、与“修改扫描参数…”大致同高；点浮层外任意处关闭。"""
        pop = self.param_pop
        pop.adjustSize()
        right = self.round_panel.mapToGlobal(QPoint(0, 0))
        btn = self.param_btn.mapToGlobal(QPoint(0, 0))
        x, y = right.x() - pop.width() - 8, max(btn.y() - 40, right.y() + 8)
        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            x = max(screen.left(), x)
            y = max(screen.top(), min(y, screen.bottom() - pop.height()))
        pop.move(x, y)
        pop.show()
        pop.activateWindow()
        # 键盘用户：焦点进浮层（第一个参数的当前选项）；浮层开着时空格作用在参数上，不会暂停/继续
        self.duplex.buttons[self.duplex.value()].setFocus(Qt.FocusReason.PopupFocusReason)

    def eventFilter(self, obj, e) -> bool:  # noqa: N802 — Qt 方法名
        if obj is self.param_pop and e.type() == QEvent.Type.Hide and self.param_btn.isVisible():
            self.param_btn.setFocus(Qt.FocusReason.PopupFocusReason)  # 浮层关闭（Esc / 点外面）后焦点回到“修改扫描参数…”
        return super().eventFilter(obj, e)

    @staticmethod
    def _field(label: str, widget: QWidget, fill: bool = False, help: str | None = None) -> QHBoxLayout:
        """左标签右控件；fill=True 时控件占满剩余宽度（用于可能很长、需要省略的文字）；help：标签后的“?”说明。"""
        row = QHBoxLayout()
        row.setSpacing(12)
        lab = QLabel(label)
        theme.set_style(lab, "QLabel { color:$INK2; font-size:13px; } QLabel:disabled { color:$DISABLED; }")
        row.addWidget(lab)
        if help:
            row.addWidget(HelpButton(help))
        if fill:
            row.addWidget(widget, 1)
        else:
            row.addStretch(1)
            row.addWidget(widget)
        return row

    # ---------- 考试 / 批次 ----------
    def set_exam(self, exam: Exam) -> bool:
        """切换到某个考试：下次点“开始识别”时在该考试下新建批次。"""
        if self.ctrl.session and self.ctrl.state in BUSY_STATES:
            raise RuntimeError("当前批次还在进行中")
        if not self._confirm_leave_batch("切换考试"):
            return False
        try:
            self.ctrl.close_batch()
        except RuntimeError as e:
            QMessageBox.warning(self, "提示", str(e))
            return False
        self.exam = exam
        self._closed_note = None
        self._reset_view()
        self.template_label.setText(exam.template_label)
        self.out_label.setText(str(exam.directory))
        self._refresh_guidance()
        return True

    def open_batch(self, exam: Exam, batch_dir: str | Path) -> bool:
        """继续已有批次：恢复拼图与异常列表。"""
        try:
            s = self.ctrl.open_batch(batch_dir, self.settings.ocr_config())
        except Exception as e:  # noqa: BLE001 — RuntimeError 和其他异常
            QMessageBox.warning(self, "提示", f"无法打开批次：{type(e).__name__}: {e}")
            if self.ctrl.session is None:
                self._reset_view()  # 目标就是当前批次时它已被关闭：视图同步为“未开始批次”
            return False
        self.exam = exam
        self._closed_note = None
        self._reset_view()
        self.template_label.setText(exam.template_label)
        self.out_label.setText(str(exam.directory))
        for seq, item in sorted(s.sheets.items()):
            st = item.get("status")
            ev = SheetEvent(seq, item.get("source", ""), st == "ok", item.get("physical"), item.get("message", ""),
                            item.get("exam_no"), int(item.get("warnings", 0)), False,
                            str(s.batch_dir / item["result"]) if item.get("result") else None)
            self._on_done(ev, animate=False)
            if st == "voided":
                self._on_voided(seq)
        self._update_batch_label()
        self._show_threshold_issues(s.threshold_issues)
        self._refresh_guidance()
        return True

    def _pending_exceptions(self) -> int:
        return sum(1 for e in self.events.values() if not e.ok and e.seq not in self._voided)

    def _confirm_leave_batch(self, action: str) -> bool:
        """关闭含未处置异常卡的批次前确认（批次保持未结束，之后可以从考试页继续）。"""
        s, pending = self.ctrl.session, self._pending_exceptions()
        if s is None or not pending:
            return True
        return QMessageBox.question(
            self, action, f"批次 {s.batch_id} 还有 {pending} 张异常卡未处理，批次尚未结束。\n"
                          f"{action}后它保持未结束状态，之后可在考试页“继续扫描”回来处理。确定{action}？"
        ) == QMessageBox.StandardButton.Yes

    def _ask_finalized_action(self) -> str | None:
        """当前批次已结束时再导入：返回 "append"（追加到本批次）/ "new"（新建批次）/ None（取消）。"""
        box = QMessageBox(QMessageBox.Icon.Question, "批次已结束",
                          "当前批次已经结束。新导入的答卷放到哪里？", parent=self)
        box.setInformativeText("追加到本批次：批次会回到未结束，导入后要再点“再次结束批次”。\n"
                               "新建批次：本批次保持已结束，新答卷进入新的批次。")
        append = box.addButton("追加到本批次", QMessageBox.ButtonRole.AcceptRole)
        new = box.addButton("新建批次", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(new)
        box.exec()
        clicked = box.clickedButton()
        return "append" if clicked is append else "new" if clicked is new else None

    def _batch_choice(self) -> str | None:
        """开始识别前的批次：'ready' 用当前批次，'new' 要新建批次，None 取消。"""
        if self.ctrl.session is not None and self.ctrl.state != FINALIZED:
            return "ready"
        if self.ctrl.session is not None:
            choice = self._ask_finalized_action()
            if choice is None:
                return None
            if choice == "append":
                return "ready"  # 登记新答卷时“已结束”自动失效（manifest 标为未结束）
        if self.exam is None:
            QMessageBox.information(self, "提示", "请先在“考试”页选择或新建考试。")
            return None
        return "new"

    def _create_batch(self, template_dir: Path | None = None) -> bool:
        try:
            self.ctrl.new_batch(self.exam, self.settings.ocr_config(), scanner=self._scanner_label(),
                                student_dir_pattern=self.settings.student_dir_pattern, template_dir=template_dir)
        except Exception as e:  # noqa: BLE001 — 包括 RuntimeError 和其他异常
            QMessageBox.warning(self, "提示", f"无法创建批次：{type(e).__name__}: {e}")
            return False
        self._closed_note = None
        self._reset_view()
        self._update_batch_label()
        return True

    def _ensure_batch(self) -> bool:
        """立即确保有可用批次（新建时在界面线程固定模板快照）；按钮操作用 _prepare_batch。"""
        choice = self._batch_choice()
        if choice is None:
            return False
        return choice == "ready" or self._create_batch()

    def _prepare_batch(self, then) -> None:
        """确保有可用批次后在界面线程调用 then()。要新建批次时，固定模板快照（要解码底图）放到后台，
        期间主按钮显示“正在准备批次…”且不可点。"""
        choice = self._batch_choice()
        if choice is None:
            return
        if choice == "ready":
            then()
            return
        exam, session = self.exam, self.ctrl.session
        staged = copy.copy(exam)  # 后台线程只改副本，界面线程用的考试对象不被并发修改
        self._set_preparing(True)

        def ready(template_dir: Path) -> None:
            self._set_preparing(False)
            if self.exam is not exam or self.ctrl.session is not session:
                return  # 准备期间换了考试或批次：不再按旧的继续
            exam.template_dir, exam.template_name = staged.template_dir, staged.template_name
            exam.template_version, exam.template_source = staged.template_version, staged.template_source
            if self._create_batch(template_dir):
                then()

        def failed(e: BaseException) -> None:
            self._set_preparing(False)
            if self.exam is not exam or self.ctrl.session is not session:
                return  # 准备期间换了考试或批次：旧考试的失败不再提示
            QMessageBox.warning(self, "提示", f"无法创建批次：{type(e).__name__}: {e}")

        self._batch_prep.submit(lambda: ensure_exam_snapshot(staged), ready, failed)

    def _set_preparing(self, preparing: bool) -> None:
        self._preparing = preparing
        if preparing:
            self.main_btn.setText("正在准备批次…")
            self.main_btn.setEnabled(False)
        else:
            self._on_state(self.ctrl.state)

    def new_batch(self) -> None:
        """另起一批：关闭当前批次（之后可在考试页“继续扫描”找回），下次点“开始识别”时才新建批次。"""
        if self.ctrl.state in BUSY_STATES:
            QMessageBox.information(self, "提示", self._busy_hint("另起一批") or "当前批次正在处理，请稍后再另起一批。")
            return
        if not self._confirm_leave_batch("另起一批"):
            return
        old = self.ctrl.session.batch_id if self.ctrl.session is not None else None
        try:
            self.ctrl.close_batch()
        except RuntimeError as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        self._closed_note = (f"已关闭批次 {old}，可在考试页“继续扫描”找回；点“开始识别”会新建一批。"
                             if old else None)
        self._reset_view()

    def _reset_view(self) -> None:
        self.events.clear()
        self._voided.clear()
        self.done_times.clear()
        self.started_at = None
        self.ocr = OcrProgress(0, 0, 0)
        self.mosaic.reset()
        self.mosaic.expanded = False
        self.expand_btn.setText("展开全部")
        self.expand_btn.setVisible(False)
        for i in reversed(range(self.exc_box.count() - 1)):
            w = self.exc_box.itemAt(i).widget()
            if w:
                w.deleteLater()
                self.exc_box.removeWidget(w)
        for i in reversed(range(self.void_box.count())):
            w = self.void_box.itemAt(i).widget()
            if w:
                w.deleteLater()
                self.void_box.removeWidget(w)
        self._run_note, self._skipped = None, []
        self._round_count = None
        self._refresh_round()
        self.notice.setVisible(False)
        self.finish_result.setVisible(False)
        self.preview.clear()
        for v in self.readout_values.values():
            v.setText("—")
        self._refresh_counts()
        self._update_batch_label()

    def _update_batch_label(self) -> None:
        s = self.ctrl.session
        self.batch_label.setText(s.batch_id if s else "未开始批次")
        if s:
            ex = self.exam.info if self.exam else None
            template = s.manifest["template"]
            label = f"{template['name']}（v{template['version']}）"
            self.template_label.setText(label)
            self.meta_label.setText(f"模板 {label}" + (f" · {ex.name} {ex.subject}".rstrip() if ex else ""))
        else:
            if self.exam:
                self.template_label.setText(self.exam.template_label)
            self.meta_label.setText("")

    # ---------- 操作 ----------
    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择答卷文件夹", self.folder_edit.text() or str(Path.home()))
        if d:
            self.folder_edit.setText(d)

    def _pick_input_file(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "选择填写好的答卷（可多选，双面请同时选正反面）", "",
                                                DOCUMENT_FILTER)
        if paths:
            self._set_picked(paths)

    def _set_picked(self, paths: list[str]) -> None:
        self._picked = list(paths)
        self._picked_text = paths[0] if len(paths) == 1 else "；".join(Path(p).name for p in paths)
        self.folder_edit.setText(self._picked_text)
        self._on_input_text(self._picked_text)

    def _on_input_text(self, text: str) -> None:
        if self._picked and text.strip() == self._picked_text:
            self.folder_edit.setToolTip("已选择：\n" + "\n".join(self._picked))
        else:
            self.folder_edit.setToolTip(FOLDER_EDIT_TIP)
        self._refresh_guidance()

    def _input_value(self) -> str | list[str]:
        """输入框里的输入：多选的文件（文字未被改动时），否则为输入框文字（文件或文件夹路径）。"""
        text = self.folder_edit.text().strip()
        if self._picked and text == self._picked_text:
            return list(self._picked)
        return text

    def start_folder(self, folder: str | Path | list) -> None:
        """兼容旧入口名称；可输入图片、PDF 文件、答卷目录，或多个文件。"""
        if not folder or (isinstance(folder, str) and not folder.strip()):
            QMessageBox.information(self, "提示", "请先选择要导入的图片、PDF 或文件夹（“文件…”可多选）。")
            return
        try:
            # 面数在创建批次后确定；这里先核验路径，避免空目录也创建批次。
            DocumentSource(folder, 1)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        self._prepare_batch(lambda: self._run_folder(folder))

    def _run_folder(self, folder: str | Path | list) -> None:
        if self.ctrl.scanning:
            return  # 重复点击：命令已排队或正在运行
        self.started_at = self.started_at or time.monotonic()
        try:
            source = DocumentSource(folder, self.ctrl.session.faces)
            self.ctrl.start(source)
        except Exception as e:  # noqa: BLE001 — RuntimeError 和读取文件夹时的其他异常
            QMessageBox.warning(self, "提示", f"无法开始识别：{type(e).__name__}: {e}")
            return
        self._run_note, self._skipped = source.pairing_note, []
        self._begin_round()
        self.notice.setVisible(False)
        self._refresh_run_notice()

    def _main_clicked(self) -> None:
        st = self.ctrl.state
        if st == RUNNING:
            self.ctrl.pause()
        elif st == PAUSED:
            self.notice.setVisible(False)
            self.ctrl.resume()
        elif self.using_scanner():
            self.start_scanner()
        else:
            self.start_folder(self._input_value())

    # ---------- 扫描仪 ----------
    def using_scanner(self) -> bool:
        return self._twain_ok and self.src_seg.value() == "scanner"

    def _stop_text(self) -> str:
        """“停止”按钮的名字：扫描仪是停止进纸，文件导入是不再读剩下的文件。"""
        return "停止进纸" if self.using_scanner() else "停止导入剩余文件"

    def _on_source_changed(self, value: str) -> None:
        scanner = value == "scanner"
        self.scanner_box.setVisible(scanner)
        self.folder_note.setVisible(not scanner)
        self.folder_box.setVisible(not scanner)
        self.param_summary.setVisible(scanner)  # 文件导入用不到扫描参数
        if not scanner:
            self.param_pop.hide()
        for wdg in (*self._scan_params, *self._param_labels):
            wdg.setEnabled(scanner)
            wdg.setToolTip("" if scanner else "扫描仪参数，文件导入时不适用")
        extra = "" if scanner else NOT_FOR_FILES
        for key, seg in (("duplex", self.duplex), ("dpi", self.dpi), ("color", self.color),
                         ("dropout", self.dropout), ("crop", self.crop)):
            for value, button in seg.buttons.items():
                button.setToolTip(PARAM_TIPS[key][value] + extra)
        self.double_feed.setToolTip(DOUBLE_FEED_TIP + extra)
        self._refresh_guidance()

    def _refresh_scanners(self) -> None:
        names = list_scanners()
        self.scanner_combo.clear()
        self.scanner_combo.addItems(names)
        if not names:
            from ...source import twain_source
            self.scanner_combo.setPlaceholderText("未找到扫描仪")
            self.scanner_combo.setToolTip("没找到扫描仪：" + (twain_source.last_error or "请检查扫描仪电源、USB 连接和驱动")
                                          + "；接好后点“刷新”")
            self._refresh_guidance()
            return
        self.scanner_combo.setToolTip("选择要用的扫描仪；换了扫描仪或刚接上时点“刷新”")
        last = self.settings.scan.scanner
        if last in names:
            self.scanner_combo.setCurrentText(last)
        self._refresh_guidance()

    def _scanner_label(self) -> str:
        if self.using_scanner():
            return self.scanner_combo.currentText() or "TWAIN"
        return "folder"

    def scan_config(self) -> TwainConfig:
        return TwainConfig(scanner=self.scanner_combo.currentText(), duplex=self.duplex.value() == "1",
                           dpi=int(self.dpi.value()), color=self.color.value(),
                           double_feed=self.double_feed.isChecked(), dropout=self.dropout.value(),
                           crop=self.crop.value(), paper_mm=self._template_paper())

    def _template_paper(self) -> tuple[float, float] | None:
        """当前批次模板快照的纸张尺寸（mm），供“按模板裁切”使用。"""
        s = self.ctrl.session
        if s is None:
            return None
        try:
            paper = json.loads((s.template_dir / "template.json").read_text(encoding="utf-8"))["paper"]
            return float(paper[0]), float(paper[1])
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None

    def _remember_scan_params(self, cfg: TwainConfig) -> None:
        sd = self.settings.scan
        sd.scanner, sd.duplex, sd.dpi, sd.color, sd.double_feed = (cfg.scanner, cfg.duplex, cfg.dpi, cfg.color,
                                                                   cfg.double_feed)
        sd.dropout, sd.crop = cfg.dropout, cfg.crop
        try:
            save_settings(self.settings)
        except OSError:
            pass  # 记住参数失败不影响扫描

    def apply_scan_defaults(self) -> None:
        defaults = copy.deepcopy(self.settings.scan)
        if self.ctrl.scanning or self.ctrl.state in BUSY_STATES:
            self._pending_defaults = defaults
            return
        self._apply_defaults(defaults)

    def _apply_defaults(self, defaults) -> None:
        self._pending_defaults = None
        self.duplex.set_value("1" if defaults.duplex else "0")
        self.dpi.set_value(str(defaults.dpi))
        self.color.set_value(defaults.color)
        self.double_feed.setChecked(defaults.double_feed)
        self.dropout.set_value(defaults.dropout)
        self.crop.set_value(defaults.crop)
        if self.scanner_combo.findText(defaults.scanner) >= 0:
            self.scanner_combo.setCurrentText(defaults.scanner)

    def start_scanner(self) -> None:
        if not self.scanner_combo.currentText():
            QMessageBox.information(self, "提示", "没有找到扫描仪。请检查扫描仪电源和 USB 连接后点“刷新”。")
            return
        self._prepare_batch(self._run_scanner)

    def _run_scanner(self) -> None:
        if self.ctrl.scanning:
            return
        cfg = self.scan_config()
        self._remember_scan_params(cfg)
        self.notice.setVisible(False)
        self._run_note, self._skipped = None, []
        self.started_at = self.started_at or time.monotonic()
        try:
            self.ctrl.start(TwainSource(cfg, self.ctrl.session.faces))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "提示", f"无法开始扫描：{type(e).__name__}: {e}")
            return
        self._begin_round()

    def _stop_round(self) -> None:
        left = self.ctrl.remaining_inputs()
        self.ctrl.stop()
        if left:
            self._show_notice(f"已停止导入：还有 {left} 份答卷的文件没有导入，批次没有结束。之后再点“开始识别”"
                              "会跳过已导入的答卷，接着导入剩余文件。", "info")

    def _busy_hint(self, action: str) -> str | None:
        """当前状态下不能做 action 时返回提示（说明该怎么做），可以做时返回 None。"""
        st = self.ctrl.state
        if st in (RUNNING, PAUSED):
            return f"正在识别（含暂停）：请先点“{self._stop_text()}”，等它停下后再{action}。"
        if st in REWORK_STATES:
            return f"正在{STATE_TEXT[st][:-1]}另一张卡，请等它完成后再{action}。"
        if st == STOPPING:
            return f"正在停止并等待设备释放，请稍后再{action}。"
        if st == FINISHING:
            return f"正在结束批次，请稍后再{action}。"
        if self.ctrl.scanning:
            return f"当前批次正在处理，请稍后再{action}。"
        return None

    def _refresh_run_notice(self) -> None:
        """本轮的配对提示与重复导入提示合并显示。"""
        parts = [self._run_note] if self._run_note else []
        if self._skipped:
            same = "、".join(f"#{seq:04d}" for seq in sorted(set(self._skipped))[:5])
            more = "…" if len(set(self._skipped)) > 5 else ""
            parts.append(f"跳过 {len(self._skipped)} 份已导入的答卷（与本批次 {same}{more} 相同），不会重复识别。")
        if parts:
            self._show_notice("\n".join(parts), "warn")

    def _on_device(self, e: DeviceNotice) -> None:
        title = {"double_feed": "双张", "paper_jam": "卡纸", "no_paper": "进纸器已空", "error": "扫描仪错误",
                 "notice": "提示", "cancelled": "采集已暂停", "input_overlap": "页面重复"}.get(e.kind, e.kind)
        level = ("error" if e.kind in ("double_feed", "paper_jam", "error")
                 else "warn" if e.kind == "input_overlap" else "info")
        self._show_notice(f"{title}：{DEVICE_TEXT.get(e.kind, e.message)}", level)

    def _show_threshold_issues(self, issues: list[dict]) -> None:
        """涂卡质量提示：白话说明；原文（带涂黑度等数字）放进悬停说明。"""
        if issues:
            self._show_notice("涂卡质量提示：" + "；".join(
                QUALITY_TEXT.get(i.get("code", ""), i.get("msg", i.get("code", ""))) for i in issues), "warn",
                tip="\n".join(str(i.get("msg", "")) for i in issues if i.get("msg")))

    def _show_notice(self, text: str, level: str = "info", tip: str = "") -> None:
        color, bg = {"error": ("ERR", "ERR_BG"), "warn": ("WARN", "WARN_BG")}.get(level, ("INK2", "NOTICE"))
        self.notice.setText(text)
        self.notice.setToolTip(tip)
        theme.set_style(self.notice, f"#deviceNotice {{ color:${color}; background:${bg}; padding:8px 12px;"
                                    f" border-left:3px solid ${color}; font-size:13px; }}")
        self.notice.setVisible(True)

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Space and self.ctrl.state in (RUNNING, PAUSED):
            self._main_clicked()
        else:
            super().keyPressEvent(e)

    def finalize(self) -> None:
        if self.ctrl.session is None:
            return
        pending = self._pending_exceptions()
        if pending:
            QMessageBox.information(self, "提示", f"还有 {pending} 张异常卡未处理（重扫或作废）。")
            return
        left = self.ctrl.remaining_inputs() if self.ctrl.state == PAUSED else None
        if left and QMessageBox.question(
                self, "结束批次", f"还有 {left} 份答卷的文件没有导入，结束批次后不会识别它们。\n"
                                  "要继续导入请点“取消”后点“继续”。确定结束批次？") != QMessageBox.StandardButton.Yes:
            return
        self.notice.setVisible(False)
        try:
            self.ctrl.finalize()
        except RuntimeError as e:
            QMessageBox.information(self, "提示", str(e))

    def _rescan(self, seq: int) -> None:
        hint = self._busy_hint("重扫")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        if self.using_scanner():
            if QMessageBox.question(self, "重扫", f"把 #{seq:04d} 放进进纸器，然后点“是”开始扫描。") \
                    != QMessageBox.StandardButton.Yes:
                return
            try:
                self.ctrl.rescan_scanner(seq, TwainSource(self.scan_config(), self.ctrl.session.faces,
                                                          single_sheet=True))
            except (RuntimeError, ValueError) as e:
                QMessageBox.information(self, "提示", str(e))
            return
        start = self._picked[0] if self._picked else self.folder_edit.text()
        files, _ = QFileDialog.getOpenFileNames(self, f"选择 #{seq:04d} 重扫后的图片（正反面）或 PDF", start,
                                                DOCUMENT_FILTER)
        if files:
            try:
                self.ctrl.rescan(seq, sorted(files))
            except (RuntimeError, ValueError) as e:
                QMessageBox.information(self, "提示", str(e))

    def _raw_paths(self, seq: int) -> list[Path] | None:
        """这张卡保存在 raw/ 的完整原图；记录不完整或文件缺失时返回 None。"""
        s = self.ctrl.session
        if s is None:
            return None
        item = s.sheets.get(seq, {})
        pages = item.get("input_pages") or []
        if not pages or len(pages) != len(item.get("files", [])):
            return None
        try:
            paths = [s.batch_dir / o["raw"] for o in pages]
        except (KeyError, TypeError):
            return None
        return paths if all(p.is_file() for p in paths) else None

    def _reprocess(self, seq: int) -> None:
        hint = self._busy_hint("重新识别")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        if self._raw_paths(seq) is None:
            QMessageBox.information(self, "提示", f"#{seq:04d} 的原图不完整，只能重扫。")
            return
        try:
            self.ctrl.reprocess(seq)
        except RuntimeError as e:
            QMessageBox.information(self, "提示", str(e))

    def _confirm_identity(self, seq: int) -> None:
        hint = self._busy_hint("重新识别")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        if self._raw_paths(seq) is None:
            QMessageBox.information(self, "提示", f"#{seq:04d} 的原图不完整，只能重扫。")
            return
        if not self._ask_not_wrong_card(seq):
            return
        try:
            self.ctrl.reprocess(seq, confirm_identity=True)
        except RuntimeError as e:
            QMessageBox.information(self, "提示", str(e))

    def _ask_not_wrong_card(self, seq: int) -> bool:
        answer = QMessageBox.question(
            self, "确认不是混卡",
            f"#{seq:04d} 卡面上的科目名与模板对不上。请先核对原图：确实是本场考试、本科目的答题卡吗？\n\n"
            "确认后不再比对卡面上的科目名，按本模板重新识别，结果会带“操作员确认”警告。"
            "混入的其他科目答题卡请作废后另行处理。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _relocate(self, seq: int) -> None:
        hint = self._busy_hint("手动定位")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        s = self.ctrl.session
        if s is None:
            return
        from ...imageio import imread_gray
        from ...template import load_template
        paths = self._raw_paths(seq)
        if paths is None:
            QMessageBox.information(self, "提示", f"#{seq:04d} 的原图不完整，只能重扫。")
            return

        def load() -> tuple:
            # 模板底图与原图都要解码，放到后台，不卡界面
            t, bases = load_template(s.template_dir)
            try:
                images = [imread_gray(p) for p in paths]
            except OSError as e:  # 原图的问题不报成“模板读不了”
                raise RuntimeError(f"#{seq:04d} 的原图无法读取（{e}），只能重扫") from e
            if any(img is None for img in images):
                raise RuntimeError(f"#{seq:04d} 的原图无法读取，只能重扫")
            return t, bases, images

        def failed(e: BaseException) -> None:
            if isinstance(e, (OSError, ValueError)):
                QMessageBox.warning(self, "提示", f"无法读取批次模板：{e}")
            else:
                QMessageBox.warning(self, "提示", f"无法打开手动定位：{e}")

        self._relocate_loader.submit(load, lambda loaded: self._open_relocate(seq, s, paths, *loaded), failed)

    def _open_relocate(self, seq: int, session, paths: list[Path], t, bases, images) -> None:
        from ..widgets.manual_align import ManualAlignDialog
        if self.ctrl.session is not session or seq in self._voided:
            return  # 读取期间换了批次，或这张卡已作废
        hint = self._busy_hint("手动定位")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        e = self.events.get(seq)
        confirm = bool(e and e.physical == "WRONG_CARD")  # 混卡卡片上的手动定位：先确认不是混卡
        if confirm and not self._ask_not_wrong_card(seq):
            return
        dlg = ManualAlignDialog(seq, t, bases, paths, self, images=images)
        if dlg.exec():
            try:
                self.ctrl.relocate(seq, dlg.result_value(), confirm_identity=confirm)
            except RuntimeError as e:
                QMessageBox.information(self, "提示", str(e))

    def _void(self, seq: int) -> None:
        hint = self._busy_hint("作废")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        if QMessageBox.question(self, "作废", f"确定作废 #{seq:04d}？作废后不会输出结果。\n"
                                            "之后可以在左栏“已作废”里撤销作废或重扫。") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.ctrl.void(seq)
        except RuntimeError as e:
            QMessageBox.information(self, "提示", str(e))

    def _restore(self, seq: int) -> None:
        hint = self._busy_hint("撤销作废")
        if hint:
            QMessageBox.information(self, "提示", hint)
            return
        try:
            self.ctrl.unvoid(seq)
        except RuntimeError as e:
            QMessageBox.information(self, "提示", str(e))

    def _open_cell(self, seq: int) -> None:
        if self.ctrl.session is not None:
            self.openResult.emit(str(self.ctrl.session.batch_dir), seq)

    def _toggle_mosaic_expand(self) -> None:
        """切换拼图展开/折叠。"""
        self.mosaic.expanded = not self.mosaic.expanded
        self.expand_btn.setText("只看最近 10 行" if self.mosaic.expanded else "展开全部")
        self.mosaic.updateGeometry()
        self.mosaic.update()

    # ---------- 信号处理 ----------
    def _begin_round(self) -> None:
        self._round_count = 0
        self.finish_result.setVisible(False)  # 又导入了答卷：上次结束批次的结果已不是现在的情况
        self._refresh_round()

    def _refresh_round(self) -> None:
        self.round_label.setText("还没开始导入或扫描" if self._round_count is None else f"这次已读入 {self._round_count} 张")

    def _on_queued(self, seq: int, source: str) -> None:
        if self._round_count is not None and self.ctrl.state in (RUNNING, PAUSED, STOPPING):
            self._round_count += 1  # 重扫 / 重新识别不算本轮读入
            self._refresh_round()
        self.mosaic.set_cell(seq, QUEUED)
        self.progress_label.setText(f"{len(self.mosaic.cells)} 张")
        self.expand_btn.setVisible(self.mosaic.total_slots() > 1000)

    def _on_done(self, e: SheetEvent, animate: bool = True) -> None:
        self.events[e.seq] = e
        if (e.rescan and not e.ok and self.ctrl.session is not None
                and self.ctrl.session.sheets.get(e.seq, {}).get("status") == "voided"):
            self._on_voided(e.seq)
            self._show_notice(e.message, "warn")
            return
        self._voided.discard(e.seq)
        self._remove_exception(e.seq)
        if e.ok:
            self.mosaic.set_cell(e.seq, WARN if e.warnings else OK, f"{e.warnings} 条警告" if e.warnings else "")
            if animate:
                self.done_times.append(time.monotonic())
                self._show_latest(e)
        else:
            self.mosaic.set_cell(e.seq, ERR, PHYSICAL_TEXT.get(e.physical or "", e.physical or ""))
            self._add_exception(e.seq, e.physical or "", e.message)
            if animate:
                self.done_times.append(time.monotonic())
            if e.rescan:
                self._show_notice(e.message, "warn")  # 重扫 / 手动定位 / 重新识别失败：旧记录已保留
        self.mosaic.current = None
        self._refresh_counts()

    def _add_exception(self, seq: int, code: str, message: str, voided: bool = False) -> None:
        item = ExceptionItem(seq, code, message, self._raw_thumb(seq),
                             raw_ok=self._raw_paths(seq) is not None, voided=voided)
        item.rescanClicked.connect(self._rescan)
        item.reprocessClicked.connect(self._reprocess)
        item.confirmIdentityClicked.connect(self._confirm_identity)
        item.relocateClicked.connect(self._relocate)
        item.voidClicked.connect(self._void)
        item.restoreClicked.connect(self._restore)
        if voided:
            self.void_box.addWidget(item)
        else:
            self.exc_box.insertWidget(self.exc_box.count() - 1, item)

    def _on_voided(self, seq: int) -> None:
        self._voided.add(seq)
        self._remove_exception(seq)
        e = self.events.get(seq)
        self._add_exception(seq, (e.physical if e else None) or "", e.message if e else "", voided=True)
        self.mosaic.set_cell(seq, VOID, "已作废")
        self._refresh_counts()

    def _on_unvoided(self, seq: int) -> None:
        s = self.ctrl.session
        item = s.sheets.get(seq, {}) if s is not None else {}
        old = self.events.get(seq)
        e = SheetEvent(seq, item.get("source", old.source if old else ""), False,
                       item.get("physical") or (old.physical if old else None),
                       item.get("message") or (old.message if old else ""), None, 0, False)
        self._on_done(e, animate=False)
        self._show_notice(f"#{seq:04d} 已撤销作废，变回异常卡；结束批次前需要重扫、重新识别或再作废。", "info")

    def _on_skipped(self, e: SheetSkipped) -> None:
        self._skipped.append(e.existing_seq)
        self._refresh_run_notice()

    def _on_finalized(self, e: FinalizedEvent) -> None:
        """结束批次的结果写在左栏“结束批次”区（白话，不显示阈值）；“最近一张”的标题不动。"""
        self._finish_ok = e.completed
        if e.completed:
            self._show_threshold_issues(e.issues)
            changed = f"有 {e.changed} 张卡的答案变了" if e.changed else "没有卡的答案变化"
            text = f"批次已结束。按整批情况重新判定后，{changed}。下一步：到结果页核对，再检查交付。"
            level = "SUCCESS"
        else:
            # 涂卡质量提示不是挡住结束批次的原因：另起一行单独说
            quality = [i for i in e.issues if i.get("code") in QUALITY_TEXT]
            reasons = [self._unfinished_reason(i, e) for i in e.issues if i.get("code") not in QUALITY_TEXT]
            text = "批次没能结束：" + ("；".join(r for r in reasons if r) or "请重试")
            if quality:
                text += "\n另外，涂卡质量提示：" + "；".join(QUALITY_TEXT[i["code"]] for i in quality)
            self._show_notice(text, "error", tip="\n".join(str(i.get("msg", "")) for i in e.issues))
            parts = []
            if e.blocked_seqs:
                parts.append(f"还有 {len(e.blocked_seqs)} 份答卷没处理")
            if e.failed_seqs:
                parts.append(f"{len(e.failed_seqs)} 份重新判定失败")
            text = "批次没能结束" + ("：" + "，".join(parts) if parts else "") + "。处理后再点“结束批次”。"
            level = "ERR"
        self.finish_result.setText(text)
        self.finish_result.setToolTip(f"已涂 / 未涂的界线：{e.mark:.3f}（涂黑程度 0～1）" if e.completed else "")
        theme.set_style(self.finish_result, f"#finishResult {{ color:${level}; font-size:13px; }}")
        self.finish_result.setVisible(True)
        self._update_batch_label()
        self._refresh_guidance()

    def _unfinished_reason(self, issue: dict, e: FinalizedEvent) -> str:
        """结束批次没完成的原因：按代码换成现在界面上的说法（原文提到旧按钮名）。

        NOT_SETTLED 只在两种情况下出现（runner.finalize）：采集仍在进行，或批次里没有答卷；
        按批次里有没有答卷区分，不靠消息里的字眼。"""
        code, msg = issue.get("code"), str(issue.get("msg", ""))
        if code == "BATCH_CHANGED":
            return "结束批次期间批次内容有变化，请再点一次“结束批次”"
        if code == "UNRESOLVED_SHEETS":
            return f"还有 {len(e.blocked_seqs)} 份答卷没处理（重扫、重新识别或作废）"
        if code == "REDECISION_FAILED":
            return f"有 {len(e.failed_seqs)} 份重新判定失败，可以再点一次“结束批次”重试"
        if code == "NOT_SETTLED":
            s = self.ctrl.session
            if s is None or not s.sheets:
                return "批次里还没有答卷，不能结束"
            return "还在读入答卷：先暂停或停止，再结束批次"
        return msg

    def _on_ocr(self, p: OcrProgress) -> None:
        self.ocr = p
        self._refresh_counts()

    def _on_state(self, state: str) -> None:
        if state not in BUSY_STATES and self._pending_defaults is not None:
            self._apply_defaults(self._pending_defaults)
        colors = {RUNNING: "SUCCESS", PAUSED: "WARN", FINISHING: "ACCENT", FINALIZED: "INK3", IDLE: "INK3",
                  RESCANNING: "ACCENT", RELOCATING: "ACCENT", REPROCESSING: "ACCENT", STOPPING: "WARN"}
        self.state_chip.setText(f"● {STATE_TEXT.get(state, state)}")
        self.state_chip.setToolTip("正在排空已接收页面并等待设备释放" if state == STOPPING else "")
        theme.set_style(self.state_chip, f"color:${colors.get(state, 'INK3')}; font-weight:600; font-size:13px;"
                                        "border:1px solid $LINE2; border-radius:2px; padding:1px 8px;")
        self.state_chip.setFixedHeight(24)
        self.main_btn.setText({RUNNING: "暂停", PAUSED: "继续"}.get(state, "开始识别"))
        self.main_btn.setObjectName("bigHold" if state == RUNNING else "big")
        self.main_btn.style().unpolish(self.main_btn)
        self.main_btn.style().polish(self.main_btn)
        self.main_btn.setEnabled(state not in (FINISHING, STOPPING, *REWORK_STATES))
        if self._preparing:  # 准备新批次期间：主按钮保持不可点
            self.main_btn.setText("正在准备批次…")
            self.main_btn.setEnabled(False)
        # 忙碌时不能切换输入源：切换后“停止”“重扫”会按错误的输入源工作
        self.src_seg.setEnabled(state not in BUSY_STATES)
        self.finish_btn.setText("再次结束批次" if state == FINALIZED else "结束批次")
        self.finish_btn.setEnabled(self._finish_allowed())
        self.stop_btn.setEnabled(state in (RUNNING, PAUSED))
        self.new_btn.setEnabled(state in (IDLE, FINALIZED))
        self._refresh_guidance()

    # ---------- 显示 ----------
    def _remove_exception(self, seq: int) -> None:
        for box in (self.exc_box, self.void_box):
            for i in range(box.count()):
                w = box.itemAt(i).widget()
                if isinstance(w, ExceptionItem) and w.seq == seq:
                    box.removeWidget(w)
                    w.deleteLater()
                    break

    def _raw_thumb(self, seq: int) -> Path | None:
        s = self.ctrl.session
        if not s:
            return None
        raws = sorted((s.batch_dir / "raw").glob(f"{seq:04d}_*"))
        return raws[0] if raws else None

    def _show_latest(self, e: SheetEvent) -> None:
        """预览最近一张；result.json 缺失、损坏或结构不对时跳过预览（计数照常刷新）。"""
        if not e.result:
            return
        try:
            self._show_latest_result(e, Path(e.result))
        except Exception:  # noqa: BLE001 — 预览失败不影响识别流程
            return

    def _show_latest_result(self, e: SheetEvent, p: Path) -> None:
        r = json.loads(p.read_text(encoding="utf-8"))
        self.prev_title.setText(f"#{e.seq:04d}　最近一张，正面")
        self.preview.load_image(p.parent / r["pages"].get("front", ""))  # 扫描中每张都会刷新：后台解码，不卡界面
        st = r["student"]
        exam = st.get("exam_no") or {}
        self.readout_values["exam"].setText(exam.get("value") or "—")
        bc = st.get("barcodes", {}).get("student_barcode") or {}
        bc_text = {"ok": "已识别", "failed": "未识别"}.get(bc.get("status"), "—")
        if st.get("consistent") is True:
            bc_text = "与考号一致"
        elif st.get("consistent") is False:
            bc_text = "与考号不一致"
        self.readout_values["bar"].setText(bc_text)
        absent = (st.get("absent") or {}).get("value")
        self.readout_values["absent"].setText("已涂" if absent is True else "未涂" if absent is False else "未确定")
        ok = sum(1 for o in r["objective"] if o["status"] == "ok")
        self.readout_values["obj"].setText(f"{ok} / {len(r['objective'])}")
        ocr = {q.get("ocr_status") for q in r["subjective"]}
        self.readout_values["sub"].setText(f"切图 {len(r['subjective'])}" + ("，待转写" if "pending" in ocr else ""))
        f = r["scan"]["faces"].get("front", {})
        missing = f.get("missing_anchors")
        method = ALIGN_TEXT.get(f.get("align_method"), f.get("align_method") or "—") + (
            f"（缺定位块 {'、'.join(map(str, missing))}）" if missing else "")
        self.readout_values["align"].setText(f"{method}，倾斜 {f.get('skew_deg', 0):.1f}°")

    def _refresh_counts(self) -> None:
        evs = self.events.values()
        ok = sum(1 for e in evs if e.ok)
        phys = sum(1 for e in evs if not e.ok and e.seq not in self._voided)
        warns = sum(e.warnings for e in evs if e.ok)
        self.t_scanned.set_value(len(self.events))
        self.t_ok.set_value(ok)
        self.t_err.set_value(phys)
        self.t_warn.set_value(warns)
        self.exc_title.setText(f"异常卡 {phys}")
        warned = [e for e in sorted(evs, key=lambda e: e.seq) if e.ok and e.warnings]
        self.warn_title.setText(f"警告 {warns}")
        self.warn_note.setText(f"{len(warned)} 张卡 · 只标记，不拦截" if warned else "只标记，不拦截")
        self._warning_lines = [f"<b>#{e.seq:04d}</b>　{e.warnings} 条警告" for e in warned[:8]]
        if len(warned) > 8:
            self._warning_lines.append(f"…另有 {len(warned) - 8} 份，在结果页筛选“有警告”查看")
        self.exc_empty.setVisible(phys == 0)
        self.exc_scroll.setVisible(phys > 0)
        voided = len(self._voided)
        self.void_title.setText(f"已作废 {voided}")
        self.void_empty.setVisible(voided == 0)
        self.void_fold.setVisible(voided > 0 or bool(self.events))
        self.exc_empty.setText(EXC_EMPTY_CLEAR if self.events else EXC_EMPTY_IDLE)
        self.refresh_theme()
        p = self.ocr
        if p.total:
            self.t_ocr.set_value(p.done, f"/ {p.total}")
            self.t_ocr.note.setText(f"失败 {p.failed}" if p.failed else "后台进行中" if p.done < p.total else "已完成")
        elif self.ctrl.session is not None and self.ctrl.session.manifest.get("ocr"):
            self.t_ocr.set_value(0, "/ 0")
            self.t_ocr.note.setText("等待中")
        else:
            self.t_ocr.set_value("—", "")
            self.t_ocr.note.setText("未开启")
        self.progress_label.setText(f"{len(self.mosaic.cells)} 张")
        self._refresh_guidance()

    # ---------- 提示与引导 ----------
    def _busy_reason(self, action: str) -> str | None:
        """按钮因状态不能点时的原因（不含“正在识别”这类可以先暂停的情况，由调用方自己判断）。"""
        st = self.ctrl.state
        if st in REWORK_STATES:
            return f"正在{STATE_TEXT[st][:-1]}一张卡，等它完成后再{action}"
        if st == STOPPING:
            return f"正在停止、等已接收的页面识别完，请稍后再{action}"
        if st == FINISHING:
            return f"正在结束批次，请稍后再{action}"
        return None

    def _start_problem(self) -> str | None:
        """“开始识别”前还缺什么；不缺时返回 None。"""
        if self.ctrl.session is None and self.exam is None:
            return "还没选考试：先到“考试”页选择或新建考试"
        if self.using_scanner():
            if not self.scanner_combo.currentText():
                return "没找到扫描仪：检查电源和 USB 连接后点“刷新”"
        elif not self._input_value():
            return "还没选答卷：先点“文件…”或“文件夹…”选择要导入的图片、PDF 或文件夹"
        return None

    def _refresh_guidance(self) -> None:
        """按当前状态更新按钮提示（含不能点的原因）、空状态文字和“下一步”提示。"""
        if not hasattr(self, "next_step"):
            return  # 构建中
        st, s = self.ctrl.state, self.ctrl.session
        pending = self._pending_exceptions()
        scanner = self.using_scanner()
        problem = self._start_problem()
        # 主按钮
        if st == RUNNING:
            self.main_btn.setToolTip("暂停（空格键）：先不读入新的答卷，已读入的会识别完；之后点“继续”接着来")
        elif st == PAUSED:
            self.main_btn.setToolTip("继续（空格键）：接着读入剩下的答卷")
        else:
            desc = ("按右侧的设置开始扫描" if scanner else "导入右侧选择的答卷并开始识别") + \
                   "；还没有批次时会自动新建一个批次"
            if st == FINALIZED:
                desc += "。本批次已结束，再导入时会问你追加到本批次还是新建批次"
            reason = self._busy_reason("开始识别")
            self.main_btn.setToolTip(_tip(desc, reason) if reason else desc + (f"\n先做这一步：{problem}" if problem else ""))
        # 停止导入剩余文件 / 停止进纸（批次不会结束）
        stop = self._stop_text()
        self.stop_btn.setText(stop)
        self.stop_note.setText(STOP_NOTE.format("扫描" if scanner else "导入"))
        stop_desc = ("停止扫描仪进纸（已接收的页面会识别完）。批次不会结束，可以再重扫或继续扫描" if scanner else
                     "不再读取剩余文件（已读入的答卷会识别完）。批次不会结束；之后再点“开始识别”会跳过已导入的答卷，"
                     "接着导入剩余文件")
        self.stop_btn.setToolTip(_tip(stop_desc, None if self.stop_btn.isEnabled() else
                                      self._busy_reason(stop) or "现在没有正在进行的识别，开始识别后才能用"))
        # 另起一批
        new_desc = "关闭当前批次，下次点“开始识别”时新建一个批次；当前批次之后可在考试页“继续扫描”找回"
        new_reason = None
        if not self.new_btn.isEnabled():
            new_reason = (self._busy_reason("另起一批") or
                          f"正在识别（含暂停）：先点“{stop}”，等它停下后再另起一批")
        self.new_btn.setToolTip(_tip(new_desc, new_reason))
        # 结束批次 / 再次结束批次
        if st == FINALIZED:
            fin_desc = "再次结束批次：追加了答卷或改动过异常卡后，按整批的涂黑情况重新判定一遍"
        else:
            fin_desc = "全部扫完、异常卡都处理完后点：按整批的涂黑情况重新判定每张卡，批次结束后才能交付"
        fin_reason = None
        if not self.finish_btn.isEnabled():
            if s is None:
                fin_reason = "还没有批次：先导入或扫描答卷"
            elif st == RUNNING:
                fin_reason = "正在识别：先点“暂停”，或等这一轮识别完"
            else:
                fin_reason = self._busy_reason("结束批次") or "当前状态不能结束批次，请稍后再试"
        elif pending:
            fin_desc += f"\n先做这一步：左栏还有 {pending} 张异常卡没处理（重扫 / 重新识别 / 手动定位 / 作废）"
        self.finish_btn.setToolTip(_tip(fin_desc, fin_reason))
        self._finish_items = self._finish_checks()
        self._render_finish_checks()
        if self._finish_ok and (s is None or not s.manifest.get("finalized")):
            # 恢复作废卡、重扫、重新识别等让批次回到未结束：“批次已结束…”已不再成立
            self._finish_ok = False
            self.finish_result.setVisible(False)
        # 输入源
        busy = st in BUSY_STATES
        folder_btn = self.src_seg.buttons["folder"]
        folder_desc = "从电脑上的图片、PDF 或文件夹导入已经扫好的答卷"
        busy_src = f"识别进行中不能切换输入源，请先点“{stop}”"
        folder_btn.setToolTip(_tip(folder_desc, busy_src if busy else None))
        scanner_btn = self.src_seg.buttons["scanner"]
        if not self._twain_ok:
            scanner_btn.setToolTip(f"用扫描仪直接扫描答卷\n现在不能用：{self._twain_reason}")
        else:
            scanner_btn.setToolTip(_tip("用扫描仪直接扫描答卷，扫完自动识别", busy_src if busy else None))
        self.src_seg.setToolTip(busy_src if busy else "")
        # 拼图空状态 / 用法
        if self.mosaic.cells:
            self.mosaic_hint.setText("每格一张卡：悬停看序号和原因，点击到结果页查看这张卡")
        elif s is not None:
            self.mosaic_hint.setText("批次已打开，还没有答卷。在右侧选好输入后点“开始识别”，识别出的卡会一格一格出现在这里。")
        elif self.exam is None:
            self.mosaic_hint.setText("还没有批次。先到“考试”页选择或新建考试，再回到这里选输入、点“开始识别”。")
        else:
            self.mosaic_hint.setText("还没有批次。在右侧选好输入后点“开始识别”，会自动新建批次；识别出的卡会一格一格出现在这里。")
        self.next_step.setText("<b>下一步：</b>" + self._next_step_text(st, s, pending, problem))

    def _finish_allowed(self) -> bool:
        """“结束批次”能不能点；前置检查清单的状态一项也用它，两者不会不一致。"""
        return self.ctrl.state in (IDLE, PAUSED, FINALIZED) and self.ctrl.session is not None

    def _finish_checks(self) -> list[tuple[bool, str]]:
        """结束批次前置检查：(是否满足, 说明)。未处理的异常卡在点击时拦下（按钮仍可点，与原逻辑一致）；
        状态一项打 ✕ 当且仅当按钮不能点；警告只提示不拦截。"""
        st = self.ctrl.state
        if self.ctrl.session is None:
            return [(False, "还没有批次：先导入或扫描答卷")]
        pending = self._pending_exceptions()
        checks = [(not pending, f"异常卡还有 {pending} 张未处理" if pending else "异常卡都已处理")]
        if self._finish_allowed():
            checks.append((True, {PAUSED: "已暂停：可以结束批次，还有没导入的文件时会先问你",
                                  FINALIZED: "批次已结束；追加了答卷或改过异常卡后要再点一次"}.get(
                st, "没有正在进行的识别")))
        elif st == RUNNING:
            checks.append((False, f"还在识别：先暂停或点“{self._stop_text()}”"))
        else:
            checks.append((False, self._busy_reason("结束批次") or "当前状态不能结束批次，请稍后再试"))
        warns = sum(e.warnings for e in self.events.values() if e.ok)
        checks.append((True, f"警告 {warns} 条不拦截，结束批次后到结果页核对" if warns else "没有警告"))
        return checks

    def _render_finish_checks(self) -> None:
        ok_c, bad_c, dim_c = theme.color("SUCCESS"), theme.color("ERR"), theme.color("INK3")
        rows = "".join(
            f"<tr><td width='16' style='color:{ok_c if ok else bad_c}'>{'✓' if ok else '✕'}</td>"
            f"<td style='color:{dim_c if ok else theme.color('INK')}'>{html.escape(text)}</td></tr>"
            for ok, text in self._finish_items)
        self.finish_checks.setText(f"<table cellspacing='0' cellpadding='1'>{rows}</table>")

    def _render_region_legend(self) -> None:
        items = [(theme.REGION[k], label) for k, label in theme.REGION_LABELS] + [
            (theme.color("ERR"), "异常卡"), (theme.color("WARN"), "有警告")]
        parts = []
        for c, label in items:
            mark = (f"<span style='color:{theme.color('INK2')}'>□</span>" if c == theme.REGION["loc"]
                    else f"<span style='color:{c}'>■</span>")  # 定位点是黑色，深色主题下画成空心框才看得见
            # 折行只在两项之间：项内用不换行空格和连字符（U+2060）连住，中文本来可以在任意字间断行
            parts.append(mark + "&nbsp;" + "⁠".join(label).replace("⁠ ⁠", "&nbsp;"))
        self.region_legend.setText("区域色码　　" + "　　".join(parts))

    def _next_step_text(self, st: str, s, pending: int, problem: str | None) -> str:
        if st == RUNNING:
            return "正在识别。可以随时“暂停”；有红色异常卡先不用管，识别完再到左栏统一处理。"
        if st == PAUSED:
            return f"已暂停。点“继续”接着识别；不想导入剩下的答卷就点“{self._stop_text()}”（批次不会结束）。"
        if st == STOPPING:
            return "正在停止，等已接收的页面识别完。"
        if st == FINISHING:
            return "正在结束批次（按整批情况重新判定），请稍候。"
        if st in REWORK_STATES:
            return f"正在{STATE_TEXT[st][:-1]}一张卡，完成后左栏和拼图会更新。"
        if st == FINALIZED:
            return "批次已结束。到“结果”页核对警告，然后点“检查交付”；还要追加答卷就再点“开始识别”。"
        if s is not None and self.events:
            if pending:
                return (f"这次识别完成，先处理左栏 {pending} 张异常卡（重扫 / 重新识别 / 手动定位 / 作废），"
                        "处理完再点“结束批次”。")
            return "这次识别完成，没有异常卡。还有答卷就继续“开始识别”；都扫完了点“结束批次”。"
        if s is None and self._closed_note:
            return self._closed_note
        if problem:
            return problem + "。"
        if self.using_scanner():
            return "把答卷放进进纸器，点“开始识别”开始扫描。"
        return "点“开始识别”导入选好的答卷。"

    def refresh_theme(self) -> None:
        """Recolor the displayed warning summary without updating scan state."""
        self._render_region_legend()
        if getattr(self, "_finish_items", None) is not None:
            self._render_finish_checks()
        if self._warning_lines is not None:
            self.warn_list.setText("<br>".join(
                text if text.startswith("…") else f"<span style='color:{theme.WARN}'>■</span> {text}"
                for text in self._warning_lines)
                or f"<span style='color:{theme.INK3}'>暂无。多涂、未作答、考号与条码不一致等会列在这里，不影响继续扫描。</span>")

    def _tick(self) -> None:
        now = time.monotonic()
        recent = [t for t in self.done_times if now - t <= 60]
        if len(recent) >= 2:
            span = max(now - recent[0], 1e-3)
            speed = int(round(len(recent) / span * 60))
        else:
            speed = 0
        self.t_speed.set_value(speed)
        self.round_speed.setText(f"速度 {speed} 张/分" if self._round_count is not None else "")
