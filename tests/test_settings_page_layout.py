"""设置页 A 布局：左侧分节目录 + 顶部常驻保存条。原有每个控件都要可达、可用。"""
import pytest
from PySide6.QtWidgets import QLineEdit, QMessageBox

from scan_answer.app.settings import AppSettings, bundled_templates_dir
from scan_answer.ui.pages.settings_page import FIELD_LABELS, SECTION_FIELDS, SettingsPage
from scan_answer.ui.widgets.common import RegionLegend
from scan_answer.ui.widgets.guide import HelpButton


def make_page(qtbot, tmp_path, **kw) -> SettingsPage:
    kw.setdefault("output_root", str(tmp_path / "out"))
    page = SettingsPage(AppSettings(template_dirs=[str(bundled_templates_dir())], workers=2, **kw))
    qtbot.addWidget(page)
    page.resize(1280, 800)
    page.show()
    qtbot.waitExposed(page)
    return page


def control(page, key):
    return {"dpi": page.dpi, "color": page.color, "duplex": page.duplex, "double_feed": page.double_feed,
            "dropout": page.dropout, "crop": page.crop, "workers": page.workers, "ocr_on": page.ocr_on,
            "base_url": page.base_url, "api_key": page.api_key, "model": page.model,
            "concurrency": page.concurrency, "output_root": page.output_root, "operator": page.operator,
            "exam_pattern": page.exam_pattern, "student_pattern": page.student_pattern}[key]


def test_sections_switch_and_default_is_output(qtbot, tmp_path):
    page = make_page(qtbot, tmp_path)
    assert list(page.nav_items) == ["out", "scan", "ocr", "legend"]
    assert [page.nav_items[k].title.text() for k in page.nav_items] == ["输出", "扫描默认参数", "大模型转写", "区域色码"]
    assert [page.nav_items[k].tag.text() for k in page.nav_items] == ["必设", "用扫描仪时", "可选", "参考"]
    assert page.current_section() == "out" and page.nav_items["out"].isChecked()
    assert page.output_root.isVisible() and not page.dpi.isVisible()
    for key in ("scan", "ocr", "legend", "out"):
        page.nav_items[key].click()
        assert page.current_section() == key
        assert page.nav_items[key].isChecked()
        assert sum(item.isChecked() for item in page.nav_items.values()) == 1
    page.nav_items["legend"].click()
    legend = page.section_pages["legend"].findChildren(RegionLegend)
    assert legend and legend[0].isVisible()


def test_every_field_is_reachable_in_its_section(qtbot, tmp_path):
    page = make_page(qtbot, tmp_path)
    assert set(FIELD_LABELS) == set(page._form_values())
    assert sorted(k for keys in SECTION_FIELDS.values() for k in keys) == sorted(FIELD_LABELS)
    for section, keys in SECTION_FIELDS.items():
        page.show_section(section)
        for key in keys:
            assert control(page, key).isVisible(), key
            assert page.field_rows[key].isAncestorOf(control(page, key)), key
    # 原来散在表单里的按钮、“?”、示例
    page.show_section("out")
    assert page.browse_btn.isVisible() and page.naming_sample.isVisible()
    page.show_section("ocr")
    assert page.show_key_btn.isVisible() and page.test_btn.isVisible() and page.test_result is not None
    concepts = {b.text_html for b in page.section_pages["ocr"].findChildren(HelpButton)}
    assert any("转写" in c for c in concepts)
    page.show_section("scan")
    concepts = " ".join(b.text_html for b in page.section_pages["scan"].findChildren(HelpButton))
    assert all(w in concepts for w in ("颜色滤除", "裁边", "识别进程数"))
    page.show_section("out")
    concepts = " ".join(b.text_html for b in page.section_pages["out"].findChildren(HelpButton))
    assert "目录命名规则" in concepts


def test_section_summaries_follow_form_values(qtbot, tmp_path):
    page = make_page(qtbot, tmp_path)
    nav = page.nav_items
    assert str(tmp_path / "out") in nav["out"].summary.text()
    assert "已修改" not in nav["out"].summary.text()
    assert nav["scan"].summary.text() == "300 DPI · 灰度 · 双面 · 识别进程 2"
    assert nav["ocr"].summary.text() == "未启用"
    page.dpi.buttons["600"].click()
    page.color.buttons["color"].click()
    page.duplex.buttons["0"].click()
    assert nav["scan"].summary.text() == "600 DPI · 彩色 · 单面 · 识别进程 2"
    page.output_root.setText(str(tmp_path / "new"))
    assert nav["out"].summary.text() == f"{tmp_path / 'new'}（已修改）"
    page.model.setText("qwen-vl-max")
    assert nav["ocr"].summary.text() == "未启用 · 已保留 qwen-vl-max 的配置"
    page.ocr_on.setChecked(True)
    assert nav["ocr"].summary.text() == "已启用 · qwen-vl-max · 并发 4"
    assert nav["legend"].summary.text()


def test_unsaved_changes_listed_dotted_and_show_original(qtbot, app_home, no_dialogs, tmp_path):
    page = make_page(qtbot, tmp_path)
    old_root = str(tmp_path / "out")
    assert page.save_status.text() == ""
    assert not any(item.dot.isVisible() for item in page.nav_items.values())
    page.output_root.setText(str(tmp_path / "new"))
    page.workers.setValue(3)
    assert page.save_status.text() == "2 项未保存：输出根目录、识别进程数"
    assert "保存设置" in page.save_status.toolTip()
    assert page.nav_items["out"].dot.isVisible() and page.nav_items["scan"].dot.isVisible()
    assert not page.nav_items["ocr"].dot.isVisible() and not page.nav_items["legend"].dot.isVisible()
    assert page.field_rows["output_root"].changed and page.field_rows["workers"].changed
    assert not page.field_rows["operator"].changed
    assert page.field_rows["output_root"].was.text() == f"原为 {old_root}"
    assert page.field_rows["output_root"].was.isVisible()
    assert not page.field_rows["operator"].was.isVisible()
    page.show_section("scan")
    assert page.field_rows["workers"].was.text() == "原为 2" and page.field_rows["workers"].was.isVisible()
    # 分段、开关显示选项名；密钥不显示明文
    page.dpi.buttons["600"].click()
    page.double_feed.setChecked(False)
    assert page.field_rows["dpi"].was.text() == "原为 300"
    assert page.field_rows["double_feed"].was.text() == "原为 开"
    page.api_key.setText("sk-secret")
    assert "sk-secret" not in page.field_rows["api_key"].was.text()
    assert page.field_rows["api_key"].was.text() == "原为 空"
    assert "5 项未保存" in page.save_status.text() and "密钥" in page.save_status.toolTip()
    # 改回原值：不再算未保存
    page.workers.setValue(2)
    assert not page.field_rows["workers"].changed and not page.field_rows["workers"].was.isVisible()
    assert "识别进程数" not in page.save_status.toolTip()
    # 保存后全部清除
    assert page.save()
    assert "已保存" in page.save_status.text()
    assert not any(item.dot.isVisible() for item in page.nav_items.values())
    assert not any(row.changed for row in page.field_rows.values())
    assert not page.field_rows["output_root"].was.isVisible()
    assert page.nav_items["out"].summary.text() == str(tmp_path / "new")


def test_leaving_with_unsaved_changes_still_asks(qtbot, app_home, no_dialogs, tmp_path):
    page = make_page(qtbot, tmp_path)
    assert page.can_leave() and not no_dialogs
    page.operator.setText("王老师")
    no_dialogs.question_answer = QMessageBox.StandardButton.Cancel
    assert not page.can_leave() and "未保存" in no_dialogs[-1][1]
    no_dialogs.question_answer = QMessageBox.StandardButton.Discard
    assert page.can_leave()
    assert page.operator.text() == "" and page.save_status.text() == ""
    assert not page.nav_items["out"].dot.isVisible()


def test_ocr_labels_are_chinese_with_english_secondary(qtbot, tmp_path):
    page = make_page(qtbot, tmp_path)
    for key, main, sub in (("base_url", "接口地址", "base_url"), ("api_key", "密钥", "api_key")):
        row = page.field_rows[key]
        assert row.label.text() == main and row.sub_label.text() == sub
    assert FIELD_LABELS["base_url"] == "接口地址" and FIELD_LABELS["api_key"] == "密钥"


def test_show_key_browse_test_connection_and_naming_sample_work(qtbot, tmp_path, monkeypatch):
    page = make_page(qtbot, tmp_path)
    page.show_section("ocr")
    assert page.api_key.echoMode() == QLineEdit.EchoMode.Password
    page.show_key_btn.click()
    assert page.api_key.echoMode() == QLineEdit.EchoMode.Normal
    page.show_key_btn.click()
    assert page.api_key.echoMode() == QLineEdit.EchoMode.Password
    page.base_url.setText("")
    page.test_btn.click()
    assert page.test_result.text() and page.test_btn.isEnabled()
    page.show_section("out")
    monkeypatch.setattr("scan_answer.ui.pages.settings_page.QFileDialog.getExistingDirectory",
                        lambda *a, **k: str(tmp_path / "picked"))
    page.browse_btn.click()
    assert page.output_root.text() == str(tmp_path / "picked")
    page.exam_pattern.setText("{name}")
    page.student_pattern.setText("{exam_no}")
    assert "/ batch_…/students/" in page.naming_sample.text()
    page.exam_pattern.setText("{oops}")
    assert "无法识别的占位符" in page.naming_sample.text()


@pytest.mark.parametrize("key", ["out", "scan", "ocr", "legend"])
def test_section_header_and_lead(qtbot, tmp_path, key):
    page = make_page(qtbot, tmp_path)
    page.show_section(key)
    assert page.section_pages[key].heading.text() == page.nav_items[key].title.text()
    assert page.section_pages[key].lead.text()
