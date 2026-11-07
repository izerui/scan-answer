"""扫描页：左栏异常与警告，中栏计数 + 批次拼图 + 最近一张，右栏输入源与参数。"""
from __future__ import annotations

import json
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from ...app.library import Exam
from ...app.settings import AppSettings
from ...pipeline.events import FinalizedEvent, OcrProgress, SheetEvent
from ...source.folder import FolderSource
from .. import theme
from ..controller import FINALIZED, FINISHING, IDLE, PAUSED, RUNNING, ScanController
from ..widgets.card_view import CardView
from ..widgets.common import CounterTile, RegionLegend, Segmented, Swatch, Toggle, hline, section_header
from ..widgets.mosaic import ERR, OK, QUEUED, VOID, WARN, BatchMosaic, MosaicLegend

PHYSICAL_TEXT = {"ALIGN_FAILED": "定位失败", "FACE_MISMATCH": "正反面无法匹配", "BLANK_PAGE": "空白页",
                 "MISSING_FACE": "缺少一面", "SKEW_TOO_LARGE": "倾斜过大"}
STATE_TEXT = {IDLE: "就绪", RUNNING: "识别中", PAUSED: "已暂停", FINISHING: "结束批次中", FINALIZED: "已结束"}


class ExceptionItem(QWidget):
    """待处理物理异常：缩略图 + 原因 + 重扫 / 作废。"""
    rescanClicked = Signal(int)
    voidClicked = Signal(int)

    def __init__(self, seq: int, code: str, message: str, thumb: Path | None, parent: QWidget | None = None):
        super().__init__(parent)
        self.seq = seq
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(12)
        pic = QLabel()
        pic.setFixedSize(96, 68)
        pic.setStyleSheet(f"background:#fff; border:1px solid {theme.LINE};")
        if thumb and thumb.exists():
            pm = QPixmap(str(thumb))
            if not pm.isNull():
                pic.setPixmap(pm.scaled(96, 68, Qt.AspectRatioMode.KeepAspectRatio,
                                        Qt.TransformationMode.SmoothTransformation))
        lay.addWidget(pic, 0, Qt.AlignmentFlag.AlignTop)
        info = QVBoxLayout()
        info.setSpacing(2)
        no = QLabel(f"#{seq:04d}")
        no.setStyleSheet("font-weight:600;")
        why = QLabel(PHYSICAL_TEXT.get(code, code))
        why.setStyleSheet(f"color:{theme.ERR}; font-weight:500;")
        msg = QLabel(message)
        msg.setObjectName("muted")
        msg.setWordWrap(True)
        info.addWidget(no)
        info.addWidget(why)
        info.addWidget(msg)
        acts = QHBoxLayout()
        b1 = QPushButton("重扫")
        b2 = QPushButton("作废")
        b2.setObjectName("danger")
        b1.clicked.connect(lambda: self.rescanClicked.emit(self.seq))
        b2.clicked.connect(lambda: self.voidClicked.emit(self.seq))
        acts.addWidget(b1)
        acts.addWidget(b2)
        acts.addStretch(1)
        info.addLayout(acts)
        lay.addLayout(info, 1)


class ScanPage(QWidget):
    openResult = Signal(str, int)  # 批次目录, 序号

    def __init__(self, controller: ScanController, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.ctrl = controller
        self.settings = settings
        self.exam: Exam | None = None
        self.events: dict[int, SheetEvent] = {}
        self._voided: set[int] = set()
        self.done_times: deque[float] = deque(maxlen=400)
        self.started_at: float | None = None
        self.ocr = OcrProgress(0, 0, 0)
        self._build()
        c = self.ctrl
        c.sheetQueued.connect(self._on_queued)
        c.sheetDone.connect(self._on_done)
        c.voided.connect(self._on_voided)
        c.finalized.connect(self._on_finalized)
        c.ocrProgress.connect(self._on_ocr)
        c.stateChanged.connect(self._on_state)
        c.errorOccurred.connect(lambda m: QMessageBox.warning(self, "出错了", m))
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._on_state(IDLE)

    # ---------- 布局 ----------
    def _build(self) -> None:
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._left(), 0)
        root.addWidget(self._middle(), 1)
        root.addWidget(self._right(), 0)

    def _left(self) -> QWidget:
        w = QWidget()
        w.setObjectName("panel")
        w.setFixedWidth(304)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.exc_header = section_header("物理异常 0", "需处理后才能结束批次")
        lay.addWidget(self.exc_header)
        self.exc_box = QVBoxLayout()
        self.exc_box.setContentsMargins(0, 0, 0, 0)
        self.exc_box.setSpacing(0)
        holder = QWidget()
        holder.setLayout(self.exc_box)
        self.exc_box.addStretch(1)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setWidget(holder)
        lay.addWidget(sa, 3)
        self.warn_header = section_header("数据警告 0", "只标记，不拦截")
        lay.addWidget(self.warn_header)
        self.warn_list = QLabel("")
        self.warn_list.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.warn_list.setWordWrap(True)
        self.warn_list.setContentsMargins(16, 8, 16, 8)
        self.warn_list.setStyleSheet("font-size:13px; line-height:150%;")
        lay.addWidget(self.warn_list, 2)
        lay.addWidget(section_header("区域色码"))
        lay.addWidget(RegionLegend(extra=[(theme.ERR, "物理异常"), (theme.WARN_FILL, "数据警告")]))
        return w

    def _middle(self) -> QWidget:
        w = QWidget()
        w.setObjectName("work")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(24, 0, 24, 12)
        lay.setSpacing(0)
        bar = QHBoxLayout()
        self.batch_label = QLabel("未开始批次")
        self.batch_label.setObjectName("h2")
        self.state_chip = QLabel()
        bar.addWidget(self.batch_label)
        bar.addSpacing(12)
        bar.addWidget(self.state_chip)
        bar.addStretch(1)
        self.meta_label = QLabel("")
        self.meta_label.setStyleSheet(f"color:{theme.INK2}; font-size:13px;")
        bar.addWidget(self.meta_label)
        barw = QWidget()
        barw.setFixedHeight(56)
        barw.setLayout(bar)
        lay.addWidget(barw)
        lay.addWidget(hline())
        tiles = QHBoxLayout()
        tiles.setSpacing(0)
        self.t_scanned = CounterTile("已扫描", note="张，正反面已配对")
        self.t_ok = CounterTile("成功", note="可输出")
        self.t_err = CounterTile("物理异常", theme.ERR, theme.ERR, note="待处理")
        self.t_warn = CounterTile("数据警告", theme.WARN_FILL, theme.WARN, note="已标记")
        self.t_speed = CounterTile("速度", unit="张/分", note="最近一分钟")
        self.t_ocr = CounterTile("主观题转写", theme.REGION["sub"], note="未开启")
        for t in (self.t_scanned, self.t_ok, self.t_err, self.t_warn, self.t_speed, self.t_ocr):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)
        lay.addWidget(hline())
        head = QHBoxLayout()
        head.setContentsMargins(0, 14, 0, 10)
        head.setSpacing(12)
        t = QLabel("批次进度")
        t.setStyleSheet("font-weight:600; font-size:15px;")
        head.addWidget(t)
        self.progress_label = QLabel("0 张")
        self.progress_label.setStyleSheet(f"color:{theme.INK2};")
        head.addWidget(self.progress_label)
        hint = QLabel("每格一张纸，悬停看序号，点击查看结果")
        hint.setObjectName("muted")
        head.addWidget(hint)
        head.addStretch(1)
        head.addWidget(MosaicLegend())
        lay.addLayout(head)
        self.mosaic = BatchMosaic()
        self.mosaic.cellClicked.connect(self._open_cell)
        lay.addWidget(self.mosaic)
        lay.addSpacing(12)
        lay.addWidget(hline())
        prev = QHBoxLayout()
        prev.setContentsMargins(0, 12, 0, 0)
        left = QVBoxLayout()
        self.prev_title = QLabel("最近一张")
        self.prev_title.setStyleSheet("font-weight:600;")
        left.addWidget(self.prev_title)
        self.preview = CardView()
        self.preview.setFixedSize(392, 277)
        left.addWidget(self.preview)
        prev.addLayout(left)
        prev.addSpacing(20)
        self.readout = QGridLayout()
        self.readout.setVerticalSpacing(10)
        self.readout_values: dict[str, QLabel] = {}
        for i, (key, label, color) in enumerate([("exam", "考号", theme.REGION["id"]), ("bar", "条码", theme.REGION["bar"]),
                                                 ("absent", "缺考", theme.REGION["info"]), ("obj", "客观题", theme.REGION["obj"]),
                                                 ("sub", "主观题", theme.REGION["sub"]), ("align", "定位", theme.REGION["loc"])]):
            self.readout.addWidget(Swatch(color, 8), i, 0)
            k = QLabel(label)
            k.setStyleSheet(f"color:{theme.INK2};")
            self.readout.addWidget(k, i, 1)
            v = QLabel("—")
            v.setStyleSheet("font-weight:600;")
            v.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.readout.addWidget(v, i, 2)
            self.readout_values[key] = v
        rw = QWidget()
        rw.setLayout(self.readout)
        prev.addWidget(rw, 1, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(prev)
        lay.addStretch(1)
        return w

    def _right(self) -> QWidget:
        w = QWidget()
        w.setObjectName("panel")
        w.setFixedWidth(328)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(section_header("扫描参数"))
        form = QVBoxLayout()
        form.setContentsMargins(16, 12, 16, 12)
        form.setSpacing(10)
        self.src_seg = Segmented([("scanner", "扫描仪"), ("folder", "导入图片")], "folder")
        self.src_seg.buttons["scanner"].setEnabled(False)
        self.src_seg.buttons["scanner"].setToolTip("扫描仪接入（TWAIN）在后续版本提供")
        form.addLayout(self._field("输入源", self.src_seg))
        note = QLabel("当前版本使用“导入图片”：选择扫描好的图片文件夹，文件名带 _1/_2 后缀时按前缀配对正反面。")
        note.setWordWrap(True)
        note.setStyleSheet(f"background:#e9ecef; color:{theme.INK2}; font-size:12px; padding:8px 10px;")
        form.addWidget(note)
        row = QHBoxLayout()
        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("图片文件夹")
        pick = QPushButton("选择…")
        pick.clicked.connect(self._pick_folder)
        row.addWidget(self.folder_edit, 1)
        row.addWidget(pick)
        form.addLayout(row)
        sd = self.settings.scan
        self.duplex = Segmented([("1", "双面"), ("0", "单面")], "1" if sd.duplex else "0")
        self.dpi = Segmented([(str(d), str(d)) for d in (200, 300, 400, 600)], str(sd.dpi))
        self.color = Segmented([("gray", "灰度"), ("color", "彩色")], sd.color)
        self.double_feed = Toggle(sd.double_feed)
        for label, wdg in (("进纸", self.duplex), ("分辨率 DPI", self.dpi), ("色彩", self.color),
                           ("双张检测（超声波）", self.double_feed)):
            wdg.setEnabled(False)
            wdg.setToolTip("导入图片时不适用；接入扫描仪后生效")
            form.addLayout(self._field(label, wdg))
        self.template_label = QLabel("—")
        form.addLayout(self._field("模板", self.template_label))
        self.out_label = QLabel("—")
        self.out_label.setWordWrap(True)
        self.out_label.setStyleSheet("font-size:12px;")
        form.addLayout(self._field("输出目录", self.out_label))
        lay.addLayout(form)
        lay.addStretch(1)
        btns = QVBoxLayout()
        btns.setContentsMargins(16, 16, 16, 16)
        btns.setSpacing(10)
        self.main_btn = QPushButton("开始识别")
        self.main_btn.setObjectName("big")
        self.main_btn.clicked.connect(self._main_clicked)
        btns.addWidget(self.main_btn)
        r2 = QHBoxLayout()
        self.new_btn = QPushButton("新批次")
        self.new_btn.clicked.connect(self.new_batch)
        self.finish_btn = QPushButton("结束批次")
        self.finish_btn.clicked.connect(self.finalize)
        r2.addWidget(self.new_btn)
        r2.addWidget(self.finish_btn)
        btns.addLayout(r2)
        hint = QLabel("空格键 暂停 / 继续")
        hint.setObjectName("muted")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        btns.addWidget(hint)
        bw = QWidget()
        bw.setObjectName("btnPanel")
        bw.setLayout(btns)
        # 用对象名限定选择器：不带选择器的样式会传给子控件，覆盖按钮自身的样式
        bw.setStyleSheet(f"#btnPanel {{ background:#eef0f2; border-top:1px solid {theme.LINE}; }}")
        lay.addWidget(bw)
        return w

    @staticmethod
    def _field(label: str, widget: QWidget) -> QHBoxLayout:
        row = QHBoxLayout()
        lab = QLabel(label)
        lab.setStyleSheet(f"color:{theme.INK2}; font-size:13px;")
        row.addWidget(lab)
        row.addStretch(1)
        row.addWidget(widget)
        return row

    # ---------- 考试 / 批次 ----------
    def set_exam(self, exam: Exam) -> bool:
        """切换到某个考试：下次点"开始识别"时在该考试下新建批次。"""
        if self.ctrl.session and self.ctrl.state in (RUNNING, PAUSED, FINISHING):
            raise RuntimeError("当前批次还在进行中")
        try:
            self.ctrl.close_batch()
        except RuntimeError as e:
            QMessageBox.warning(self, "提示", str(e))
            return False
        self.exam = exam
        self._reset_view()
        self.template_label.setText(exam.template_dir.name)
        self.out_label.setText(str(exam.directory))
        return True

    def open_batch(self, exam: Exam, batch_dir: str | Path) -> bool:
        """继续已有批次：恢复拼图与异常列表。"""
        try:
            s = self.ctrl.open_batch(batch_dir, self.settings.ocr_config())
        except Exception as e:  # noqa: BLE001 — RuntimeError 和其他异常
            QMessageBox.warning(self, "提示", f"无法打开批次：{e}")
            return False
        self.exam = exam
        self._reset_view()
        self.template_label.setText(exam.template_dir.name)
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
        return True

    def _ensure_batch(self) -> bool:
        if self.ctrl.session is not None and self.ctrl.state != FINALIZED:
            return True
        if self.exam is None:
            QMessageBox.information(self, "提示", "请先在“考试”页选择或新建考试。")
            return False
        try:
            self.ctrl.new_batch(self.exam, self.settings.ocr_config())
        except Exception as e:  # noqa: BLE001 — 包括 RuntimeError 和其他异常
            QMessageBox.warning(self, "提示", f"无法创建批次：{e}")
            return False
        self._reset_view()
        self._update_batch_label()
        return True

    def new_batch(self) -> None:
        if self.ctrl.state in (RUNNING, PAUSED, FINISHING):
            QMessageBox.information(self, "提示", "请先暂停并结束当前批次。")
            return
        try:
            self.ctrl.close_batch()
        except RuntimeError as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        self._reset_view()

    def _reset_view(self) -> None:
        self.events.clear()
        self._voided.clear()
        self.done_times.clear()
        self.started_at = None
        self.ocr = OcrProgress(0, 0, 0)
        self.mosaic.reset()
        for i in reversed(range(self.exc_box.count() - 1)):
            w = self.exc_box.itemAt(i).widget()
            if w:
                w.deleteLater()
                self.exc_box.removeWidget(w)
        self.preview.clear()
        for v in self.readout_values.values():
            v.setText("—")
        self._refresh_counts()
        self._update_batch_label()

    def _update_batch_label(self) -> None:
        s = self.ctrl.session
        self.batch_label.setText(f"批次 {s.batch_id}" if s else "未开始批次")
        if s:
            ex = self.exam.info if self.exam else None
            self.meta_label.setText(f"模板 {s.manifest['template']['name']}" + (f"　{ex.name} {ex.subject}" if ex else ""))
        else:
            self.meta_label.setText("")

    # ---------- 操作 ----------
    def _pick_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择图片文件夹", self.folder_edit.text() or str(Path.home()))
        if d:
            self.folder_edit.setText(d)

    def start_folder(self, folder: str | Path) -> None:
        if not Path(folder).is_dir():
            QMessageBox.warning(self, "提示", f"文件夹不存在：{folder}")
            return
        if not self._ensure_batch():
            return
        self.started_at = self.started_at or time.monotonic()
        try:
            self.ctrl.start(FolderSource(folder, self.ctrl.session.faces))
        except RuntimeError:
            pass  # 重复点击：命令已排队但尚未开始运行

    def _main_clicked(self) -> None:
        st = self.ctrl.state
        if st == RUNNING:
            self.ctrl.pause()
        elif st == PAUSED:
            self.ctrl.resume()
        else:
            self.start_folder(self.folder_edit.text().strip())

    def keyPressEvent(self, e) -> None:
        if e.key() == Qt.Key.Key_Space and self.ctrl.state in (RUNNING, PAUSED):
            self._main_clicked()
        else:
            super().keyPressEvent(e)

    def finalize(self) -> None:
        if self.ctrl.session is None:
            return
        pending = sum(1 for e in self.events.values() if not e.ok and e.seq not in self._voided)
        if pending:
            QMessageBox.information(self, "提示", f"还有 {pending} 张物理异常未处理（重扫或作废）。")
            return
        self.ctrl.finalize()

    def _rescan(self, seq: int) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, f"选择 #{seq:04d} 重扫后的图片（正反面）",
                                                self.folder_edit.text(), "图片 (*.jpg *.jpeg *.png *.tif *.tiff *.bmp)")
        if files:
            self.ctrl.rescan(seq, sorted(files))

    def _void(self, seq: int) -> None:
        if QMessageBox.question(self, "作废", f"确定作废 #{seq:04d}？作废后不会输出结果。") == QMessageBox.StandardButton.Yes:
            self.ctrl.void(seq)

    def _open_cell(self, seq: int) -> None:
        if self.ctrl.session is not None:
            self.openResult.emit(str(self.ctrl.session.batch_dir), seq)

    # ---------- 信号处理 ----------
    def _on_queued(self, seq: int, source: str) -> None:
        self.mosaic.set_cell(seq, QUEUED)
        self.progress_label.setText(f"{len(self.mosaic.cells)} 张")

    def _on_done(self, e: SheetEvent, animate: bool = True) -> None:
        self.events[e.seq] = e
        self._voided.discard(e.seq)
        self._remove_exception(e.seq)
        if e.ok:
            self.mosaic.set_cell(e.seq, WARN if e.warnings else OK, f"{e.warnings} 条警告" if e.warnings else "")
            if animate:
                self.done_times.append(time.monotonic())
                self._show_latest(e)
        else:
            self.mosaic.set_cell(e.seq, ERR, PHYSICAL_TEXT.get(e.physical or "", e.physical or ""))
            thumb = self._raw_thumb(e.seq)
            item = ExceptionItem(e.seq, e.physical or "", e.message, thumb)
            item.rescanClicked.connect(self._rescan)
            item.voidClicked.connect(self._void)
            self.exc_box.insertWidget(self.exc_box.count() - 1, item)
            if animate:
                self.done_times.append(time.monotonic())
        self.mosaic.current = None
        self._refresh_counts()

    def _on_voided(self, seq: int) -> None:
        self._voided.add(seq)
        self._remove_exception(seq)
        self.mosaic.set_cell(seq, VOID, "已作废")
        self._refresh_counts()

    def _on_finalized(self, e: FinalizedEvent) -> None:
        self.prev_title.setText(f"批次已结束：最终阈值 {e.mark:.3f}，重新判定后变化 {e.changed} 张")
        self._update_batch_label()

    def _on_ocr(self, p: OcrProgress) -> None:
        self.ocr = p
        self._refresh_counts()

    def _on_state(self, state: str) -> None:
        colors = {RUNNING: theme.REGION["obj"], PAUSED: theme.WARN_FILL, FINISHING: theme.REGION["id"],
                  FINALIZED: theme.INK3, IDLE: theme.LINE2}
        self.state_chip.setText(f"● {STATE_TEXT.get(state, state)}")
        self.state_chip.setStyleSheet(f"color:{colors.get(state, theme.INK3)}; font-weight:600; font-size:13px;"
                                      f"border:1px solid {theme.LINE2}; border-radius:2px; padding:1px 8px;")
        self.state_chip.setFixedHeight(24)
        self.main_btn.setText({RUNNING: "暂停", PAUSED: "继续"}.get(state, "开始识别"))
        self.main_btn.setObjectName("bigHold" if state == RUNNING else "big")
        self.main_btn.style().unpolish(self.main_btn)
        self.main_btn.style().polish(self.main_btn)
        self.main_btn.setEnabled(state != FINISHING)
        self.finish_btn.setEnabled(state == IDLE and self.ctrl.session is not None)
        self.new_btn.setEnabled(state in (IDLE, FINALIZED))

    # ---------- 显示 ----------
    def _remove_exception(self, seq: int) -> None:
        for i in range(self.exc_box.count()):
            w = self.exc_box.itemAt(i).widget()
            if isinstance(w, ExceptionItem) and w.seq == seq:
                self.exc_box.removeWidget(w)
                w.deleteLater()
                return

    def _raw_thumb(self, seq: int) -> Path | None:
        s = self.ctrl.session
        if not s:
            return None
        raws = sorted((s.batch_dir / "raw").glob(f"{seq:04d}_*"))
        return raws[0] if raws else None

    def _show_latest(self, e: SheetEvent) -> None:
        if not e.result:
            return
        p = Path(e.result)
        try:
            r = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.prev_title.setText(f"#{e.seq:04d}　最近一张，正面")
        self.preview.set_image(p.parent / r["pages"].get("front", ""))
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
        self.readout_values["absent"].setText("已涂" if (st.get("absent") or {}).get("value") else "未涂")
        ok = sum(1 for o in r["objective"] if o["status"] == "ok")
        self.readout_values["obj"].setText(f"{ok} / {len(r['objective'])}")
        ocr = {q.get("ocr_status") for q in r["subjective"]}
        self.readout_values["sub"].setText(f"切图 {len(r['subjective'])}" + ("，待转写" if "pending" in ocr else ""))
        f = r["scan"]["faces"].get("front", {})
        self.readout_values["align"].setText(f"{f.get('align_method', '—')}，倾斜 {f.get('skew_deg', 0):.1f}°")

    def _refresh_counts(self) -> None:
        evs = self.events.values()
        ok = sum(1 for e in evs if e.ok)
        phys = sum(1 for e in evs if not e.ok and e.seq not in self._voided)
        warns = sum(e.warnings for e in evs if e.ok)
        self.t_scanned.set_value(len(self.events))
        self.t_ok.set_value(ok)
        self.t_err.set_value(phys)
        self.t_warn.set_value(warns)
        self.exc_header.findChild(QLabel).setText(f"物理异常 {phys}")
        self.warn_header.findChild(QLabel).setText(f"数据警告 {warns}")
        warned = [e for e in sorted(evs, key=lambda e: e.seq) if e.ok and e.warnings][:8]
        self.warn_list.setText("<br>".join(
            f"<span style='color:{theme.WARN_FILL}'>■</span> <b>#{e.seq:04d}</b>　{e.warnings} 条警告" for e in warned)
            or f"<span style='color:{theme.INK3}'>暂无</span>")
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

    def _tick(self) -> None:
        now = time.monotonic()
        recent = [t for t in self.done_times if now - t <= 60]
        if len(recent) >= 2:
            span = max(now - recent[0], 1e-3)
            self.t_speed.set_value(int(round(len(recent) / span * 60)))
        else:
            self.t_speed.set_value(0)
