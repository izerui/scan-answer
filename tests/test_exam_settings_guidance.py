"""考试页、设置页的界面内提示与引导：悬停说明、禁用原因、说明条、空状态、“?”按钮。"""
from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QSpinBox

from scan_answer.app.library import ExamInfo, create_exam
from scan_answer.app.settings import AppSettings, bundled_templates_dir, default_output_root
from scan_answer.ui.pages.exam_page import ExamPage
from scan_answer.ui.pages.settings_page import SettingsPage
from scan_answer.ui.widgets.guide import HelpButton

from conftest import TEMPLATE_DIR


def make_settings(tmp_path, **kw) -> AppSettings:
    kw.setdefault("output_root", str(tmp_path / "out"))
    return AppSettings(template_dirs=[str(bundled_templates_dir())], workers=0, **kw)


def show(qtbot, page):
    qtbot.addWidget(page)
    page.resize(1280, 800)
    page.show()
    qtbot.waitExposed(page)
    return page


def missing_tooltips(page) -> list[str]:
    """页面上所有可见的按钮、下拉框、输入框、数字框中，没有悬停说明的。"""
    missing = []
    for cls in (QAbstractButton, QComboBox, QLineEdit, QSpinBox):
        for w in page.findChildren(cls):
            if not w.isVisible():
                continue
            if isinstance(w, QLineEdit) and isinstance(w.parent(), (QAbstractSpinBox, QComboBox)):
                continue  # 数字框、下拉框内部的编辑框，说明在外层控件上
            if not w.toolTip().strip():
                missing.append(f"{type(w).__name__}:{getattr(w, 'text', lambda: '')()}")
    return missing


def empty_hint(view) -> str:
    return view._empty_hint.label.text()


# ---------- 考试页 ----------
def test_exam_page_all_controls_have_tooltips(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    e = create_exam(s.output_root, ExamInfo("期中考试", "英语"), TEMPLATE_DIR)
    from scan_answer.pipeline.session import BatchSession
    BatchSession.create(e.directory, e.template_dir, batch_id="b1")
    page = show(qtbot, ExamPage(s))
    assert page.exams and page.batches
    assert missing_tooltips(page) == []
    # 表头说明各列含义
    for table in (page.exam_table, page.batch_table):
        for c in range(table.columnCount()):
            assert table.horizontalHeaderItem(c).toolTip()
    # 批次标题、模板下拉旁有“?”
    concepts = {b.text_html for b in page.findChildren(HelpButton)}
    assert any("批次" in t for t in concepts) and any("所用模板" in t for t in concepts)


def test_exam_page_row_actions_exist_only_with_rows(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    page = show(qtbot, ExamPage(s))
    # 没有考试：没有考试行，也就没有行尾操作（空状态说明见 test_exam_page_empty_states_and_root_hint）
    for b in (page.scan_btn, page.open_dir_btn, page.cont_btn, page.res_btn, page.bdir_btn):
        assert b is None
    assert missing_tooltips(page) == []
    # 有考试但没有批次：考试行尾可扫描新批次；批次子表说明先点“扫描新批次”
    create_exam(s.output_root, ExamInfo("期中考试"), TEMPLATE_DIR)
    page.refresh()
    assert page.scan_btn.isEnabled() and "新建一个批次" in page.scan_btn.toolTip()
    assert page.cont_btn is None and "还没有批次" in empty_hint(page.batch_table)
    assert "扫描新批次" in empty_hint(page.batch_table)
    # 创建按钮：随表单状态说明还差什么
    assert "考试名称" in page.create_btn.toolTip()
    page.f_name.setText("期末考试")
    assert "新建考试" in page.create_btn.toolTip()


def test_exam_page_without_templates_explains(qtbot, app_home, tmp_path, monkeypatch):
    monkeypatch.setattr("scan_answer.ui.pages.exam_page.list_templates", lambda _dirs: [])
    s = make_settings(tmp_path)
    page = show(qtbot, ExamPage(s))
    assert page.f_template.count() == 0
    assert "没有可用的模板" in page.f_template.toolTip()
    assert "没有可用的模板" in page.create_btn.toolTip()


def test_exam_page_empty_states_and_root_hint(qtbot, app_home, tmp_path):
    s = make_settings(tmp_path)
    (tmp_path / "out").mkdir()
    page = show(qtbot, ExamPage(s))
    assert "还没有考试" in empty_hint(page.exam_table)
    assert "选中一场考试" in empty_hint(page.batch_table)
    assert not page.root_hint.isVisible()  # 自定义的输出目录：不提示
    create_exam(s.output_root, ExamInfo("期中考试"), TEMPLATE_DIR)
    page.refresh()
    assert "还没有批次" in empty_hint(page.batch_table)
    # 输出根目录不可用时：说明去设置页处理
    s.output_root = str(tmp_path / "nope")
    page.refresh()
    assert "设置" in empty_hint(page.exam_table)


def test_exam_page_default_root_hint(qtbot, app_home, tmp_path, monkeypatch):
    home = tmp_path / "user"
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    s = make_settings(tmp_path, output_root=str(default_output_root()))
    page = show(qtbot, ExamPage(s))
    assert page.root_hint.isVisible() and "设置" in page.root_hint.text()
    create_exam(s.output_root, ExamInfo("期中考试"), TEMPLATE_DIR)
    page.refresh()
    assert not page.root_hint.isVisible()


# ---------- 设置页 ----------
def test_settings_page_all_controls_have_tooltips(qtbot, app_home, tmp_path):
    page = show(qtbot, SettingsPage(make_settings(tmp_path)))
    for key in page.nav_items:  # 一次只显示一节：每节都查
        page.show_section(key)
        assert missing_tooltips(page) == [], key
    concepts = " ".join(b.text_html for b in page.findChildren(HelpButton))
    for word in ("去掉彩色印刷", "裁掉纸边", "同时识别几张", "转写", "目录命名规则"):
        assert word in concepts, word
    # 分段按钮的每个选项都有自己的说明
    assert "推荐" in page.dpi.buttons["300"].toolTip()
    assert "黑色印刷" in page.dropout.buttons["none"].toolTip()


def test_settings_page_test_connection_and_save_feedback(qtbot, app_home, no_dialogs, tmp_path):
    page = show(qtbot, SettingsPage(make_settings(tmp_path)))
    # 配置不完整时测试连接：直接在旁边给出失败原因，不弹窗
    page.base_url.setText("")
    page.test_connection()
    assert page.test_result.text() and page.test_btn.isEnabled()
    assert page.test_btn.toolTip()
    page._ping_done(True, "连接正常，模型 qwen-vl-max")
    assert "连接正常" in page.test_result.text()
    page.model.setText("other")  # 改了配置：旧的测试结果清掉
    assert page.test_result.text() == ""
    # 改了没保存：保存栏提示；保存后提示已保存
    page.operator.setText("王老师")
    assert "未保存" in page.save_status.text()
    assert page.save()
    assert "已保存" in page.save_status.text()
    assert no_dialogs == []
