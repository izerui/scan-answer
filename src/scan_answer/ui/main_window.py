"""主窗口：顶栏 + 左侧图标栏 + 页面区 + 底部状态栏；负责页面之间的跳转。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from ..app.library import Exam
from ..app.settings import AppSettings
from ..pipeline.events import OcrProgress
from . import theme
from .controller import FINISHING, PAUSED, RUNNING, ScanController
from .pages.exam_page import ExamPage
from .pages.results_page import ResultsPage
from .pages.scan_page import ScanPage
from .pages.settings_page import SettingsPage
from .pages.template_page import TemplatePage

VERSION = "0.3.0"
PAGES = [("exam", "考试"), ("scan", "扫描"), ("template", "模板"), ("results", "结果")]


def rail_icon(kind: str, color: str) -> QIcon:
    """图标栏的简单几何图标（避免引入图标资源文件）。"""
    pm = QPixmap(40, 40)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    pen = QPen(theme.qcolor(color), 2.4)
    p.setPen(pen)
    if kind == "exam":
        for x, y in ((8, 8), (22, 8), (8, 22), (22, 22)):
            p.drawRect(QRectF(x, y, 10, 10))
    elif kind == "scan":
        p.drawRect(QRectF(7, 16, 26, 14))
        p.drawRect(QRectF(12, 8, 16, 8))
        p.drawLine(12, 24, 28, 24)
    elif kind == "template":
        p.drawRect(QRectF(9, 7, 22, 26))
        p.drawLine(14, 15, 26, 15)
        p.drawLine(14, 21, 26, 21)
    elif kind == "results":
        for y in (11, 20, 29):
            p.drawLine(15, y, 32, y)
            p.drawPoint(9, y)
    elif kind == "settings":
        for y, x in ((11, 14), (20, 26), (29, 18)):
            p.drawLine(8, y, 32, y)
            p.drawEllipse(QRectF(x - 3, y - 3, 6, 6))
    p.end()
    return QIcon(pm)


class MainWindow(QMainWindow):
    def __init__(self, settings: AppSettings, controller: ScanController):
        super().__init__()
        self.settings = settings
        self.ctrl = controller
        self.setWindowTitle("答题卡扫描识别")
        self.resize(1440, 900)
        self.setMinimumSize(1280, 800)
        central = QWidget()
        central.setObjectName("chassis")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())
        mid = QHBoxLayout()
        mid.setSpacing(0)
        mid.addWidget(self._rail())
        self.stack = QStackedWidget()
        mid.addWidget(self.stack, 1)
        root.addLayout(mid, 1)
        root.addWidget(self._statusbar())

        self.exam_page = ExamPage(settings)
        self.scan_page = ScanPage(controller, settings)
        self.template_page = TemplatePage(settings)
        self.results_page = ResultsPage()
        self.settings_page = SettingsPage(settings)
        self.pages = {"exam": self.exam_page, "scan": self.scan_page, "template": self.template_page,
                      "results": self.results_page, "settings": self.settings_page}
        for w in self.pages.values():
            self.stack.addWidget(w)

        self.exam_page.startScan.connect(self.start_scan)
        self.exam_page.continueBatch.connect(self.continue_batch)
        self.exam_page.showResults.connect(lambda e, d: self.show_results(e, d))
        self.scan_page.openResult.connect(lambda d, seq: self.show_results(self.scan_page.exam, d, seq))
        self.template_page.changed.connect(self.exam_page.refresh)
        self.settings_page.saved.connect(self._settings_saved)
        controller.stateChanged.connect(lambda _s: self._update_status())
        controller.ocrProgress.connect(self._ocr_progress)
        controller.finalized.connect(lambda _e: self.exam_page.refresh())
        self.ocr_text = ""
        self._ocr_session = None  # ocr_text 所属的批次：换批次后清空，状态栏只反映当前批次
        self.show_page("exam")
        self._update_status()

    # ---------- 外框 ----------
    def _header(self) -> QWidget:
        w = QWidget()
        w.setObjectName("header")
        w.setFixedHeight(36)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        name = QLabel("答题卡扫描识别")
        name.setStyleSheet("font-weight:600; font-size:13px;")
        lay.addWidget(name)
        lay.addSpacing(16)
        self.context_label = QLabel("")
        self.context_label.setStyleSheet(f"color:{theme.INK2}; font-size:13px;")
        lay.addWidget(self.context_label)
        lay.addStretch(1)
        self.operator_label = QLabel("")
        self.operator_label.setStyleSheet(f"color:{theme.INK2}; font-size:13px;")
        lay.addWidget(self.operator_label)
        return w

    def _rail(self) -> QWidget:
        w = QWidget()
        w.setObjectName("rail")
        w.setFixedWidth(64)
        lay = QVBoxLayout(w)
        lay.setContentsMargins(4, 8, 4, 12)
        lay.setSpacing(4)
        self.rail_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        group.setExclusive(True)

        def add(key: str, label: str) -> None:
            b = QToolButton()
            b.setText(label)
            b.setIcon(rail_icon(key, "#c9ced4"))
            b.setIconSize(QSize(22, 22))
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setCheckable(True)
            b.setFixedSize(56, 56)
            b.clicked.connect(lambda _c=False, k=key: self.show_page(k))
            group.addButton(b)
            self.rail_buttons[key] = b
            lay.addWidget(b)

        for key, label in PAGES:
            add(key, label)
        lay.addStretch(1)
        add("settings", "设置")
        return w

    def _statusbar(self) -> QWidget:
        w = QWidget()
        w.setObjectName("statusbar")
        w.setFixedHeight(28)
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(18)
        self.status_labels = [QLabel() for _ in range(4)]
        for lab in self.status_labels:
            lay.addWidget(lab)
        lay.addStretch(1)
        v = QLabel(f"v{VERSION}")
        lay.addWidget(v)
        return w

    # ---------- 导航 ----------
    def show_page(self, key: str) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.rail_buttons.items():
            b.setChecked(k == key)
            b.setIcon(rail_icon(k, theme.INK if k == key else "#c9ced4"))
        if key == "exam":
            self.exam_page.refresh()
        elif key == "template":
            self.template_page.refresh()

    def _busy(self) -> bool:
        return self.ctrl.state in (RUNNING, PAUSED, FINISHING)

    def start_scan(self, exam: Exam) -> None:
        if self._busy():
            QMessageBox.information(self, "提示", "当前批次还在进行中，请先暂停并结束批次。")
            self.show_page("scan")
            return
        if not self.scan_page.set_exam(exam):
            return
        self._set_context(exam)
        self.show_page("scan")

    def continue_batch(self, exam: Exam, batch_dir: str) -> None:
        if self._busy():
            QMessageBox.information(self, "提示", "当前批次还在进行中，请先暂停并结束批次。")
            return
        if not self.scan_page.open_batch(exam, batch_dir):
            return
        self._set_context(exam)
        self.show_page("scan")

    def show_results(self, exam: Exam | None, batch_dir: str, seq: int | None = None) -> None:
        if not self.results_page.open_batch(batch_dir, seq):
            return
        if exam:
            self._set_context(exam)
        self.show_page("results")

    def _set_context(self, exam: Exam) -> None:
        i = exam.info
        self.context_label.setText("　|　".join(x for x in (i.name, i.subject, i.site) if x))
        self.operator_label.setText(f"操作员 {i.operator}" if i.operator else "")
        self._update_status()

    # ---------- 状态栏 ----------
    def _ocr_progress(self, p: OcrProgress) -> None:
        self._ocr_session = self.ctrl.session
        self.ocr_text = f"转写 {p.done} / {p.total}" + (f"（失败 {p.failed}）" if p.failed else "")
        self._update_status()

    def _update_status(self) -> None:
        s = self.ctrl.session
        if s is not self._ocr_session:
            self.ocr_text, self._ocr_session = "", s
        exam = self.scan_page.exam
        texts = ["导入图片模式（扫描仪接入在后续版本）",
                 f"模板 {exam.template_dir.name}" if exam else "未选择考试",
                 f"批次 {s.batch_id}" if s else "无进行中的批次",
                 self.ocr_text or ("转写未开启" if not self.settings.ocr else "转写已开启")]
        for lab, t in zip(self.status_labels, texts):
            lab.setText(t)

    def _settings_saved(self) -> None:
        self.ctrl.workers = self.settings.workers
        self.exam_page.refresh()
        self._update_status()

    def closeEvent(self, e) -> None:
        if self._busy():
            if QMessageBox.question(self, "退出", "批次还在进行中，确定退出？已识别的结果都已保存，"
                                                  "下次可在“考试”页继续扫描。") != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        n = self.ctrl.ocr_pending()
        if n:
            if QMessageBox.question(self, "退出", f"还有 {n} 项手写转写未完成。退出后剩余项保留为“等待转写”，"
                                                  "下次“继续扫描”该批次时会接着转写。确定退出？"
                                    ) != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.ctrl.shutdown()  # 取消全部转写队列（当前与后台）
        e.accept()


def run_app(argv: list[str] | None = None) -> int:
    import gc
    import multiprocessing
    import sys

    from PySide6.QtWidgets import QApplication

    from ..app.settings import load_settings

    multiprocessing.freeze_support()  # 打包后 spawn 子进程需要
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName("答题卡扫描识别")
    theme.apply_theme(app)
    settings = load_settings()
    mkdir_error = None
    try:
        Path(settings.output_root).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        mkdir_error = str(e)
    ctrl = ScanController(workers=settings.workers)
    win = MainWindow(settings, ctrl)
    win.show()
    if mkdir_error:
        from PySide6.QtWidgets import QMessageBox
        QMessageBox.warning(win, "提示", f"无法创建输出目录：{mkdir_error}\n请在设置页修改输出根目录。")
    code = app.exec()
    ctrl.shutdown()
    import shiboken6
    shiboken6.delete(win)
    del win
    gc.collect()
    _exit_if_ocr_threads_linger(code)
    return code


def _exit_if_ocr_threads_linger(code: int, grace: float = 1.0) -> None:
    """转写队列已取消，但在途的 HTTP 请求最长要等到超时才返回，非守护线程会拖住进程退出。
    稍等片刻后仍有转写线程时直接结束进程：未完成的项在 result.json 中仍是“等待转写”，
    回写本身是原子替换，最多留下一个过期锁文件（超过 60 秒自动清理）。"""
    import os
    import sys
    import threading
    import time

    deadline = time.monotonic() + grace
    for t in threading.enumerate():
        if t.name.startswith("ocr") and t.is_alive():
            t.join(max(0.0, deadline - time.monotonic()))
    if any(t.name.startswith("ocr") and t.is_alive() for t in threading.enumerate()):
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
