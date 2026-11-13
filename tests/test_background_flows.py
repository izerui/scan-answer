"""耗时操作在后台线程执行、界面线程不被占用；连续触发只认最后一次；准备期间按钮给出进度且不可重复点。"""
import threading

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject

from scan_answer.app import library
from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.ui import main_window as main_window_module
from scan_answer.ui.background import Latest
from scan_answer.ui.controller import ScanController
from scan_answer.ui.main_window import MainWindow
from scan_answer.ui.pages import exam_page as exam_page_module
from scan_answer.ui.pages import scan_page as scan_page_module
from scan_answer.ui.widgets import manual_align

from conftest import TEMPLATE_DIR, settle


def make_window(qtbot, tmp_path):
    settings = AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(bundled_templates_dir())], workers=0)
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    settle()  # 考试页在后台读取模板与考试
    return win, ctrl


def off_main(monkeypatch, module, name) -> list[bool]:
    """包装 module.name：记录每次调用是否在界面线程。"""
    calls = []
    real = getattr(module, name)

    def spy(*args, **kwargs):
        calls.append(threading.current_thread() is threading.main_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return calls


# ---------- Latest ----------
def test_latest_delivers_only_the_last_submission(qtbot):
    owner = QObject()
    latest = Latest(owner)
    done = []
    gate = threading.Event()

    def work(i):
        if i == 0:
            gate.wait(5)  # 第一个任务还在执行时，后面的任务陆续提交
        return i

    for i in range(5):
        latest.submit(lambda i=i: work(i), done.append)
    gate.set()
    settle()
    assert done == [4]  # 已在执行的旧任务跑完但结果丢弃，只有最后一次回调


def test_latest_cancel_and_failure(qtbot):
    owner = QObject()
    latest = Latest(owner)
    done, failed = [], []
    latest.submit(lambda: 1, done.append)
    latest.cancel()
    settle()
    assert done == []

    def boom():
        raise ValueError("坏了")

    latest.submit(boom, done.append, failed.append)
    settle()
    assert done == [] and isinstance(failed[0], ValueError)


# ---------- 进入扫描：模板检查 ----------
def test_start_scan_checks_template_off_the_ui_thread(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.exam_page.refresh()
        settle()  # 考试页在后台刷新
        calls = off_main(monkeypatch, main_window_module, "template_problem")
        win.start_scan(exam)
        # 检查期间：按钮显示进度且不可点，还没进入扫描页
        assert win.exam_page.scan_btn.text() == "正在检查模板…" and not win.exam_page.scan_btn.isEnabled()
        assert win.stack.currentWidget() is win.exam_page
        settle()
        assert calls == [False]
        assert win.stack.currentWidget() is win.scan_page and win.scan_page.exam is exam
        assert win.exam_page.scan_btn.text() == "扫描新批次"
    finally:
        ctrl.shutdown()


def test_start_scan_twice_enters_the_last_exam(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        a = create_exam(win.settings.output_root, ExamInfo("A"), TEMPLATE_DIR)
        b = create_exam(win.settings.output_root, ExamInfo("B"), TEMPLATE_DIR)
        win.start_scan(a)
        win.start_scan(b)
        settle()
        assert win.scan_page.exam is b
    finally:
        ctrl.shutdown()


# ---------- 新建考试 ----------
def test_create_exam_runs_off_the_ui_thread_and_blocks_double_click(qtbot, app_home, no_dialogs, tmp_path,
                                                                    monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ep.f_template.setCurrentIndex(ep.f_template.findData(str(TEMPLATE_DIR)))
        ep.f_name.setText("期中")
        calls = off_main(monkeypatch, exam_page_module, "create_exam")
        ep.create_btn.click()
        assert ep.create_btn.text() == "正在创建…" and not ep.create_btn.isEnabled()
        assert not ep.f_name.isEnabled() and not ep.f_template.isEnabled()  # 创建期间表单不可改
        ep.create_btn.click()  # 创建期间再点无效
        settle()
        assert calls == [False]
        assert [e.info.name for e in library.list_exams(win.settings.output_root)] == ["期中"]
        assert ep.create_btn.isEnabled() and ep.create_btn.text() == "创建并进入扫描" and ep.f_name.isEnabled()
        assert win.stack.currentWidget() is win.scan_page
    finally:
        ctrl.shutdown()


def test_create_exam_failure_is_reported(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ep.f_template.setCurrentIndex(ep.f_template.findData(str(TEMPLATE_DIR)))
        ep.f_name.setText("期中")

        def fail(*_a, **_k):
            raise ValueError("模板坏了")

        monkeypatch.setattr(exam_page_module, "create_exam", fail)
        ep.create_btn.click()
        settle()
        assert "创建考试失败：模板坏了" in no_dialogs[-1][1]
        assert ep.create_btn.isEnabled() and win.stack.currentWidget() is win.exam_page
    finally:
        ctrl.shutdown()


# ---------- 开始导入：新建批次 ----------
def test_new_batch_snapshot_is_prepared_off_the_ui_thread(qtbot, app_home, no_dialogs, small_dataset, tmp_path,
                                                         monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()
        sp = win.scan_page
        calls = off_main(monkeypatch, scan_page_module, "ensure_exam_snapshot")
        sp.folder_edit.setText(str(small_dataset))
        sp.main_btn.click()
        # 先在后台列输入文件，再固定模板快照、新建批次：期间主按钮显示进度且不可点
        assert sp.main_btn.text().startswith("正在") and not sp.main_btn.isEnabled()
        assert ctrl.session is None
        qtbot.waitUntil(lambda: ctrl.session is not None, timeout=30000)  # 批次在扫描线程上新建
        assert calls == [False]
        qtbot.waitUntil(lambda: len(sp.events) == 4, timeout=120000)
        assert sp.main_btn.text() != "正在准备批次…"
    finally:
        ctrl.shutdown()


def test_switching_exam_while_preparing_drops_the_old_batch(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        a = create_exam(win.settings.output_root, ExamInfo("A"), TEMPLATE_DIR)
        b = create_exam(win.settings.output_root, ExamInfo("B"), TEMPLATE_DIR)
        win.start_scan(a)
        settle()
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        sp.main_btn.click()
        sp.set_exam(b)  # 准备期间换了考试：不再按 A 创建批次
        settle()
        assert ctrl.session is None and not list(a.directory.glob("*/manifest.json"))
    finally:
        ctrl.shutdown()


# ---------- 手动定位 ----------
def test_manual_align_loads_images_off_the_ui_thread(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()
        sp = win.scan_page
        assert sp._ensure_batch()
        raws = []
        for i in (1, 2):
            p = tmp_path / f"0001_{i}.png"
            Image.fromarray(np.full((30, 40), 200, dtype=np.uint8)).save(p)
            raws.append(p)
        monkeypatch.setattr(sp, "_raw_paths", lambda _seq: raws)
        from scan_answer import imageio
        loads = off_main(monkeypatch, imageio, "imread_gray")
        opened = []

        class FakeDialog:
            def __init__(self, seq, template, bases, paths, parent=None, images=None):
                opened.append((seq, paths, images))

            def exec(self):
                return 0

        monkeypatch.setattr(manual_align, "ManualAlignDialog", FakeDialog)
        sp._add_exception(1, "ALIGN_FAILED", "定位失败")
        item = next(w for w in sp.findChildren(scan_page_module.ExceptionItem) if w.seq == 1)
        sp._relocate(1)
        assert opened == []  # 读取在后台，还没弹出
        # 读取期间按钮变灰并显示“正在打开…”，没反应时不会被连点
        assert [(b.isEnabled(), b.text()) for b in item.relocate_btns] == [(False, "正在打开…")]
        settle()
        assert len(opened) == 1
        assert [(b.isEnabled(), b.text()) for b in item.relocate_btns] == [(True, "手动定位")]
        seq, paths, images = opened[0]
        assert seq == 1 and paths == raws and [img.shape for img in images] == [(30, 40), (30, 40)]
        assert loads == [False, False]  # 两张原图都在后台读好后传给对话框
    finally:
        ctrl.shutdown()


def test_late_template_check_does_not_override_later_navigation(qtbot, app_home, no_dialogs, tmp_path):
    """检查期间用户继续了别的批次或去了别的页面：晚到的检查结果不能关掉批次、也不能把页面拉回扫描页。"""
    from scan_answer.pipeline.session import BatchSession
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        a = create_exam(win.settings.output_root, ExamInfo("A"), TEMPLATE_DIR)
        b = create_exam(win.settings.output_root, ExamInfo("B"), TEMPLATE_DIR)
        batch = BatchSession.create(a.directory, a.template_dir, batch_id="b1")
        win.start_scan(b)
        win.continue_batch(a, str(batch.batch_dir))
        qtbot.waitUntil(lambda: ctrl.session is not None and not ctrl.switching, timeout=30000)  # 批次在后台打开
        session = ctrl.session
        settle()
        assert ctrl.session is session and win.scan_page.exam is a
        assert win.exam_page.scan_btn.text() == "扫描新批次"
        win.show_page("exam")
        win.start_scan(b)
        win.show_page("settings")
        settle()
        assert win.stack.currentWidget() is win.settings_page and ctrl.session is session
    finally:
        ctrl.shutdown()


def test_closing_waits_for_background_work(qtbot, app_home, no_dialogs, tmp_path):
    """退出时等后台任务（如复制模板快照）做完，避免留下写了一半的文件。"""
    import time
    win, ctrl = make_window(qtbot, tmp_path)
    finished = []

    def slow():
        time.sleep(0.3)
        finished.append(True)

    Latest(win).submit(slow, lambda _r: None)
    time.sleep(0.05)  # 确保任务已开始
    win.close()
    assert finished == [True]


def test_wait_for_modal_defers_until_dialog_closes(qtbot):
    from PySide6.QtWidgets import QDialog
    owner = QObject()
    latest = Latest(owner, wait_for_modal=True)
    plain = Latest(owner)
    done, image_done = [], []
    dlg = QDialog()
    qtbot.addWidget(dlg)
    dlg.setModal(True)
    dlg.show()
    latest.submit(lambda: 1, done.append)
    plain.submit(lambda: 2, image_done.append)
    settle()
    qtbot.wait(150)
    assert done == [] and image_done == [2]  # 弹窗开着：会弹窗的操作先不处理，图片加载照常
    dlg.close()
    qtbot.waitUntil(lambda: done == [1])


def test_manual_align_skipped_when_card_voided_while_loading(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()
        sp = win.scan_page
        assert sp._ensure_batch()
        raw = tmp_path / "0001_1.png"
        Image.fromarray(np.full((30, 40), 200, dtype=np.uint8)).save(raw)
        monkeypatch.setattr(sp, "_raw_paths", lambda _seq: [raw])
        opened = []
        monkeypatch.setattr(manual_align, "ManualAlignDialog", lambda *a, **k: opened.append(a))
        sp._relocate(1)
        sp._voided.add(1)  # 读取期间这张卡被作废
        settle()
        assert opened == []
    finally:
        ctrl.shutdown()


def test_closing_drops_queued_callbacks(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    """关窗时后台任务做完了，但排队的回调不能再建批次、开始识别。"""
    from PySide6.QtWidgets import QApplication
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        sp.main_btn.click()
        win.close()
        for _ in range(5):
            QApplication.processEvents()
        assert ctrl.session is None and not list(exam.directory.glob("*/manifest.json"))
    finally:
        ctrl.shutdown()


def test_switching_page_while_creating_exam_stays_put(qtbot, app_home, no_dialogs, tmp_path):
    """新建考试期间切到别的页面：创建完成后不把用户拉回扫描页。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ep.f_template.setCurrentIndex(ep.f_template.findData(str(TEMPLATE_DIR)))
        ep.f_name.setText("期中")
        ep.create_btn.click()
        win.show_page("settings")
        settle()
        assert win.stack.currentWidget() is win.settings_page
        assert [e.info.name for e in library.list_exams(win.settings.output_root)] == ["期中"]  # 考试照常建好
        assert ep.create_btn.isEnabled()
    finally:
        ctrl.shutdown()
