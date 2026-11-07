"""结果页：左侧学生列表（筛选、搜索），右侧单个学生：卡面叠层、异常、客观题答案表、主观题切图与转写。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPixmap, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox,
                               QPushButton, QScrollArea, QSplitter, QTableView, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from ...app.results import StudentRow, face_overlays, load_batch_template, load_rows, warning_text
from ...app.settings import AppSettings
from ...ocr.queue import OcrQueue, enable_ocr, reset_failed, retry_item
from ...template.model import Template
from .. import theme
from ..widgets.card_view import CardView, Overlay
from ..widgets.common import Segmented, hline

class _RetrySignals(QObject):
    """后台重试线程 → 界面线程的信号桥。"""
    done = Signal(bool, str)  # (成功, 消息)


STATUS_TEXT = {"ok": "正常", "blank": "未答", "multiple": "多涂", "uncertain": "存疑", "partial": "部分", "failed": "失败"}
OVERLAY_COLOR = {"answer": theme.REGION["obj"], "bad": theme.ERR, "exam": theme.REGION["id"], "exam_bad": theme.ERR,
                 "omr": theme.REGION["id"], "choice": theme.REGION["sub"]}


class ResultsPage(QWidget):
    batchChanged = Signal(str)

    def __init__(self, settings: AppSettings | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self._active_batch_fn: Callable[[], str | None] | None = None  # 返回当前正在扫描的批次目录
        self.batch_dir: Path | None = None
        self.rows: list[StudentRow] = []
        self.tpl: Template | None = None
        self.result: dict | None = None
        self.current: StudentRow | None = None
        self.face = "front"
        self._retry = _RetrySignals()
        self._retry.done.connect(self._retry_done)
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
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setShowGrid(False)
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
        self.info = QLabel("")
        self.info.setStyleSheet(f"color:{theme.INK2};")
        hd.addWidget(self.info, 1)
        self.retry_all_btn = QPushButton("重试本批全部失败项")
        self.retry_all_btn.clicked.connect(self._retry_all_failed)
        hd.addWidget(self.retry_all_btn)
        hd.addSpacing(8)
        self.prev_btn = QPushButton("上一份")
        self.next_btn = QPushButton("下一份")
        self.prev_btn.clicked.connect(lambda: self._step(-1))
        self.next_btn.clicked.connect(lambda: self._step(1))
        hd.addWidget(self.prev_btn)
        hd.addWidget(self.next_btn)
        rl.addLayout(hd)
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
        self.card = CardView()
        cl.addWidget(self.card, 1)
        self.anomalies = QLabel("")
        self.anomalies.setWordWrap(True)
        self.anomalies.setTextFormat(Qt.TextFormat.RichText)
        self.anomalies.setStyleSheet("font-size:13px;")
        cl.addWidget(self.anomalies)
        split.addWidget(cardw)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        oh = QLabel("客观题答案")
        oh.setStyleSheet("font-weight:600; padding:10px 16px;")
        sl.addWidget(oh)
        self.answers = QTableWidget(0, 4)
        self.answers.setHorizontalHeaderLabels(["题号", "答案", "状态", "涂黑度"])
        self.answers.verticalHeader().setVisible(False)
        self.answers.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.answers.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.answers.setShowGrid(False)
        sl.addWidget(self.answers, 1)
        sh = QLabel("主观题")
        sh.setStyleSheet(f"font-weight:600; padding:10px 16px; border-top:1px solid {theme.LINE};")
        sl.addWidget(sh)
        self.subj_box = QVBoxLayout()
        self.subj_box.setContentsMargins(16, 0, 16, 12)
        holder = QWidget()
        holder.setLayout(self.subj_box)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        sa.setWidget(holder)
        sl.addWidget(sa, 1)
        split.addWidget(side)
        split.setSizes([680, 432])
        rl.addWidget(split, 1)
        root.addWidget(right, 1)

    # ---------- 数据 ----------
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
        self.face_seg.buttons["back"].setEnabled("back" in self.tpl.faces)
        self.current = self.result = None
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
            if f == "warn" and not (r.status == "ok" and r.warnings):
                continue
            if f == "err" and r.status not in ("physical", "voided"):
                continue
            if q and q not in f"{r.seq:04d}" and q not in (r.exam_no or "") and q not in r.source:
                continue
            out.append(r)
        return out

    def _refill(self) -> None:
        self.model.removeRows(0, self.model.rowCount())
        rows = self.visible_rows()
        for r in rows:
            exam = r.exam_no or {"physical": f"物理异常 {r.physical or ''}", "voided": "已作废"}.get(r.status, "—")
            items = [QStandardItem(f"#{r.seq:04d}"), QStandardItem(exam),
                     QStandardItem(str(r.warnings) if r.status == "ok" and r.warnings else "")]
            if r.status != "ok":
                for it in items:
                    it.setForeground(QColor(theme.ERR if r.status == "physical" else theme.INK3))
            elif r.warnings:
                items[2].setForeground(QColor(theme.WARN))
            items[0].setData(r.seq, Qt.ItemDataRole.UserRole)
            self.model.appendRow(items)
        self.count_label.setText(f"{len(rows)} / {len(self.rows)} 份")

    def select_seq(self, seq: int) -> bool:
        for i in range(self.model.rowCount()):
            if self.model.item(i, 0).data(Qt.ItemDataRole.UserRole) == seq:
                self.table.selectRow(i)
                self._select_row(i)
                return True
        return False

    def _step(self, d: int) -> None:
        i = self.table.currentIndex().row() + d
        if 0 <= i < self.model.rowCount():
            self.table.selectRow(i)

    def _select_row(self, i: int) -> None:
        if i < 0 or i >= self.model.rowCount():
            return
        seq = self.model.item(i, 0).data(Qt.ItemDataRole.UserRole)
        row = next(r for r in self.rows if r.seq == seq)
        self.current = row
        self.result = None
        broken = False
        if row.result and row.result.exists():
            try:
                r = json.loads(row.result.read_text(encoding="utf-8"))
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
        self.answers.setRowCount(0)
        self.anomalies.setText("")
        self._fill_subjective(None)

    def _set_face(self, face: str) -> None:
        self.face = face
        self._show_card()

    # ---------- 显示 ----------
    def _show(self, broken: bool = False) -> None:
        row, r = self.current, self.result
        self.title.setText(f"#{row.seq:04d}")
        if r is None:
            self.info.setText("结果文件损坏" if broken else {"physical": f"物理异常：{row.physical} {row.message}",
                                                         "voided": "已作废"}.get(row.status, "识别中"))
            self.card.clear()
            raws = sorted((self.batch_dir / "raw").glob(f"{row.seq:04d}_*"))
            if raws:
                self.card.set_image(raws[0])
            self.answers.setRowCount(0)
            self.anomalies.setText("")
            self._fill_subjective(None)
            return
        st = r["student"]
        exam = (st.get("exam_no") or {}).get("value") or "—"
        bc = ((st.get("barcodes") or {}).get("student_barcode") or {}).get("value") or "—"
        absent = "是" if (st.get("absent") or {}).get("value") else "否"
        name = ((st.get("fields") or {}).get("name") or {}).get("text") or ""
        extra = "".join(f"　{f.get('label') or k} {f.get('value') or '—'}"
                        for k, f in (st.get("omr_fields") or {}).items() if isinstance(f, dict))
        self.info.setText(f"填涂考号 {exam}　条码 {bc}　缺考 {absent}" + (f"　姓名 {name}" if name else "") + extra)
        self._show_card()
        self.anomalies.setText("<br>".join(
            f"<span style='color:{theme.ERR}'>■</span> {warning_text(w)}" for w in r.get("warnings", []))
            or f"<span style='color:{theme.INK3}'>无警告</span>")
        objs = r.get("objective", [])
        chooses = r.get("choose_one", [])
        self.answers.setRowCount(len(objs) + len(chooses))
        for i, o in enumerate(objs):
            fill = max(o.get("fill", {}).values(), default=0)
            cells = [o["no"], o["answer"] or "—", STATUS_TEXT.get(o["status"], o["status"]),
                     " / ".join(f"{k}{v:.2f}" for k, v in o.get("fill", {}).items() if v >= 0.15) or f"{fill:.2f}"]
            for j, c in enumerate(cells):
                it = QTableWidgetItem(c)
                if o["status"] != "ok":
                    it.setBackground(QColor("#fdf0ee"))
                    if j == 2:
                        it.setForeground(QColor(theme.ERR))
                self.answers.setItem(i, j, it)
        for i, c in enumerate(chooses, start=len(objs)):  # 选做组：每组一行，接在客观题后面
            cells = [f"选做：{c.get('label') or c.get('key', '')}", "、".join(c.get("chosen") or []) or "—",
                     STATUS_TEXT.get(c.get("status"), c.get("status") or ""),
                     " / ".join(f"{k}{v:.2f}" for k, v in (c.get("fill") or {}).items() if v >= 0.15)]
            for j, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c.get("status") != "ok":
                    it.setBackground(QColor("#fdf0ee"))
                    if j == 2:
                        it.setForeground(QColor(theme.ERR))
                self.answers.setItem(i, j, it)
        self._fill_subjective(r)

    # ---------- 重试转写 ----------
    def _retry_single(self, kind: str, key: str) -> None:
        """后台线程执行单项转写。"""
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

        def run() -> None:
            try:
                _text, status = retry_item(result_path, kind, key, cfg)
                self._retry.done.emit(status == "done", "转写完成" if status == "done" else "转写失败")
            except Exception as e:  # noqa: BLE001
                self._retry.done.emit(False, f"转写失败：{e}")

        threading.Thread(target=run, daemon=True).start()

    def _retry_done(self, ok: bool, msg: str) -> None:
        """转写完成后刷新当前学生。"""
        if self.current:
            i = self.table.currentIndex().row()
            if i >= 0:
                self._select_row(i)

    def _retry_all_failed(self) -> None:
        """重试本批全部失败项：重置 failed→pending，投入临时 OcrQueue。"""
        if not self.batch_dir or not self.rows:
            return
        # 检查是否正在扫描
        if self._active_batch_fn:
            active = self._active_batch_fn()
            if active and str(self.batch_dir) == active:
                QMessageBox.information(self, "提示", "请在扫描页结束批次后再重试")
                return
        if not self.settings or not self.settings.ocr:
            QMessageBox.information(self, "提示", "请先在设置中配置大模型转写")
            return
        try:
            cfg = self.settings.ocr_config()
        except ValueError as e:
            QMessageBox.warning(self, "配置有误", str(e))
            return
        # 重置 failed 并启用 none 项
        count = 0
        result_paths = []
        for r in self.rows:
            if r.result and r.result.exists():
                count += reset_failed(r.result)
                count += enable_ocr(r.result)
                result_paths.append(r.result)
        if count == 0:
            QMessageBox.information(self, "提示", "没有需要重试的转写项")
            return
        # 在后台线程中用临时 OcrQueue 处理
        q = OcrQueue(cfg)
        for rp in result_paths:
            q.enqueue(rp)

        def run() -> None:
            q.wait()
            q.close()
            self._retry.done.emit(True, f"批量重试完成，共 {count} 项")

        threading.Thread(target=run, daemon=True).start()

    def _show_card(self) -> None:
        r = self.result
        if r is None or self.tpl is None:
            return
        page = r["pages"].get(self.face)
        if not page or not self.card.set_image(self.current.result.parent / page):
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
        base = self.current.result.parent
        has_ocr = bool(self.settings and self.settings.ocr)
        for q in r.get("subjective", []):
            t = QLabel(f"第 {q['no']} 题")
            t.setStyleSheet("font-weight:600; margin-top:8px;")
            self.subj_box.addWidget(t)
            for img in q.get("images", []):
                pic = QLabel()
                pm = QPixmap(str(base / img))
                if not pm.isNull():
                    pic.setPixmap(pm.scaledToWidth(360, Qt.TransformationMode.SmoothTransformation))
                pic.setStyleSheet(f"border:1px solid {theme.REGION['sub']};")
                self.subj_box.addWidget(pic)
            status = {"none": "未开启转写", "pending": "等待转写", "failed": "转写失败", "done": ""}.get(
                q.get("ocr_status"), "")
            txt = QLabel(q.get("text") or status)
            txt.setWordWrap(True)
            txt.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            txt.setStyleSheet(f"background:#f7f8f9; border:1px solid {theme.LINE}; padding:8px 10px;"
                              + ("" if q.get("text") else f"color:{theme.INK3};"))
            self.subj_box.addWidget(txt)
            # 重试/转写按钮
            ocr_st = q.get("ocr_status")
            if ocr_st == "failed":
                btn = QPushButton("重试转写")
                btn.setFixedWidth(100)
                if not has_ocr:
                    btn.setEnabled(False)
                    btn.setToolTip("请先在设置中配置大模型转写")
                else:
                    btn.clicked.connect(lambda _c=False, k=q["no"]: self._retry_single("subjective", k))
                self.subj_box.addWidget(btn)
            elif ocr_st in ("none", "pending"):
                btn = QPushButton("转写")
                btn.setFixedWidth(100)
                if not has_ocr:
                    btn.setEnabled(False)
                    btn.setToolTip("请先在设置中配置大模型转写")
                else:
                    btn.clicked.connect(lambda _c=False, k=q["no"]: self._retry_single("subjective", k))
                self.subj_box.addWidget(btn)
        self.subj_box.addStretch(1)
