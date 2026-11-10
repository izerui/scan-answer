"""结果页：左侧学生列表（筛选、搜索），右侧单个学生：卡面叠层、异常、客观题答案表、主观题切图与转写。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRectF, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractSlider, QApplication, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QTableView,
                               QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ...app.results import (PHYSICAL_TEXT, StudentRow, face_overlays, load_batch_template, load_rows,
                            warning_text)
from ...app.settings import AppSettings
from ...ocr.queue import OcrQueue, enable_ocr, fail_unclaimed, reset_failed, skip_pending
from ...output.paths import resolve_path
from ...pipeline.paths import resolve_result_path
from ...template.model import Template, subjective_item_key
from .. import theme
from ..widgets.card_view import CardView, Overlay
from ..widgets.common import ElidedLabel, Segmented, hline, set_empty_text, setup_table
from ..widgets.evidence_preview import EvidencePreview

class _RetrySignals(QObject):
    """后台重试线程 → 界面线程的信号桥。"""
    done = Signal(str, bool, str)  # (owning result or batch, success, message)
    progress = Signal(str, int, int, int)  # (owning result or batch, done, failed, total)


STATUS_TEXT = {"ok": "正常", "blank": "未答", "multiple": "多涂", "uncertain": "存疑", "partial": "部分", "failed": "失败"}
OVERLAY_COLOR = {"answer": theme.REGION["obj"], "bad": theme.PAPER_ERR, "exam": theme.REGION["id"], "exam_bad": theme.PAPER_ERR,
                 "omr": theme.REGION["id"], "choice": theme.REGION["sub"]}


FILTER_TEXT = {"all": "全部", "warn": "有警告", "err": "异常"}


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
        self._data_sig: object = None  # 上次读取的批次数据指纹，用来判断刷新时数据是否变化
        self._retry = _RetrySignals()
        self._retry.done.connect(self._retry_done)
        self._retry.progress.connect(self._retry_progress)
        self._build()

    # ---------- 布局 ----------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        left = QWidget()
        left.setObjectName("panel")
        left.setFixedWidth(296)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        head = QHBoxLayout()
        head.setContentsMargins(16, 12, 16, 8)
        t = QLabel("学生")
        t.setStyleSheet("font-weight:600;")
        head.addWidget(t)
        head.addStretch(1)
        self.count_label = QLabel("")
        self.count_label.setObjectName("muted")
        head.addWidget(self.count_label)
        ll.addLayout(head)
        self.search = QLineEdit()
        self.search.setPlaceholderText("序号、考号或来源")
        self.search.textChanged.connect(self._refill)
        sw = QHBoxLayout()
        sw.setContentsMargins(16, 0, 16, 8)
        sw.addWidget(self.search)
        ll.addLayout(sw)
        self.filter = Segmented([("all", "全部"), ("warn", "有警告"), ("err", "异常")], "all")
        self.filter.changed.connect(lambda _k: self._refill())
        fw = QHBoxLayout()
        fw.setContentsMargins(16, 0, 16, 10)
        fw.addWidget(self.filter)
        fw.addStretch(1)
        ll.addLayout(fw)
        self.model = QStandardItemModel(0, 3, self)
        self.model.setHorizontalHeaderLabels(["序号", "考号", "警告"])
        self.table = QTableView()
        self.table.setModel(self.model)
        setup_table(self.table, stretch=1)
        self.table.setColumnWidth(0, 76)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        set_empty_text(self.table, "尚未打开批次。\n在“考试”页选中批次点“查看结果”，或在扫描页点击拼图格子。")
        self.table.selectionModel().currentRowChanged.connect(lambda cur, _p: self._select_row(cur.row()))
        ll.addWidget(self.table, 1)
        root.addWidget(left)

        right = QWidget()
        right.setObjectName("work")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(0)
        hd = QHBoxLayout()
        hd.setContentsMargins(24, 10, 24, 10)
        self.title = QLabel("未选择批次")
        self.title.setObjectName("h2")
        hd.addWidget(self.title)
        hd.addSpacing(16)
        self.info = ElidedLabel("", Qt.TextElideMode.ElideRight)
        theme.set_style(self.info, "color:$INK2;")
        hd.addWidget(self.info, 1)
        hd.addSpacing(12)
        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setToolTip("重新读取本批次的识别与转写结果")
        self.refresh_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(lambda: self.refresh(announce=True))
        hd.addWidget(self.refresh_btn)
        self.retry_all_btn = QPushButton("补做和重试转写")
        self.retry_all_btn.setEnabled(False)
        self.retry_all_btn.clicked.connect(self._retry_all_failed)
        hd.addWidget(self.retry_all_btn)
        self.skip_btn = QPushButton("放弃未完成转写…")
        self.skip_btn.setEnabled(False)
        self.skip_btn.clicked.connect(self._skip_pending)
        hd.addWidget(self.skip_btn)
        hd.addSpacing(8)
        self.prev_btn = QPushButton("上一份")
        self.next_btn = QPushButton("下一份")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(1))
        for btn, key, d in ((self.prev_btn, Qt.Key.Key_PageUp, -1), (self.next_btn, Qt.Key.Key_PageDown, 1)):
            btn.setToolTip(f"{btn.text()}（{QKeySequence(key).toString(QKeySequence.SequenceFormat.NativeText)}）")
            sc = QShortcut(QKeySequence(key), self)
            sc.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            sc.activated.connect(lambda d=d: self._page_key(d))
        hd.addWidget(self.prev_btn)
        hd.addWidget(self.next_btn)
        rl.addLayout(hd)
        # 操作提示行：转写进度与结果、刷新后选中项变化等（没有内容时隐藏）
        self.notice = QLabel("")
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        self.notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.set_style(self.notice, "color:$INK2; padding:0 24px 8px 24px;")
        self.notice.hide()
        rl.addWidget(self.notice)
        self._delivery_band = QVBoxLayout()
        self._delivery_band.setContentsMargins(24, 0, 24, 0)
        self.delivery_panel = None
        rl.addLayout(self._delivery_band)
        self._attach_delivery_panel()
        rl.addWidget(hline())
        split = QSplitter(Qt.Orientation.Horizontal)
        cardw = QWidget()
        cl = QVBoxLayout(cardw)
        cl.setContentsMargins(24, 12, 12, 12)
        bar = QHBoxLayout()
        lbl = QLabel("叠层：绿=识别选项　蓝=考号 / 填涂字段　橙=选做组　红=异常")
        lbl.setObjectName("muted")
        bar.addWidget(lbl)
        bar.addStretch(1)
        self.face_seg = Segmented([("front", "正面"), ("back", "背面")], "front")
        self.face_seg.changed.connect(self._set_face)
        bar.addWidget(self.face_seg)
        cl.addLayout(bar)
        self.card = CardView(placeholder="在左侧选择学生查看卡面")
        cl.addWidget(self.card, 1)
        self.anomalies = QLabel("")
        self.anomalies.setWordWrap(True)
        self.anomalies.setTextFormat(Qt.TextFormat.RichText)
        self.anomalies.setStyleSheet("font-size:13px;")
        cl.addWidget(self.anomalies)
        self.card_tabs = QTabWidget()
        self.card_tabs.addTab(cardw, "卡面")
        self.evidence = EvidencePreview()
        self.card_tabs.addTab(self.evidence, "证据 / JSON")
        split.addWidget(self.card_tabs)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        oh = QLabel("客观题答案")
        oh.setStyleSheet("font-weight:600; padding:10px 16px;")
        sl.addWidget(oh)
        self.answers = QTableWidget(0, 4)
        self.answers.setHorizontalHeaderLabels(["题号", "答案", "状态", "涂黑度"])
        setup_table(self.answers, stretch=3, row_height=30)
        for column, width in enumerate((68, 80, 64)):
            self.answers.setColumnWidth(column, width)
        self._answers_empty = set_empty_text(self.answers, "")
        sl.addWidget(self.answers, 1)
        sh = QLabel("信息栏与主观题")
        theme.set_style(sh, "font-weight:600; padding:10px 16px; border-top:1px solid $LINE;")
        sl.addWidget(sh)
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
        split.addWidget(side)
        split.setSizes([680, 432])
        rl.addWidget(split, 1)
        root.addWidget(right, 1)
        # 卡面（QGraphicsView）等滚动控件会先吃掉 PgUp/PgDn，窗口级快捷键收不到：在这些控件上直接拦截
        for w in (self.table, self.card, self.answers, self.evidence.view):
            w.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
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
        self._delivery_band.addWidget(self.delivery_panel)

    def _protected_dirs(self) -> list[tuple[Path, str]]:
        """不允许放交付目录的位置：设置中的输出根目录（考试页按目录扫描批次）。"""
        root = getattr(self.settings, "output_root", None) if self.settings else None
        return [(Path(root), "输出根目录")] if root else []

    def _notify(self, text: str) -> None:
        """在页内提示行显示操作结果，并转发给主窗口状态栏。"""
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
        if text:
            self.statusMessage.emit(text)

    def _mark_delivery_stale(self) -> None:
        mark = getattr(self.delivery_panel, "mark_stale", None)
        if mark is not None:
            mark()

    def _signature(self, rows: list[StudentRow]) -> object:
        """manifest 与各 result.json 的修改时间 + 行数据：任何一项变化都说明批次数据变了。"""
        def stamp(path: Path | None):
            try:
                st = path.stat() if path is not None else None
                return (st.st_mtime_ns, st.st_size) if st else None
            except OSError:
                return None
        return (stamp(self.batch_dir / "manifest.json") if self.batch_dir else None,
                [(r, stamp(r.result)) for r in rows])

    def open_batch(self, batch_dir: str | Path, select_seq: int | None = None) -> bool:
        bd = Path(batch_dir)
        try:
            tpl = load_batch_template(bd)
            rows = load_rows(bd)
        except Exception as e:  # noqa: BLE001
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "提示", f"无法打开批次：{type(e).__name__}: {e}")
            return False
        self.batch_dir = bd
        self.tpl = tpl
        self.rows = rows
        self._data_sig = self._signature(rows)
        self._notify("")
        self.face_seg.buttons["back"].setEnabled("back" in self.tpl.faces)
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
        self.batchChanged.emit(str(self.batch_dir))
        return True

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
        set_empty_text(self.table, "没有符合条件的学生" if self.rows else "这个批次还没有识别结果")

    @staticmethod
    def _row_items(r: StudentRow) -> list[QStandardItem]:
        """列表一行的三列（新建行和原地更新共用）。"""
        reason = PHYSICAL_TEXT.get(r.physical or "", r.physical or "")
        exam = r.exam_no or {"physical": f"物理异常 {reason}".strip(), "voided": "已作废",
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
        before = self.notice.text()
        self._reload(None, force=announce)
        self._update_retry_controls()
        if announce and self.notice.text() == before:
            self._notify("已刷新")

    def on_settings_changed(self) -> None:
        """设置保存后（例如配置了大模型转写）重新计算重试按钮。"""
        self._update_buttons()

    def on_scan_state_changed(self) -> None:
        """扫描开始/结束后重新计算按钮（正在扫描的批次不能重试转写）。"""
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._update_retry_controls()
        if self.result is not None:
            self._fill_subjective(self.result)  # 单项转写按钮的可用状态

    def _reload(self, changed: Path | None, force: bool = False) -> bool:
        """重新读取行数据。changed 为某份 result.json 时只在它是当前学生时重画右侧；None 表示整批。

        数据没变且不是强制刷新时不重画；当前学生不再符合筛选时选中相邻一份并给出提示，而不是清空。"""
        try:
            rows = load_rows(self.batch_dir)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            self._notify(f"无法重新读取批次：{e}")
            return False
        sig = self._signature(rows)
        if sig == self._data_sig and not force and changed is None:
            return False
        if sig != self._data_sig:
            self._data_sig = sig
            self._mark_delivery_stale()
        selected = self.current.seq if self.current else None
        before = [r.seq for r in self.visible_rows()]
        self.rows = rows
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
        by_seq = {r.seq: r for r in rows}
        for i in range(self.model.rowCount()):
            row = by_seq.get(self.model.item(i, 0).data(Qt.ItemDataRole.UserRole))
            if row is not None:
                for j, item in enumerate(self._row_items(row)):
                    self.model.setItem(i, j, item)
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

        只有焦点在能滚动的长文本（JSON、交付明细）里时才用于文本翻页，便于阅读。"""
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

    def _select_row(self, i: int) -> None:
        if i < 0 or i >= self.model.rowCount():
            self._clear_detail()
            return
        seq = self.model.item(i, 0).data(Qt.ItemDataRole.UserRole)
        row = next(r for r in self.rows if r.seq == seq)
        self.current = row
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
        self.title.setText("未选择学生")
        self.info.setText("")
        self.card.clear()
        self.evidence.set_result(None)
        self.answers.setRowCount(0)
        self._answers_empty.setText("在左侧选择学生")
        self._warning_texts = None
        self.anomalies.setText("")
        self._fill_subjective(None)

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
            self.info.setText("结果文件损坏" if broken else {"physical": f"物理异常：{reason}　{row.message}",
                                                         "voided": "已作废",
                                                         "broken": "结果文件不可用"}.get(row.status, "识别中"))
            self.card.clear()
            try:
                root = self.batch_dir.resolve()
                raw_dir = resolve_path(root, root, "raw")
                raws = sorted(raw_dir.glob(f"{row.seq:04d}_*"))
                if raws:
                    image = resolve_path(root, root, raws[0].relative_to(root).as_posix())
                    self.card.set_image(image)
            except (ValueError, OSError, RuntimeError):
                self.card.setToolTip("图片路径无效")
            self.answers.setRowCount(0)
            self._answers_empty.setText(
                "结果文件损坏，读不到识别结果。\n请从备份恢复该答卷目录；“检查交付”会列出具体文件。" if broken else
                {"physical": "物理异常，没有识别结果。\n请在扫描页重扫或作废。",
                 "voided": "已作废，没有识别结果",
                 "broken": row.message}.get(row.status, "没有识别结果"))
            self._warning_texts = None
            self.anomalies.setText("")
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
        self.answers.setRowCount(len(objs) + len(chooses))
        for i, o in enumerate(objs):
            values = {k: v for k, v in o.get("fill", {}).items() if isinstance(v, (int, float))}
            fill = max(values.values(), default=0)
            cells = [o["no"], o["answer"] or "—", STATUS_TEXT.get(o["status"], o["status"]),
                     " / ".join(f"{k} {v:.2f}" for k, v in values.items() if v >= 0.15)
                     or (f"{fill:.2f}" if values else "未确定")]
            for j, c in enumerate(cells):
                it = QTableWidgetItem(c)
                it.setToolTip(str(c))
                if o["status"] != "ok":
                    theme.set_item_colors(it, background="ERR_BG", foreground="ERR" if j == 2 else None)
                self.answers.setItem(i, j, it)
        for i, c in enumerate(chooses, start=len(objs)):  # 选做组：每组一行，接在客观题后面
            cells = [f"选做：{c.get('label') or c.get('key', '')}", "、".join(c.get("chosen") or []) or "—",
                     STATUS_TEXT.get(c.get("status"), c.get("status") or ""),
                     " / ".join(f"{k} {v:.2f}" for k, v in (c.get("fill") or {}).items()
                                if isinstance(v, (int, float)) and v >= 0.15) or "未确定"]
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

    # ---------- 重试转写 ----------
    def _retry_block_reason(self) -> str:
        if self.batch_dir is None:
            return "请先选择批次"
        if self._retry_owner is not None:
            return "正在转写，请等本次完成"
        try:
            manifest = json.loads((self.batch_dir / "manifest.json").read_text(encoding="utf-8"))
            if (manifest.get("delivery") or {}).get("sealed"):
                return "交付目录为只读，请在原工作批次重试转写"
        except (OSError, ValueError, AttributeError):
            return "批次信息无法读取，不能重试转写"
        active = self._active_batch_fn() if self._active_batch_fn else None
        if active and Path(active).resolve() == self.batch_dir.resolve():
            return "请在扫描页结束批次后再重试"
        return ""

    def _update_retry_controls(self) -> None:
        reason = self._retry_block_reason()
        ocr_reason = reason or ("" if self.settings and self.settings.ocr else "请先在设置中配置大模型转写")
        self.retry_all_btn.setEnabled(not ocr_reason and bool(self.rows))
        self.retry_all_btn.setToolTip(ocr_reason or "把失败项改回等待并重新转写，未开启转写的项一并补做")
        pending = sum(r.ocr_pending for r in self.rows)
        self.skip_btn.setEnabled(not reason and pending > 0)
        self.skip_btn.setToolTip(reason or (f"把 {pending} 项“等待转写”标为未启用，使批次可以交付" if pending
                                            else "没有等待转写的项"))
        self.refresh_btn.setEnabled(self.batch_dir is not None)

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
            QMessageBox.information(self, "提示", "请先在设置中配置大模型转写")
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
            self._reload(path if single else None)
        self._update_buttons()

    def _retry_all_failed(self) -> None:
        """重试本批全部失败项并补做未开启的项：逐份重置 failed→pending、none→pending，投入临时 OcrQueue。

        某一份 result.json 坏了只跳过这一份并在结束时汇总，不影响其他已投递的项。"""
        if not self.batch_dir or not self.rows:
            return
        if not self._allow_retry():
            return
        if not self.settings or not self.settings.ocr:
            QMessageBox.information(self, "提示", "请先在设置中配置大模型转写")
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
            text = "没有需要补做或重试的转写项；在途任务不会重复投递"
            QMessageBox.information(self, "提示", f"{text}。\n{problems}" if problems else text)
            if problems:
                self._notify(problems)
            return
        self._begin_retry(owner, f"正在补做和重试转写：共 {q.total} 项…" + (f"（{problems}）" if problems else ""))

        def run() -> None:
            try:
                q.wait()
                q.close()
                parts = [f"完成 {q.done}"]
                if q.failed - q.cancelled:
                    parts.append(f"失败 {q.failed - q.cancelled}")
                if q.cancelled:
                    parts.append(f"已取消 {q.cancelled}（该批次在扫描页被重新打开或程序正在退出，保留为“等待转写”）")
                msg = f"补做和重试转写结束：{'，'.join(parts)}，共 {q.total} 项" + (f"；{problems}" if problems else "")
                self._retry.done.emit(owner, q.done == q.total and not skipped, msg)
            except Exception as e:  # noqa: BLE001
                q.close(cancel=True)
                self._retry.done.emit(owner, False, f"补做和重试转写中断：{e}")

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
        answer = QMessageBox.question(
            self, "放弃未完成转写",
            f"将本批 {pending} 项“等待转写”标为“未启用”？\n\n"
            "这些题不再转写，交付时不再阻断，交付记录会逐项注明“文字转写已放弃”。"
            "以后配置好大模型转写，仍可用“补做和重试转写”补做。")
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
        msg = f"已把 {changed} 项等待转写标为未启用"
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
        if not self.card.set_image(image):
            return
        w, _h = self.card.image_size()
        s = w / self.tpl.paper[0]  # 整页图像素 / mm
        self.card.set_overlays([
            Overlay(QRectF(o.rect[0] * s, o.rect[1] * s, o.rect[2] * s, o.rect[3] * s), OVERLAY_COLOR[o.kind],
                    fill_alpha=70 if o.kind in ("answer", "exam", "omr", "choice") else 25, label=o.label)
            for o in face_overlays(self.tpl, r, self.face)])

    def _fill_subjective(self, r: dict | None) -> None:
        while self.subj_box.count():
            it = self.subj_box.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        if not r:
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
        status = {"none": "已放弃转写（未启用）" if q.get("ocr_skipped") else "未开启转写",
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
            button = QPushButton(f"查看区域 {i}" if kind == "subjective" else "查看图片")
            button.setToolTip("在“证据 / JSON”页查看该区域的切图")
            button.clicked.connect(lambda _c=False, path=img: self._show_evidence(path))
            acts.addWidget(button)
        acts.addStretch(1)
        if ocr_st in ("failed", "none", "pending"):
            btn = QPushButton("重试转写" if ocr_st == "failed" else "转写")
            if blocked or not has_ocr:
                btn.setEnabled(False)
                btn.setToolTip(blocked or "请先在设置中配置大模型转写")
            else:
                btn.clicked.connect(lambda _c=False, k=kind, kk=key, lb=title: self._retry_single(k, kk, lb))
            acts.addWidget(btn)
        self.subj_box.addWidget(row)

    def _show_evidence(self, path: str) -> None:
        self.evidence.select_image(path)
        self.card_tabs.setCurrentWidget(self.evidence)
