"""主窗口：顶栏 + 左侧图标栏 + 页面区 + 底部状态栏；负责页面之间的跳转。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QThreadPool, QTimer
from PySide6.QtGui import QIcon, QKeySequence, QPainter, QPen, QPixmap, QShortcut
from PySide6.QtWidgets import (QButtonGroup, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QStackedWidget,
                               QToolButton, QVBoxLayout, QWidget)

from .. import __version__
from ..app.library import Exam, template_problem
from ..app.settings import AppSettings
from .help_text import PAGE_GUIDES
from ..pipeline.events import OcrProgress
from . import theme
from .background import Latest, settings_writer
from .controller import BUSY_STATES, ScanController
from .pages.exam_page import BUSY_TIP, ExamPage
from .pages.results_page import ResultsPage
from .pages.scan_page import ScanPage
from .pages.settings_page import SettingsPage
from .pages.template_page import TemplatePage

def help_button_tip(key: str) -> str:
    """状态栏“使用说明 F1”的悬停说明：先列当前页怎么用（与侧栏按钮的说明同源），再说点开能看什么。"""
    title, steps = PAGE_GUIDES[key]
    return ("<b>本页怎么用</b>（" + title + "）<br>" + "<br>".join(f"{i}. {t}" for i, t in enumerate(steps, 1))
            + "<br><br>点这里或按 F1 打开完整的使用说明：整体流程、各页功能、模板区域和容易混的词")


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
        QShortcut(QKeySequence(Qt.Key.Key_F1), self, self.show_help)

        self.exam_page = ExamPage(settings)
        self.scan_page = ScanPage(controller, settings)
        self.template_page = TemplatePage(settings)
        self.results_page = ResultsPage(settings=settings)
        self.settings_page = SettingsPage(settings)
        self.pages = {"exam": self.exam_page, "scan": self.scan_page, "template": self.template_page,
                      "results": self.results_page, "settings": self.settings_page}
        for w in self.pages.values():
            self.stack.addWidget(w)

        # 告知结果页当前正在扫描的批次目录（用于阻止活跃批次的批量重试）
        # 都用控制器在后台解析好的真实路径（session_key），界面上只做比较、不再解析路径
        self.results_page._active_batch_fn = lambda: (
            str(self.ctrl.session_key) if self.ctrl.session_key and self.ctrl.state in BUSY_STATES
            else None)

        # 考试页据此拦住忙碌时的新建考试，并在批次列表标出扫描页打开的批次
        self.exam_page.busy_fn = self._busy
        self.exam_page.open_batch_fn = lambda: str(self.ctrl.session_key) if self.ctrl.session_key else None
        self._results_exam: Exam | None = None  # 结果页所查看批次所属的考试（顶栏在结果页显示它）
        self._close_confirmed = False  # 退出的确认已答过（模板在后台保存好后再关一次窗口时不重复问）
        self._startup = Latest(self, wait_for_modal=True)  # 启动时建输出根目录
        self._scan_check = Latest(self, wait_for_modal=True)  # 进入扫描前的模板检查（要解码底图）在后台执行，只认最后一次

        self.exam_page.startScan.connect(self.start_scan)
        self.exam_page.continueBatch.connect(self.continue_batch)
        self.exam_page.showResults.connect(lambda e, d: self.show_results(e, d))
        self.exam_page.openSettings.connect(lambda: self.show_page("settings"))
        self.scan_page.openResult.connect(lambda d, seq: self.show_results(self.scan_page.exam, d, seq))
        self.results_page.ocrChanged.connect(controller.refresh_ocr_progress)
        # 模板保存后只刷新考试页的模板列表；用户已选过的模板不被改成刚保存的那个
        self.template_page.changed.connect(lambda: self.exam_page.template_saved(
            self.template_page.doc.directory if self.template_page.doc is not None else None))
        self.settings_page.saved.connect(self._settings_saved)
        controller.stateChanged.connect(lambda _s: self._scan_state_changed())
        self.scan_page.src_seg.changed.connect(lambda _v: self._update_status())  # 状态栏“扫描仪 / 文件导入模式”跟着换
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
        theme.set_style(self.context_label, "color:$INK2; font-size:13px;")
        lay.addWidget(self.context_label)
        lay.addStretch(1)
        self.operator_label = QLabel("")
        theme.set_style(self.operator_label, "color:$INK2; font-size:13px;")
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
            title, steps = PAGE_GUIDES[key]
            b.setToolTip("<b>" + title + "</b><br>" + "<br>".join(f"{i}. {t}" for i, t in enumerate(steps, 1)))
            b.setIcon(rail_icon(key, theme.RAIL_INK))
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
        help_btn = QToolButton()
        help_btn.setObjectName("statusHelp")
        help_btn.setText("使用说明 F1")
        theme.set_style(help_btn, "#statusHelp { border: 1px solid $LINE2; border-radius: 4px; background: $PANEL;"
                                  " color: $INK; font-size: 12px; padding: 1px 10px; }"
                                  "#statusHelp:hover { border-color: $ACCENT; }")
        self.help_btn = help_btn
        help_btn.clicked.connect(self.show_help)
        lay.addWidget(help_btn)
        v = QLabel(f"v{__version__}")
        lay.addWidget(v)
        return w

    def show_help(self) -> None:
        from .widgets.guide import HelpDialog
        HelpDialog(self).exec()

    # ---------- 导航 ----------
    def refresh_theme(self) -> None:
        for key, button in self.rail_buttons.items():
            button.setIcon(rail_icon(key, theme.INK if button.isChecked() else theme.RAIL_INK))

    def show_page(self, key: str) -> None:
        self._cancel_scan_check()
        current = self.stack.currentWidget()
        if key == "template" and current is self.template_page and self.template_page.doc is not None:
            self.template_page._back()  # 编辑器里再点“模板”：回到模板列表（有未保存的修改时先问）
            return
        # 设置有未保存的修改：询问；选保存时在后台写设置文件，写好才切换（期间图标栏仍停在设置）
        if key != "settings" and current is self.settings_page:
            self._restore_rail("settings")
            self.settings_page.leave(lambda: self._switch_page(key, current))
            return
        # 模板有未保存的修改：询问；选保存时在后台保存，保存成功才切换（期间图标栏仍停在模板）
        if key != "template" and current is self.template_page:
            self._restore_rail("template")
            self.template_page.leave(lambda: self._switch_page(key, current))
            return
        self._switch_page(key, current)

    def _restore_rail(self, key: str) -> None:
        """取消或推迟切换：图标栏选中状态还原。"""
        for k, b in self.rail_buttons.items():
            b.setChecked(k == key)

    def _switch_page(self, key: str, current) -> None:
        self.stack.setCurrentWidget(self.pages[key])
        for k, b in self.rail_buttons.items():
            b.setChecked(k == key)
            b.setIcon(rail_icon(k, theme.INK if k == key else theme.RAIL_INK))
        if key == "exam":
            self.exam_page.refresh()
        elif key == "template":
            self.template_page.refresh()
        elif key == "settings" and current is not self.settings_page:
            self.settings_page.load()  # 扫描页会记住扫描参数并写设置：每次进入都按当前设置重新填表
        elif key == "results" and hasattr(self.results_page, "refresh"):
            self.results_page.refresh()
        self.help_btn.setToolTip(help_button_tip(key))
        self._update_context()

    def _busy(self) -> bool:
        return self.ctrl.state in BUSY_STATES

    def _busy_redirect(self) -> bool:
        """扫描忙碌（含暂停）时不能切换考试或批次：提示并跳到扫描页。两个入口行为一致。"""
        if not self._busy():
            return False
        QMessageBox.information(self, "提示", BUSY_TIP)
        self.show_page("scan")
        return True

    def start_scan(self, exam: Exam) -> None:
        if self._busy_redirect():
            return
        # 先检查模板：不可用时不关闭当前批次，也不进入扫描页（否则到“开始识别”才报错）。
        # 检查要解码模板底图，放到后台；期间“进入扫描”显示进度
        self.exam_page.set_preparing(True)
        session = self.ctrl.session

        def checked(problem: str | None) -> None:
            self.exam_page.set_preparing(False)
            if self.ctrl.session is not session:  # 检查期间换了批次：不再按旧的操作切换考试
                return
            if self._busy_redirect():  # 检查期间开始了扫描
                return
            if problem:
                QMessageBox.warning(self, "模板不可用", problem)
                return

            # 上一批在后台关闭：期间“扫描新批次”显示“正在进入扫描…”，关好再切到扫描页（用户已去别的页面时不拉回）
            if self.scan_page.set_exam(exam, then=self._enter_scan_from_exam,
                                       finished=lambda: self.exam_page.set_preparing(False)) and self.ctrl.switching:
                self.exam_page.set_preparing(True, "正在进入扫描…")

        def failed(e: BaseException) -> None:
            self.exam_page.set_preparing(False)
            QMessageBox.warning(self, "模板不可用", f"{type(e).__name__}: {e}")

        self._scan_check.submit(lambda: template_problem(exam), checked, failed)

    def _cancel_scan_check(self) -> None:
        """用户做了别的操作（切换页面、继续批次、查看结果）：还没回来的“进入扫描”检查作废。"""
        self._scan_check.cancel()
        self.exam_page.set_preparing(False)

    def _enter_scan_from_exam(self) -> None:
        """后台打开 / 关闭批次完成：用户还在考试页时才切到扫描页。"""
        if self.stack.currentWidget() is self.exam_page:
            self.show_page("scan")

    def continue_batch(self, exam: Exam, batch_dir: str) -> None:
        if self.ctrl.switching:  # 上一次打开 / 切换还没完成：不动（也不清掉进行中的提示）
            return
        self._cancel_scan_check()
        if self._busy_redirect():
            return

        # 批次在后台恢复：期间考试页该批次的“继续扫描”显示“正在打开…”；打开失败时扫描页会提示
        if self.scan_page.open_batch(exam, batch_dir, then=self._enter_scan_from_exam,
                                     finished=lambda: self.exam_page.set_opening(None)):
            self.exam_page.set_opening(Path(batch_dir))

    def show_results(self, exam: Exam | None, batch_dir: str, seq: int | None = None) -> None:
        self._cancel_scan_check()
        if not self.results_page.open_batch(batch_dir, seq):
            return
        self._results_exam = exam
        self.show_page("results")

    def _update_context(self) -> None:
        """顶栏跟随当前页面：结果页显示所查看批次的考试，其他页面显示扫描页正在扫描的考试。"""
        exam = self.scan_page.exam
        if self.stack.currentWidget() is self.results_page and self._results_exam is not None:
            exam = self._results_exam
        i = exam.info if exam else None
        self.context_label.setText("　|　".join(x for x in (i.name, i.subject, i.site) if x) if i else "")
        self.operator_label.setText(f"操作员 {i.operator}" if i and i.operator else "")
        self._update_status()

    def _scan_state_changed(self) -> None:
        self._update_context()
        if hasattr(self.results_page, "on_scan_state_changed"):
            self.results_page.on_scan_state_changed()
        if self.stack.currentWidget() is self.exam_page:
            self.exam_page.refresh()  # 批次列表里“扫描中 / 已打开”的标注随状态更新

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
        template = s.manifest["template"] if s else None
        template_label = (f"{template['name']}（v{template['version']}）" if template
                          else exam.template_label if exam else "")
        ocr_enabled = s.manifest.get("ocr", False) if s else bool(self.settings.ocr)
        texts = ["扫描仪模式" if self.scan_page.using_scanner() else "文件导入模式",
                 f"模板 {template_label}" if template_label else "未选择考试",
                 f"批次 {s.batch_id}" if s else "无进行中的批次",
                 self.ocr_text or ("转写已开启" if ocr_enabled else "转写未开启")]
        for lab, t in zip(self.status_labels, texts):
            lab.setText(t)

    def _settings_saved(self) -> None:
        self.ctrl.workers = self.settings.workers
        self.scan_page.apply_scan_defaults()
        self.exam_page.refresh()
        if hasattr(self.results_page, "on_settings_changed"):
            self.results_page.on_settings_changed()
        self._update_status()

    def closeEvent(self, e) -> None:
        if self.ctrl.switching or self.template_page.busy or self.settings_page.saving:
            QMessageBox.information(self, "请稍候", "正在打开 / 关闭批次或处理模板，完成后再退出。")
            e.ignore()
            return
        confirmed, self._close_confirmed = self._close_confirmed, False
        if not confirmed and not self._confirm_close():
            e.ignore()
            return
        if self.template_page.doc is not None and self.template_page.doc.dirty:
            # 模板有未保存的修改：最后再问（前面的问题都答过了）；选保存时在后台保存，保存成功后再关一次窗口
            e.ignore()
            self.template_page.leave(self._close_after_confirm)
            return
        if self.stack.currentWidget() is self.settings_page and self.settings_page.is_dirty():
            # 设置有未保存的修改：问；选保存时在后台写好后再关一次窗口
            e.ignore()
            self.settings_page.leave(self._close_after_confirm)
            return
        # 等后台任务做完再退出：新建考试、准备批次会写模板快照，中途结束进程会留下写了一半的文件。
        # 先全部作废：没开始的不再执行，做完的也不再回调（不在退出过程中建批次、开始识别）。
        # 等待期间窗口先隐藏：用户点了关闭就看到窗口关掉，不会对着一个卡住的窗口
        Latest.cancel_all()
        self.hide()
        QThreadPool.globalInstance().waitForDone(30000)
        settings_writer().wait_idle(10)  # 记住的扫描参数等设置写完再退出
        self.ctrl.shutdown()  # 取消全部转写队列（当前与后台）
        e.accept()

    def ensure_output_root(self) -> None:
        """启动时在后台建输出根目录，建好后刷新考试列表；建不了时提示到设置页修改。"""
        root = Path(self.settings.output_root)

        def made(_value: object) -> None:
            self.exam_page.refresh()

        def failed(e: BaseException) -> None:
            QMessageBox.warning(self, "提示", f"无法创建输出目录：{e}\n请在设置页修改输出根目录。")

        self._startup.submit(lambda: root.mkdir(parents=True, exist_ok=True), made, failed)

    def _close_after_confirm(self) -> None:
        self._close_confirmed = True  # 退出的问题已经答过：再关一次时不重复问
        QTimer.singleShot(0, self.close)  # 可能正在 closeEvent 里（选了“放弃修改”）：下一轮再关

    def _confirm_close(self) -> bool:
        """退出前的确认（交付处理中、批次进行中、转写未完成）；用户取消时返回 False。"""
        panel = self.results_page.delivery_panel
        if panel is not None and panel.busy:
            QMessageBox.information(self, "交付处理中", "请等待交付检查或目录生成完成后再退出。")
            return False
        if self._busy():
            if QMessageBox.question(self, "退出", "批次还在进行中，确定退出？已识别的结果都已保存，"
                                                  "下次可在“考试”页继续扫描。") != QMessageBox.StandardButton.Yes:
                return False
        n = self.ctrl.ocr_pending()
        if n:
            if QMessageBox.question(self, "退出", f"还有 {n} 项手写转写未完成。退出后剩余项保留为“等待转写”，"
                                                  "下次“继续扫描”该批次时会接着转写。确定退出？"
                                    ) != QMessageBox.StandardButton.Yes:
                return False
        return True

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
    settings = load_settings()  # 界面按设置搭建，窗口出现前读一次
    ctrl = ScanController(workers=settings.workers)
    win = MainWindow(settings, ctrl)
    win.show()
    win.ensure_output_root()
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
