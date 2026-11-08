"""结果页：左侧学生列表（筛选、搜索），右侧单个学生：卡面叠层、异常、客观题答案表、主观题切图与转写。"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractScrollArea, QAbstractSlider, QApplication, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSplitter, QTableView, QTableWidget,
                               QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget)

from ...app.results import (PHYSICAL_TEXT, StudentRow, face_overlays, load_batch_template, load_rows,
                            warning_text)
from ...app.settings import AppSettings
from ...ocr.queue import OcrQueue, enable_ocr, reset_failed
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


STATUS_TEXT = {"ok": "正常", "blank": "未答", "multiple": "多涂", "uncertain": "存疑", "partial": "部分", "failed": "失败"}
OVERLAY_COLOR = {"answer": theme.REGION["obj"], "bad": theme.PAPER_ERR, "exam": theme.REGION["id"], "exam_bad": theme.PAPER_ERR,
                 "omr": theme.REGION["id"], "choice": theme.REGION["sub"]}


class ResultsPage(QWidget):
    batchChanged = Signal(str)
    ocrChanged = Signal(str)

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
        self.retry_all_btn = QPushButton("补做和重试转写")
        self.retry_all_btn.setEnabled(False)
        self.retry_all_btn.clicked.connect(self._retry_all_failed)
        hd.addWidget(self.retry_all_btn)
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
        sh = QLabel("主观题")
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

    # ---------- 数据 ----------
    def _attach_delivery_panel(self) -> None:
        if self.delivery_panel is not None:
            return
        from ..widgets.delivery_panel import DeliveryPanel
        self.delivery_panel = DeliveryPanel(self)
        self.delivery_panel.active_batch_fn = lambda: self._active_batch_fn() if self._active_batch_fn else None
        self._delivery_band.addWidget(self.delivery_panel)

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
            if f == "warn" and not (r.status == "ok" and (r.warnings or r.ocr_failed)):
                continue
            if f == "err" and r.status not in ("physical", "voided"):
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
            reason = PHYSICAL_TEXT.get(r.physical or "", r.physical or "")
            exam = r.exam_no or {"physical": f"物理异常 {reason}".strip(), "voided": "已作废"}.get(r.status, "—")
            items = [QStandardItem(f"#{r.seq:04d}"), QStandardItem(exam),
                     QStandardItem(self._row_warning(r))]
            if r.status != "ok":
                for it in items:
                    theme.set_item_colors(it, foreground="ERR" if r.status == "physical" else "INK3")
            elif r.warnings or r.ocr_failed:
                theme.set_item_colors(items[2], foreground="WARN")
            items[0].setData(r.seq, Qt.ItemDataRole.UserRole)
            self.model.appendRow(items)
        self.count_label.setText(f"{len(rows)} / {len(self.rows)} 份")
        set_empty_text(self.table, "没有符合条件的学生" if self.rows else "这个批次还没有识别结果")

    @staticmethod
    def _row_warning(row: StudentRow) -> str:
        if row.status != "ok":
            return ""
        parts = [str(row.warnings)] if row.warnings else []
        if row.ocr_failed:
            parts.append(f"转写失败 {row.ocr_failed}")
        return " / ".join(parts)

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
        """PgUp/PgDn 切换上一份/下一份；焦点在 JSON 等其他可滚动区域时仍用于翻页。"""
        w = QApplication.focusWidget()
        if isinstance(w, QAbstractScrollArea) and w is not self.table:
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
                                                         "voided": "已作废"}.get(row.status, "识别中"))
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
            self._answers_empty.setText({"physical": "物理异常，没有识别结果。\n请在扫描页重扫或作废。",
                                         "voided": "已作废，没有识别结果"}.get(row.status, "没有识别结果"))
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
        if row.ocr_failed:
            warnings.append(f"转写失败 {row.ocr_failed} 项")
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
        if not reason and not (self.settings and self.settings.ocr):
            reason = "请先在设置中配置大模型转写"
        self.retry_all_btn.setEnabled(not reason and bool(self.rows))
        self.retry_all_btn.setToolTip(reason)

    def _allow_retry(self) -> bool:
        reason = self._retry_block_reason()
        if reason:
            self._update_retry_controls()
            QMessageBox.information(self, "提示", reason)
            return False
        return True

    def _retry_single(self, kind: str, key: str) -> None:
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
        q = OcrQueue(cfg)
        try:
            q.enqueue_item(result_path, kind, key)
        except Exception as e:
            q.close(cancel=True)
            QMessageBox.warning(self, "转写失败", str(e))
            return

        def run() -> None:
            try:
                q.wait()
                q.close()
                ok = q.total > 0 and q.done == q.total
                self._retry.done.emit(str(result_path), ok, "转写完成" if ok else "转写未完成")
            except Exception as e:  # noqa: BLE001
                q.close(cancel=True)
                self._retry.done.emit(str(result_path), False, f"转写失败：{e}")

        threading.Thread(target=run, daemon=True, name="ocr-retry-wait").start()

    def _retry_done(self, owner: str, ok: bool, msg: str) -> None:
        """Only refresh the result whose task completed."""
        path = Path(owner).resolve()
        single = path.name == "result.json"
        batch = path.parent.parent.parent if single else path
        self.ocrChanged.emit(str(batch))
        if self.batch_dir is None or self.batch_dir.resolve() != batch:
            return
        try:
            updated = {row.seq: row for row in load_rows(self.batch_dir)}
        except (OSError, ValueError, KeyError, TypeError):
            updated = {}
        before = [row.seq for row in self.visible_rows()]
        for row in self.rows:
            if row.seq in updated:
                row.ocr_failed = updated[row.seq].ocr_failed
        if before != [row.seq for row in self.visible_rows()]:
            selected = self.current.seq if self.current else None
            self._refill()
            if selected is not None:
                self.select_seq(selected)
        else:
            for i, row in enumerate(self.visible_rows()):
                item = self.model.item(i, 2)
                if item is not None:
                    item.setText(self._row_warning(row))
        if self.current and (not single or self.current.result and self.current.result.resolve() == path):
            i = self.table.currentIndex().row()
            if i >= 0:
                self._select_row(i)

    def _retry_all_failed(self) -> None:
        """重试本批全部失败项：重置 failed→pending，投入临时 OcrQueue。"""
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
        # 重置 failed 并启用 none 项
        owner = str(self.batch_dir.resolve())
        q = OcrQueue(cfg)
        try:
            for r in self.rows:
                if r.result and r.result.exists():
                    reset_failed(r.result)
                    enable_ocr(r.result)
                    q.enqueue(r.result)
        except Exception as e:
            q.close(cancel=True)
            QMessageBox.warning(self, "无法开始转写", str(e))
            return
        if q.total == 0:
            q.close()
            QMessageBox.information(self, "提示", "没有需要补做或重试的转写项；在途任务不会重复投递")
            return

        def run() -> None:
            q.wait()
            q.close()
            self._retry.done.emit(owner, q.done == q.total, f"处理完成 {q.done} / {q.total} 项")

        threading.Thread(target=run, daemon=True, name="ocr-batch-wait").start()

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
        for q in r.get("subjective", []):
            t = QLabel(f"第 {q['no']} 题" + (f"（{q['sub']}）" if q.get("sub") else ""))
            t.setTextFormat(Qt.TextFormat.PlainText)
            t.setStyleSheet("font-weight:600; margin-top:10px;")
            self.subj_box.addWidget(t)
            status = {"none": "未开启转写", "pending": "等待转写", "failed": "转写失败", "done": ""}.get(
                q.get("ocr_status"), "")
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
            for i, img in enumerate(q.get("images", []), 1):
                button = QPushButton(f"查看区域 {i}")
                button.setToolTip("在“证据 / JSON”页查看该区域的切图")
                button.clicked.connect(lambda _c=False, path=img: self._show_evidence(path))
                acts.addWidget(button)
            acts.addStretch(1)
            ocr_st = q.get("ocr_status")
            if ocr_st in ("failed", "none", "pending"):
                btn = QPushButton("重试转写" if ocr_st == "failed" else "转写")
                if blocked or not has_ocr:
                    btn.setEnabled(False)
                    btn.setToolTip(blocked or "请先在设置中配置大模型转写")
                else:
                    btn.clicked.connect(lambda _c=False, k=subjective_item_key(q): self._retry_single("subjective", k))
                acts.addWidget(btn)
            self.subj_box.addWidget(row)
        self.subj_box.addStretch(1)

    def _show_evidence(self, path: str) -> None:
        self.evidence.select_image(path)
        self.card_tabs.setCurrentWidget(self.evidence)
