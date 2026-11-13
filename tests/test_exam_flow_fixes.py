"""考试/批次生命周期、设置页、主窗口的问题修复回归测试。"""
import json
import shutil

from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QMessageBox

from scan_answer.app.library import (ExamInfo, create_exam, ensure_exam_snapshot, exam_totals, list_batches,
                                     list_exams, load_exam, output_root_problem, template_problem)
from scan_answer.app.settings import AppSettings, bundled_templates_dir, load_settings, save_settings, settings_path
from scan_answer.ocr.config import OcrConfig
from scan_answer.pipeline.session import BatchSession
from scan_answer.ui.controller import IDLE, PAUSED, ScanController
from scan_answer.ui.main_window import MainWindow
from scan_answer.ui.pages.exam_page import BUSY_TIP, CONTINUE_TIP, FINALIZED_TIP

from conftest import TEMPLATE_DIR, settle


def make_window(qtbot, tmp_path, **kw):
    settings = AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(bundled_templates_dir())],
                           workers=0, operator="李老师", **kw)
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    settle()  # 考试页在后台读取模板与考试
    return win, ctrl


def empty_hint(view) -> str:
    return view._empty_hint.label.text()


def mark_finalized(batch_dir):
    """直接把 manifest 标成已结算（空批次不能真正结算；考试页只读这个标记）。"""
    m = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    m["finalized"] = True
    (batch_dir / "manifest.json").write_text(json.dumps(m), encoding="utf-8")


# ---------- 1 / 2 / 11c：批次列表 ----------
def test_refresh_keeps_selected_exam_and_batch(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        out = win.settings.output_root
        for name in ("考试A", "考试B"):
            e = create_exam(out, ExamInfo(name), TEMPLATE_DIR)
            for bid in ("b1", "b2"):
                BatchSession.create(e.directory, e.template_dir, batch_id=bid)
        ep = win.exam_page
        ep.refresh()
        settle()  # 考试页在后台刷新
        # 选中非第一行的考试和批次（refresh 以前会重置回第一行）
        ep.exam_table.selectRow(1)
        exam_dir = ep.selected().directory
        ep.batch_table.selectRow(1)
        batch_dir = ep.batches[1].directory
        assert ep.cont_btn.toolTip() == CONTINUE_TIP
        mark_finalized(batch_dir)
        # 回到考试页、保存设置都会 refresh：选中的考试与批次保持不变
        win.show_page("exam")
        settle()  # 考试页在后台刷新
        assert ep.selected().directory == exam_dir and ep.batches[ep.batch_table.currentRow()].directory == batch_dir
        # 已结算批次：“继续扫描”的提示与扫描页的“追加 / 新建”选择一致
        assert ep.cont_btn.toolTip() == FINALIZED_TIP
        win.settings_page.operator.setText("王老师")
        assert win.settings_page.save()
        settle()
        assert ep.selected().directory == exam_dir and ep.batches[ep.batch_table.currentRow()].directory == batch_dir
        ep.batch_table.selectRow(0)
        assert ep.cont_btn.toolTip() == CONTINUE_TIP
        # 选中的考试被删掉：退回第一行
        shutil.rmtree(exam_dir)
        ep.refresh()
        settle()  # 考试页在后台刷新
        assert ep.exam_table.currentRow() == 0 and ep.selected().directory != exam_dir
    finally:
        ctrl.shutdown()


def test_batch_list_marks_batch_open_in_scan_page(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        win.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert win.scan_page._ensure_batch()
        win.show_page("exam")
        settle()  # 考试页在后台刷新
        ep = win.exam_page
        assert ep.batch_table.item(0, 8).text() == "未结束（已打开）"
        ctrl.state = PAUSED
        ep.refresh()
        settle()  # 考试页在后台刷新
        assert ep.batch_table.item(0, 8).text() == "未结束（扫描中）"
    finally:
        ctrl.state = IDLE
        ctrl.shutdown()


def test_exam_page_lead_text_matches_behaviour(qtbot, app_home, no_dialogs, tmp_path):
    from PySide6.QtWidgets import QLabel
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        texts = [w.text() for w in win.exam_page.findChildren(QLabel)]
        assert not any("结束批次后可在结果页查看" in t for t in texts)
    finally:
        ctrl.shutdown()


# ---------- 3 / 11a：设置页 ----------
def test_settings_reload_on_enter_and_save_keeps_scan_params(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.settings_page
        win.show_page("settings")
        assert sp.dpi.value() == "300"
        win.show_page("scan")
        # 扫描页记住扫描参数（_remember_scan_params 直接改设置并写盘）
        win.settings.scan.dpi, win.settings.scan.color = 600, "color"
        save_settings(win.settings)
        win.show_page("settings")
        assert sp.dpi.value() == "600" and sp.color.value() == "color"  # 进入时按当前设置重新填表
        # 停留在设置页期间扫描参数又被改：保存只覆盖用户实际改的项
        win.settings.scan.dpi = 400
        sp.workers.setValue(5)
        assert sp.save()
        s = load_settings()
        assert s.workers == 5 and s.scan.dpi == 400 and s.scan.color == "color"
        assert win.settings.scan.dpi == 400 and sp.dpi.value() == "400"
    finally:
        ctrl.shutdown()


def test_leaving_settings_with_unsaved_changes_asks(qtbot, app_home, no_dialogs, tmp_path):
    B = QMessageBox.StandardButton
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.settings_page
        win.show_page("settings")
        win.show_page("exam")  # 没有修改：不询问
        assert not no_dialogs
        win.show_page("settings")
        sp.workers.setValue(7)
        no_dialogs.question_answer = B.Cancel
        win.show_page("exam")
        assert win.stack.currentWidget() is sp and sp.workers.value() == 7
        assert win.rail_buttons["settings"].isChecked() and "未保存" in no_dialogs[-1][1]
        no_dialogs.question_answer = B.Discard
        win.show_page("exam")
        assert win.stack.currentWidget() is win.exam_page
        assert sp.workers.value() == 0 and win.settings.workers == 0  # 放弃：表单还原，设置不变
        win.show_page("settings")
        sp.workers.setValue(3)
        no_dialogs.question_answer = B.Save
        win.show_page("exam")
        assert win.stack.currentWidget() is win.exam_page and load_settings().workers == 3 and ctrl.workers == 3
    finally:
        ctrl.shutdown()


def test_empty_output_root_is_rejected_and_restored(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.settings_page
        old = win.settings.output_root
        sp.output_root.setText("  ")
        assert not sp.save()
        assert "输出根目录不能为空" in no_dialogs[-1][1]
        assert sp.output_root.text() == old and win.settings.output_root == old
        assert "已保存" not in sp.save_status.text()
    finally:
        ctrl.shutdown()


# ---------- 8：关闭转写保留配置 ----------
def test_disabling_ocr_keeps_connection_config(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        sp = win.settings_page
        sp.ocr_on.setChecked(True)
        sp.base_url.setText("https://x/v1")
        sp.api_key.setText("k")
        sp.model.setText("m")
        assert sp.save() and load_settings().ocr_config().model == "m"
        sp.ocr_on.setChecked(False)
        assert sp.save()
        s = load_settings()
        assert s.ocr is None and s.ocr_config() is None  # 读取方语义不变：未启用时得到 None
        assert s.ocr_saved["base_url"] == "https://x/v1" and s.ocr_saved["api_key"] == "k"
        assert win.settings.ocr is None and "转写未开启" in win.status_labels[3].text()
        sp.load()
        assert not sp.ocr_on.isChecked() and sp.base_url.text() == "https://x/v1" and sp.model.text() == "m"
        sp.ocr_on.setChecked(True)  # 重新启用不用再填
        assert sp.save()
        s = load_settings()
        assert s.ocr_config() == OcrConfig("https://x/v1", "k", "m") and s.ocr_saved is None
    finally:
        ctrl.shutdown()


def test_old_settings_without_ocr_saved_still_load(app_home):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps({"ocr": None, "workers": 1}), encoding="utf-8")
    s = load_settings()
    assert s.ocr is None and s.ocr_saved is None and s.workers == 1
    settings_path().write_text(json.dumps({"ocr": None, "ocr_saved": {"base_url": "https://x/v1", "api_key": 3}}),
                               encoding="utf-8")
    s = load_settings()
    assert s.ocr_config() is None and s.ocr_saved == {"base_url": "https://x/v1"}


# ---------- 4 / 5：忙碌时切换考试 ----------
def test_busy_entries_redirect_to_scan_page_consistently(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        exam = create_exam(win.settings.output_root, ExamInfo("考试"), TEMPLATE_DIR)
        other = create_exam(win.settings.output_root, ExamInfo("另一场"), TEMPLATE_DIR)
        batch = BatchSession.create(other.directory, other.template_dir, batch_id="b1")
        win.start_scan(exam)
        settle()  # 后台准备（模板检查、创建批次）完成
        ctrl.state = PAUSED  # 暂停也算忙碌
        for action in (lambda: win.start_scan(other), lambda: win.continue_batch(other, str(batch.batch_dir))):
            win.show_page("exam")
            n = len(no_dialogs)
            action()
            assert len(no_dialogs) == n + 1 and no_dialogs[-1][1].endswith(BUSY_TIP)
            assert "停止导入剩余文件 / 停止进纸" in BUSY_TIP and "暂停并结束批次" not in BUSY_TIP
            assert win.stack.currentWidget() is win.scan_page and win.scan_page.exam is exam
    finally:
        ctrl.state = IDLE
        ctrl.shutdown()


def test_create_exam_while_busy_does_not_create(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ctrl.state = PAUSED
        ep.f_name.setText("扫描中新建")
        ep._create()
        settle()  # 后台准备（模板检查、创建批次）完成
        assert list_exams(win.settings.output_root) == [] and no_dialogs[-1][1].endswith(BUSY_TIP)
        assert ep.f_name.text() == "扫描中新建"
    finally:
        ctrl.state = IDLE
        ctrl.shutdown()


# ---------- 6 / 7：新建考试表单 ----------
def test_create_form_enter_reset_and_duplicate(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        ep.f_template.setCurrentIndex(ep.f_template.findData(str(TEMPLATE_DIR)))
        ep.f_name.setText("期中")
        ep.f_name.returnPressed.emit()  # 回车只跳到下一项，不创建
        assert list_exams(win.settings.output_root) == []
        ep.f_subject.setText("管综")
        ep.f_operator.setText("临时操作员")
        ep._create()
        settle()  # 后台准备（模板检查、创建批次）完成
        assert [e.info.name for e in list_exams(win.settings.output_root)] == ["期中"]
        assert win.stack.currentWidget() is win.scan_page
        # 创建成功后清空表单，操作员恢复默认，模板保留
        assert ep.f_name.text() == "" and ep.f_subject.text() == "" and ep.f_operator.text() == "李老师"
        assert ep.f_template.currentData() == str(TEMPLATE_DIR)
        # 同名考试：确认时选否则不创建
        win.show_page("exam")
        ep.f_name.setText("期中")
        ep.f_subject.setText("管综")
        no_dialogs.question_answer = QMessageBox.StandardButton.No
        ep._create()
        settle()  # 后台准备（模板检查、创建批次）完成
        assert "同名考试" in no_dialogs[-1][1] and len(list_exams(win.settings.output_root)) == 1
        no_dialogs.question_answer = QMessageBox.StandardButton.Yes
        ep._create()
        settle()  # 后台准备（模板检查、创建批次）完成
        assert len(list_exams(win.settings.output_root)) == 2
    finally:
        ctrl.shutdown()


def test_operator_follows_default_unless_edited(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep, sp = win.exam_page, win.settings_page
        win.show_page("settings")
        sp.operator.setText("王老师")
        assert sp.save()
        settle()  # 考试页在后台刷新
        assert ep.f_operator.text() == "王老师"
        ep.f_operator.setText("手填")
        win.show_page("settings")
        sp.operator.setText("赵老师")
        assert sp.save()
        win.show_page("exam")
        settle()
        assert ep.f_operator.text() == "手填"
    finally:
        ctrl.shutdown()


# ---------- 9：顶栏跟随页面 ----------
def test_header_context_follows_current_page(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        out = win.settings.output_root
        a = create_exam(out, ExamInfo("考试A", "科目A"), TEMPLATE_DIR)
        b = create_exam(out, ExamInfo("考试B", "科目B"), TEMPLATE_DIR)
        batch = BatchSession.create(b.directory, b.template_dir, batch_id="b1")
        win.start_scan(a)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert "考试A" in win.context_label.text()
        win.show_results(b, str(batch.batch_dir))
        assert win.stack.currentWidget() is win.results_page and "考试B" in win.context_label.text()
        win.show_page("scan")
        assert "考试A" in win.context_label.text()
        win.show_page("results")
        assert "考试B" in win.context_label.text()
    finally:
        ctrl.shutdown()


# ---------- 10：模板不可用 ----------
def test_unavailable_template_is_rejected_before_entering_scan(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        out = win.settings.output_root
        good = create_exam(out, ExamInfo("好考试"), TEMPLATE_DIR)
        bad = create_exam(out, ExamInfo("坏考试"), TEMPLATE_DIR)
        win.start_scan(good)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert win.scan_page._ensure_batch()
        session = ctrl.session
        shutil.rmtree(bad.template_dir)
        bad = load_exam(bad.directory)
        assert template_problem(good) is None and "模板快照缺失" in template_problem(bad)
        win.show_page("exam")
        win.start_scan(bad)
        settle()  # 后台准备（模板检查、创建批次）完成
        assert "模板快照缺失，请新建考试并选择模板" in no_dialogs[-1][1]
        assert win.stack.currentWidget() is win.exam_page
        assert win.scan_page.exam is good and ctrl.session is session  # 当前批次没有被关闭
    finally:
        ctrl.shutdown()


def test_missing_snapshot_error_is_readable(tmp_path):
    import pytest
    exam = create_exam(tmp_path / "out", ExamInfo("考试"), TEMPLATE_DIR)
    (exam.template_dir / "template.json").unlink()
    with pytest.raises(ValueError, match="模板快照缺失，请新建考试并选择模板"):
        ensure_exam_snapshot(load_exam(exam.directory))
    with pytest.raises(ValueError, match="模板文件缺失"):
        create_exam(tmp_path / "out", ExamInfo("考试2"), tmp_path / "no_such_template")


# ---------- 11b：输出根目录不可用 ----------
def test_missing_output_root_is_reported(qtbot, app_home, no_dialogs, tmp_path):
    assert output_root_problem(tmp_path) is None
    assert "输出根目录不存在或无法读取" in output_root_problem(tmp_path / "nope")
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        win.settings.output_root = str(tmp_path / "nope")
        win.exam_page.refresh()
        settle()  # 考试页在后台刷新
        hint = empty_hint(win.exam_page.exam_table)
        assert "输出根目录不存在或无法读取" in hint and str(tmp_path / "nope") in hint
        (tmp_path / "nope").mkdir()
        win.exam_page.refresh()
        settle()  # 考试页在后台刷新
        assert "还没有考试" in empty_hint(win.exam_page.exam_table)
    finally:
        ctrl.shutdown()


# ---------- 12：封存交付目录不是批次 ----------
def test_sealed_delivery_dirs_are_not_batches(tmp_path):
    exam = create_exam(tmp_path / "out", ExamInfo("考试"), TEMPLATE_DIR)
    real = BatchSession.create(exam.directory, exam.template_dir, batch_id="real")
    manifest = json.loads((real.batch_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["counts"] = {"total": 5, "ok": 5}
    (real.batch_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    # 按 output/delivery.py 的格式：副本 manifest 带 delivery.sealed，另有 delivery.json
    shutil.copytree(real.batch_dir, exam.directory / "delivery_copy")
    sealed = dict(manifest, delivery={"sealed": True, "revision": "r"})
    (exam.directory / "delivery_copy" / "manifest.json").write_text(json.dumps(sealed), encoding="utf-8")
    (exam.directory / "delivery_copy" / "delivery.json").write_text("{}", encoding="utf-8")
    shutil.copytree(real.batch_dir, exam.directory / "sealed_only")
    (exam.directory / "sealed_only" / "manifest.json").write_text(json.dumps(sealed), encoding="utf-8")
    assert [b.batch_id for b in list_batches(exam.directory)] == ["real"]
    assert exam_totals(exam.directory)["total"] == 5
    assert ensure_exam_snapshot(load_exam(exam.directory)) == exam.template_dir


# ---------- 13：模板保存不覆盖用户选择 ----------
def test_template_save_keeps_user_chosen_template(qtbot, app_home, no_dialogs, tmp_path):
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        ep = win.exam_page
        names = [ep.f_template.itemData(i) for i in range(ep.f_template.count())]
        other = next(d for d in names if d != str(TEMPLATE_DIR))
        ep.template_saved(other)  # 用户没选过：默认选中刚保存的
        settle()
        assert ep.f_template.currentData() == other
        i = ep.f_template.findData(str(TEMPLATE_DIR))
        ep.f_template.setCurrentIndex(i)
        ep.f_template.activated.emit(i)  # 用户手动选择
        ep.template_saved(other)
        settle()
        assert ep.f_template.currentData() == str(TEMPLATE_DIR)
    finally:
        ctrl.shutdown()


def test_template_check_runs_off_the_ui_thread(qtbot, app_home, no_dialogs, tmp_path):
    """点选考试时模板检查在后台线程完成，结果回来后更新“进入扫描”的提示；切换考试后旧结果作废。"""
    import threading
    from unittest import mock
    from scan_answer.ui.pages import exam_page as ep
    win, ctrl = make_window(qtbot, tmp_path)
    try:
        page = win.exam_page
        out = win.settings.output_root
        good = create_exam(out, ExamInfo("好考试"), TEMPLATE_DIR)
        bad = create_exam(out, ExamInfo("坏考试"), TEMPLATE_DIR)
        shutil.rmtree(bad.template_dir)
        threads = []
        real = ep.template_problem

        def spy(exam):
            threads.append(threading.current_thread() is threading.main_thread())
            return real(exam)

        with mock.patch.object(ep, "template_problem", spy):
            page.refresh()
            settle()  # 考试页在后台刷新
            row = next(i for i, e in enumerate(page.exams) if e.directory == bad.directory)
            page.exam_table.selectRow(row)
            qtbot.waitUntil(lambda: "模板不可用" in page.scan_btn.toolTip())
            page.exam_table.selectRow(1 - row)
            QThreadPool.globalInstance().waitForDone()
            qtbot.wait(50)  # 让后台结果的信号送达主线程
        assert threads and not any(threads)  # 检查都不在主线程
        assert page.selected().directory == good.directory and page.scan_btn.toolTip() == ep.SCAN_TIP
    finally:
        ctrl.shutdown()
