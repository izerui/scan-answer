from PySide6.QtCore import Qt

from scan_answer.app.library import ExamInfo, create_exam, list_exams
from scan_answer.app.settings import AppSettings, bundled_templates_dir, load_settings
from scan_answer.ui.controller import IDLE, ScanController
from scan_answer.ui.main_window import MainWindow
from scan_answer.ui.widgets.mosaic import ERR, OK, WARN

from conftest import TEMPLATE_DIR, settle
from test_library import bundled_names

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
        assert ep.f_template.findText("管综199", Qt.MatchFlag.MatchStartsWith) >= 0 and ep.f_operator.text() == "李老师"
        ep.f_template.setCurrentIndex(ep.f_template.findData(str(TEMPLATE_DIR)))
        ep.f_name.setText("2026 研考")
        ep.f_subject.setText("管综")
        qtbot.mouseClick(ep.create_btn, Qt.MouseButton.LeftButton)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert win.stack.currentWidget() is win.scan_page
        assert [e.info.name for e in list_exams(win.settings.output_root)] == ["2026 研考"]
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        qtbot.mouseClick(sp.main_btn, Qt.MouseButton.LeftButton)
        settle()  # 后台准备（模板检查、创建批次）完成
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
        qtbot.waitUntil(lambda: rp.card.image_size()[0] > 3000)  # 卡面图片在后台解码
        rp.filter.buttons["err"].click()
        assert [r.seq for r in rp.visible_rows()] == [3]
        rp.filter.buttons["all"].click()
        rp.search.setText("0004")
        assert [r.seq for r in rp.visible_rows()] == [4]
        rp.select_seq(4)
        rp.face_seg.buttons["back"].click()
        assert rp.face == "back"
        qtbot.waitUntil(lambda: rp.card.image_size()[0] > 3000)
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
        names = [tp.table.item(r, 0).text() for r in range(tp.table.rowCount())]
        assert names == bundled_names() and "管综199" in names
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
        settle()  # 后台准备（模板检查、创建批次）完成
        assert win.scan_page.exam is exam1
        old_context = win.context_label.text()
        # 让 close_batch 抛异常
        monkeypatch.setattr(ctrl, "close_batch", lambda **kw: (_ for _ in ()).throw(RuntimeError("模拟超时")))
        exam2 = create_exam(win.settings.output_root, ExamInfo("考试2", "科目2"), TEMPLATE_DIR)
        win.start_scan(exam2)
        settle()  # 后台准备（模板检查、创建批次）完成
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
        settle()  # 后台准备（模板检查、创建批次）完成
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        qtbot.mouseClick(sp.main_btn, _Qt.MouseButton.LeftButton)
        settle()  # 后台准备（模板检查、创建批次）完成
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
        sp = win.scan_page
        # 先手动设置一些 events 模拟先前扫描状态
        from scan_answer.pipeline.events import SheetEvent
        sp.events[1] = SheetEvent(1, "test", True, None, "", "001", 0, False, None)
        sp.mosaic.set_cell(1, "ok")
        events_before = dict(sp.events)
        cells_before = len(sp.mosaic.cells)
        # 设置一个不存在的模板目录的考试
        from scan_answer.app.library import Exam
        from pathlib import Path
        bad_exam_dir = tmp_path / "bad_exam"
        bad_exam_dir.mkdir()
        bad_tpl = tmp_path / "no_such_template"
        sp.exam = Exam(info=ExamInfo("坏考试"), directory=bad_exam_dir, template_dir=bad_tpl, created="")
        # _ensure_batch 应该失败并显示警告
        result = sp._ensure_batch()
        assert result is False
        assert any("无法创建批次" in msg for _, msg in no_dialogs)
        # mosaic 和 events 不应被清空（_reset_view 未被调用）
        assert sp.events == events_before
        assert len(sp.mosaic.cells) == cells_before
    finally:
        ctrl.shutdown()


def test_double_click_start_no_exception(qtbot, app_home, no_dialogs, small_dataset, tmp_path):
    """快速连续两次调用 start_folder 不应抛出异常。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ep.f_name.setText("双击测试")
        ep.f_subject.setText("管综")
        from PySide6.QtCore import Qt as _Qt
        qtbot.mouseClick(ep.create_btn, _Qt.MouseButton.LeftButton)
        settle()  # 后台准备（模板检查、创建批次）完成
        sp = win.scan_page
        sp.folder_edit.setText(str(small_dataset))
        # 连续两次调用 start_folder，不应有未捕获的异常
        sp.start_folder(small_dataset)
        sp.start_folder(small_dataset)  # 第二次应被静默忽略：准备期间连点只按最后一次创建一个批次
        settle()  # 后台准备（模板检查、创建批次）完成
        assert len(list((sp.exam.directory).glob("*/manifest.json"))) == 1
        qtbot.waitUntil(lambda: len(sp.events) == 4 and ctrl.state == IDLE, timeout=120000)
    finally:
        ctrl.shutdown()


def test_main_window_survives_malformed_files(qtbot, app_home, no_dialogs, tmp_path):
    """输出根目录、模板目录、settings.json 里有合法 JSON 但结构不对的文件时，界面照常启动。"""
    import json

    from scan_answer.app.settings import settings_path
    from test_library import make_bad_tree
    good, out, tpls = make_bad_tree(tmp_path)
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({
        "output_root": str(out), "template_dirs": [str(tpls), str(bundled_templates_dir())],
        "ocr": {"base_url": "https://x/v1", "api_key": "k", "model": "m", "concurrency": "x"}}), encoding="utf-8")
    ctrl = ScanController(workers=0)
    try:
        win = MainWindow(load_settings(), ctrl)
        qtbot.addWidget(win)
        ep = win.exam_page
        assert ep.exam_table.rowCount() == 2 and ep.exam_table.item(0, 0).text() == "好考试"
        assert ep.batch_table.rowCount() == 2
        win.show_page("template")
        assert win.template_page.table.rowCount() == len(bundled_names())  # 结构不对的模板都跳过
        assert win.settings_page.concurrency.value() == 4
    finally:
        ctrl.shutdown()


def test_scan_page_open_broken_batch_keeps_controller(qtbot, app_home, no_dialogs, tmp_path):
    """继续一个打不开的批次：提示错误，控制器和扫描页都保持原批次。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成
        sp = win.scan_page
        assert sp._ensure_batch()
        cur = ctrl.session
        broken = exam.directory / "broken"
        broken.mkdir()
        win.continue_batch(exam, str(broken))
        assert "无法打开批次" in no_dialogs[-1][1]
        assert ctrl.session is cur and sp.batch_label.text() == cur.batch_id
    finally:
        ctrl.shutdown()


def test_start_error_is_shown(qtbot, app_home, no_dialogs, small_dataset, tmp_path, monkeypatch):
    """start 抛出的 RuntimeError 不再被静默吞掉。"""
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成

        def fail(_src):
            raise RuntimeError("模拟失败")
        monkeypatch.setattr(ctrl, "start", fail)
        win.scan_page.start_folder(small_dataset)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert any("无法开始识别" in m and "模拟失败" in m for _, m in no_dialogs)
    finally:
        monkeypatch.undo()
        ctrl.shutdown()


def test_results_page_broken_result_and_hidden_seq(qtbot, small_dataset, tmp_path):
    """result.json 损坏或结构不对：右侧清空并提示，不显示上一个学生；被筛选隐藏的序号先清除筛选再选中。"""
    from scan_answer.batch import run_batch
    from scan_answer.ui.pages.results_page import ResultsPage
    bd = run_batch(TEMPLATE_DIR, small_dataset, tmp_path, batch_id="b1")
    rp = ResultsPage()
    qtbot.addWidget(rp)
    assert rp.open_batch(bd, 1) and rp.current.seq == 1 and rp.answers.rowCount() == 55
    rows = {r.seq: r for r in rp.rows}
    rows[2].result.write_text("{oops", encoding="utf-8")
    rows[4].result.write_text('{"student": 1}', encoding="utf-8")
    assert rp.select_seq(2)
    assert rp.current.seq == 2 and rp.result is None and rp.info.text() == "结果文件损坏"
    assert rp.answers.rowCount() == 0 and rp.title.text() == "#0002"
    assert rp.select_seq(1) and rp.answers.rowCount() == 55
    assert rp.select_seq(4) and rp.info.text() == "结果文件损坏" and rp.answers.rowCount() == 0
    assert rp.open_batch(bd, 2) and rp.info.text() == "结果文件损坏"  # 从拼图格子进来也不抛异常
    # 筛选 + 搜索把 #1 隐藏了
    rp.filter.buttons["err"].click()
    rp.search.setText("0003")
    assert [r.seq for r in rp.visible_rows()] == [3]
    assert rp.open_batch(bd, 1)
    assert rp.current.seq == 1 and rp.filter.value() == "all" and rp.search.text() == ""
    assert rp.answers.rowCount() == 55
    # 序号不存在：右侧清空
    assert rp.open_batch(bd, 99)
    assert rp.current is None and rp.answers.rowCount() == 0 and rp.title.text() == "未选择学生"


def test_scan_preview_broken_result_still_counts(qtbot, app_home, no_dialogs, tmp_path):
    """最近一张的 result.json 结构不对：跳过预览，计数照常刷新。"""
    from scan_answer.pipeline.events import SheetEvent
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.scan_page
        bad = tmp_path / "result.json"
        bad.write_text('{"pages": {}, "student": 1}', encoding="utf-8")
        sp._on_done(SheetEvent(1, "x", True, None, "", "001", 2, False, str(bad)))
        assert sp.t_scanned.value.text() == "1" and sp.t_warn.value.text() == "2"
    finally:
        ctrl.shutdown()


def test_close_prompts_when_ocr_pending(qtbot, app_home, no_dialogs, tmp_path, monkeypatch):
    """关窗时还有转写未完成：提示；选否取消关闭，选是取消全部转写后退出。"""
    from PySide6.QtGui import QCloseEvent
    from PySide6.QtWidgets import QMessageBox
    win, ctrl = make_window(qtbot, tmp_path)
    cancelled = []
    real_cancel = ctrl.cancel_all_ocr
    monkeypatch.setattr(ctrl, "ocr_pending", lambda: 7)
    monkeypatch.setattr(ctrl, "cancel_all_ocr", lambda: (cancelled.append(1), real_cancel()))
    try:
        no_dialogs.question_answer = QMessageBox.StandardButton.No
        e = QCloseEvent()
        win.closeEvent(e)
        assert not e.isAccepted() and ctrl._thread.is_alive() and not cancelled
        assert "还有 7 项手写转写未完成" in no_dialogs[-1][1] and "等待转写" in no_dialogs[-1][1]
        no_dialogs.question_answer = QMessageBox.StandardButton.Yes
        e = QCloseEvent()
        win.closeEvent(e)
        assert e.isAccepted() and cancelled and not ctrl._thread.is_alive()
    finally:
        ctrl.shutdown()
