from PySide6.QtCore import Qt

from scan_answer.app.library import ExamInfo, create_exam, list_exams
from scan_answer.app.settings import AppSettings, bundled_templates_dir, load_settings
from scan_answer.ui.controller import IDLE, ScanController
from scan_answer.ui.main_window import MainWindow
from scan_answer.ui.widgets.mosaic import ERR, OK, WARN

from conftest import TEMPLATE_DIR

from ocr_mock import MockOcrServer


def make_window(qtbot, tmp_path):
    settings = AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(bundled_templates_dir())],
                           workers=0, operator="李老师")
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    return win, ctrl


def test_create_exam_scan_and_browse_results(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        assert ep.f_template.count() == 1 and ep.f_operator.text() == "李老师"
        ep.f_name.setText("2026 研考")
        ep.f_subject.setText("管综")
        qtbot.mouseClick(ep.create_btn, Qt.MouseButton.LeftButton)
        assert win.stack.currentWidget() is win.scan_page
        assert [e.info.name for e in list_exams(win.settings.output_root)] == ["2026 研考"]
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        qtbot.mouseClick(sp.main_btn, Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: len(sp.events) == 4 and ctrl.state == IDLE, timeout=120000)
        assert sp.t_scanned.value.text() == "4" and sp.t_err.value.text() == "1"
        assert sp.mosaic.cells[3].state == ERR and {sp.mosaic.cells[i].state for i in (1, 2, 4)} <= {OK, WARN}
        assert sp.exc_box.count() == 2  # 1 个异常条目 + 末尾的伸缩项
        # 有未处理的物理异常时不能结束批次
        sp.finalize()
        assert ctrl.state == IDLE and "物理异常未处理" in no_dialogs[-1][1]
        # 点拼图格子跳到结果页
        sp.mosaic.cellClicked.emit(2)
        assert win.stack.currentWidget() is win.results_page
        rp = win.results_page
        assert rp.current.seq == 2 and rp.answers.rowCount() == 55
        assert rp.card.image_size()[0] > 3000
        rp.filter.buttons["err"].click()
        assert [r.seq for r in rp.visible_rows()] == [3]
        rp.filter.buttons["all"].click()
        rp.search.setText("0004")
        assert [r.seq for r in rp.visible_rows()] == [4]
        rp.select_seq(4)
        rp.face_seg.buttons["back"].click()
        assert rp.face == "back" and rp.card.image_size()[0] > 3000
        # 考试页能看到批次
        win.show_page("exam")
        assert ep.batch_table.rowCount() == 1 and ep.batch_table.item(0, 4).text() == "1"
    finally:
        ctrl.shutdown()


def test_settings_page_saves_and_tests_connection(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.settings_page
        sp.workers.setValue(3)
        sp.dpi.buttons["600"].click()
        sp.ocr_on.setChecked(True)
        with MockOcrServer("ok") as srv:
            sp.base_url.setText(srv.url)
            sp.api_key.setText("k")
            sp.model.setText("mock-vl")
            sp.test_connection()
            qtbot.waitUntil(lambda: "连接正常" in sp.test_result.text(), timeout=10000)
        assert sp.save()
        s = load_settings()
        assert s.workers == 3 and s.scan.dpi == 600 and s.ocr_config().model == "mock-vl"
        assert ctrl.workers == 3
        sp.base_url.setText("ftp://bad")
        sp.test_connection()
        assert "base_url" in sp.test_result.text()
    finally:
        ctrl.shutdown()


def test_template_page_lists_and_adds(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        tp = win.template_page
        assert tp.table.rowCount() == 1 and tp.table.item(0, 0).text() == "管综199"
        (tmp_path / "extra").mkdir()
        tp.add_dir(str(tmp_path / "extra"))
        assert str(tmp_path / "extra") in load_settings().template_dirs
    finally:
        ctrl.shutdown()


def test_aborted_exam_switch_preserves_state(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    """close_batch 失败时 set_exam 不应切换考试，也不更新顶栏。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        # 先用正常考试进入扫描页
        exam1 = create_exam(win.settings.output_root, ExamInfo("考试1", "科目1"), TEMPLATE_DIR)
        win.start_scan(exam1)
        assert win.scan_page.exam is exam1
        old_context = win.context_label.text()
        # 让 close_batch 抛异常
        monkeypatch.setattr(ctrl, "close_batch", lambda **kw: (_ for _ in ()).throw(RuntimeError("模拟超时")))
        exam2 = create_exam(win.settings.output_root, ExamInfo("考试2", "科目2"), TEMPLATE_DIR)
        win.start_scan(exam2)
        # 考试不应切换
        assert win.scan_page.exam is exam1
        assert win.context_label.text() == old_context
        # 应有警告对话框
        assert any("模拟超时" in msg for _, msg in no_dialogs)
    finally:
        monkeypatch.undo()
        ctrl.shutdown()


def test_results_page_broken_batch_keeps_previous(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    """结果页打开损坏的批次时保留先前状态。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        # 先正常扫描一批
        ep = win.exam_page
        ep.f_name.setText("正常考试")
        ep.f_subject.setText("管综")
        from PySide6.QtCore import Qt as _Qt
        qtbot.mouseClick(ep.create_btn, _Qt.MouseButton.LeftButton)
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        qtbot.mouseClick(sp.main_btn, _Qt.MouseButton.LeftButton)
        qtbot.waitUntil(lambda: len(sp.events) == 4 and ctrl.state == IDLE, timeout=120000)
        # 打开结果页
        sp.mosaic.cellClicked.emit(1)
        rp = win.results_page
        assert rp.current is not None
        old_rows = len(rp.rows)
        # 尝试打开不存在的批次
        result = rp.open_batch(tmp_path / "nonexistent_batch")
        assert result is False
        assert len(rp.rows) == old_rows  # 保留先前状态
        assert any("无法打开批次" in msg for _, msg in no_dialogs)
    finally:
        ctrl.shutdown()


def test_ensure_batch_missing_template_shows_warning(qtbot, app_home, no_dialogs, tmp_path):
    """_ensure_batch 遇到损坏模板目录时显示警告，不影响视图。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        # 设置一个不存在的模板目录的考试
        from scan_answer.app.library import Exam, ExamInfo
        from pathlib import Path
        bad_exam_dir = tmp_path / "bad_exam"
        bad_exam_dir.mkdir()
        bad_tpl = tmp_path / "no_such_template"
        win.scan_page.exam = Exam(info=ExamInfo("坏考试"), directory=bad_exam_dir, template_dir=bad_tpl, created="")
        # _ensure_batch 应该失败并显示警告
        result = win.scan_page._ensure_batch()
        assert result is False
        assert any("无法创建批次" in msg for _, msg in no_dialogs)
        # mosaic 不应被清空（没有调用 _reset_view）
    finally:
        ctrl.shutdown()
