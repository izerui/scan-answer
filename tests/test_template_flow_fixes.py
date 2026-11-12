"""模板制作流程的修正：属性面板、吸附、撤销合并、离开确认、校验文案、目录管理、新建预览、底图发布等。"""
from __future__ import annotations

import copy
import shutil
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from scan_answer.app.settings import bundled_templates_dir
from scan_answer.template.doc import COPY_SUFFIX, RegionRef, TemplateDoc, UndoStack

B199 = bundled_templates_dir() / "guanzong199"


def _page(qtbot, dirs=None):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.template_page import TemplatePage
    page = TemplatePage(AppSettings(template_dirs=dirs or [str(bundled_templates_dir())]))
    qtbot.addWidget(page)
    page.resize(1400, 860)
    page.show()
    return page


def _panel(qtbot, doc, ref):
    from scan_answer.ui.editor.props import PropsPanel
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.show()
    panel.set_target(doc, ref)
    return panel


def _user_copy(tmp_path) -> Path:
    d = tmp_path / "tpl"
    shutil.copytree(B199, d)
    return d


# ── 1. 分段按钮与题型切换 ──────────────────────────────────────

def test_segmented_does_not_emit_for_current_value(qtbot):
    from scan_answer.ui.widgets.common import Segmented
    seg = Segmented([("a", "A"), ("b", "B")], "a")
    qtbot.addWidget(seg)
    got = []
    seg.changed.connect(got.append)
    seg.buttons["a"].click()
    assert got == []
    seg.buttons["b"].click()
    seg.buttons["b"].click()
    assert got == ["b"] and seg.value() == "b"
    seg.set_value("a")
    seg.buttons["a"].click()
    assert got == ["b"]


def test_qtype_switch_keeps_options_and_grid(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    ref = RegionRef("objective", 0)
    page.select_refs([ref])
    blk = page.doc.template.objective[0]
    labels, cells = list(blk.grid.labels), copy.deepcopy(blk.grid.cells)
    seg = page.props._seg_qtype
    seg.buttons[blk.qtype].click()  # 再点当前题型：不改任何东西
    assert not page.doc.dirty
    other = "multiple" if blk.qtype == "single" else "single"
    page.props._seg_qtype.buttons[other].click()
    blk = page.doc.template.objective[0]
    assert blk.qtype == other and blk.grid.labels == labels and blk.grid.cells == cells
    # 切到判断题换成 T/F；再切回单选换回同样个数的字母
    page.props._seg_qtype.buttons["judge"].click()
    assert page.doc.template.objective[0].grid.labels == ["T", "F"]
    page.props._seg_qtype.buttons["single"].click()
    assert page.doc.template.objective[0].grid.labels == ["A", "B"]


def test_n_options_keeps_custom_labels(qtbot, app_home):
    doc = TemplateDoc.load(B199)
    ref = RegionRef("objective", 0)
    doc.set_params(ref, {"labels": ["W", "X", "Y", "Z"], "n_options": 4})
    panel = _panel(qtbot, doc, ref)
    got = []
    panel.paramsChanged.connect(lambda _r, p: got.append(p))
    panel._on_n_options_changed(5)
    assert got[-1]["labels"] == ["W", "X", "Y", "Z", "A"]
    panel._on_n_options_changed(3)
    assert got[-1]["labels"] == ["W", "X", "Y"]


# ── 10. 判断题选项数固定 2 ──────────────────────────────────────

def test_judge_option_count_is_fixed(qtbot, app_home):
    from PySide6.QtWidgets import QSpinBox
    doc = TemplateDoc.load(B199)
    ref = RegionRef("objective", 0)
    doc.set_params(ref, {"qtype": "judge", "labels": ["T", "F"], "n_options": 2})
    panel = _panel(qtbot, doc, ref)
    spins = [s for s in panel._params_container.findChildren(QSpinBox) if s.maximum() == s.minimum() == 2]
    assert spins and not spins[0].isEnabled()
    with pytest.raises(ValueError, match="判断题只能有 2 个选项"):
        doc.set_params(ref, {"labels": ["T", "F", "X"], "n_options": 3})
    assert doc.template.objective[0].grid.labels == ["T", "F"]


# ── 2. 吸附后刷新位置数值框 ────────────────────────────────────

def test_snap_refreshes_position_spins(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    ref = RegionRef("objective", 0)
    x, y, w, h = page.doc.rect(ref)
    page.doc.set_rect(ref, (x + 0.8, y + 0.6, w, h))
    page.select_refs([ref])
    page._on_snap_requested(ref)
    snapped = page.doc.rect(ref)
    assert abs(snapped[0] - x) < 0.5, page.msg_label.text()
    assert abs(page.props._spin_x.value() - snapped[0]) < 0.06
    assert abs(page.props._spin_w.value() - snapped[2]) < 0.06
    assert page.props._snap_label.text()  # 吸附结果提示保留
    # 之后只改 X：宽高保持文档原值，不把网格缩放回吸附前
    page.props._spin_x.setValue(round(snapped[0], 1) + 1.0)
    assert page.doc.rect(ref)[2:] == pytest.approx(snapped[2:], abs=1e-6)


# ── 9. 只改一个分量时其余分量不取整 ──────────────────────────────

def test_rect_spin_replaces_only_changed_component(qtbot, app_home):
    doc = TemplateDoc.load(B199)
    ref = RegionRef("objective", 0)
    panel = _panel(qtbot, doc, ref)
    old = doc.rect(ref)
    got = []
    panel.rectChanged.connect(lambda _r, rect: got.append(rect))
    panel._spin_x.setValue(50.0)
    assert got[-1] == (50.0, old[1], old[2], old[3])
    panel._spin_h.setValue(60.0)
    assert got[-1] == (old[0], old[1], old[2], 60.0)


# ── 3. 未提交的输入在保存 / 离开前提交 ─────────────────────────────

def test_save_commits_pending_spin_and_line_edit(qtbot, app_home, no_dialogs, tmp_path):
    from PySide6.QtWidgets import QLineEdit
    page = _page(qtbot)
    page.open_editor(_user_copy(tmp_path))
    ref = RegionRef("field", 0)
    page.select_refs([ref])
    page.props._spin_x.lineEdit().setText("123.4")  # 只输入，没按回车也没离开焦点
    assert page.doc.rect(ref)[0] != 123.4
    assert page.save() is True
    assert page.doc.rect(ref)[0] == 123.4
    assert TemplateDoc.load(page.doc.directory).rect(ref)[0] == 123.4

    sub = RegionRef("subjective", 0)
    page.select_refs([sub])
    edit = next(e for e in page.props._params_container.findChildren(QLineEdit)
                if e.text() == page.doc.template.subjective[0].no)
    edit.setText("98")
    edit.setModified(True)
    no_dialogs.question_answer = QMessageBox.StandardButton.Cancel
    assert page.can_leave() is False  # 提交后才判断是否有修改
    assert page.doc.template.subjective[0].no == "98" and page.doc.dirty


# ── 8. 离开确认：保存 / 放弃修改 / 取消 ──────────────────────────

def test_can_leave_offers_save_discard_cancel(qtbot, app_home, tmp_path, monkeypatch):
    page = _page(qtbot)
    asked = []
    answer = {"v": QMessageBox.StandardButton.Cancel}

    def question(_parent, title, text, buttons=None, default=None):
        asked.append((title, buttons, default))
        return answer["v"]
    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    d = _user_copy(tmp_path)
    page.open_editor(d)
    version = page.doc.template.version
    ref = RegionRef("field", 0)
    page.select_refs([ref])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    b = QMessageBox.StandardButton
    assert page.can_leave() is False and page.doc is not None
    _title, buttons, default = asked[-1]
    assert buttons == b.Save | b.Discard | b.Cancel and default == b.Save
    # 保存失败（校验不过）时不离开
    anchors = [r for r in page.doc.regions("front") if r.kind == "anchor"]
    page._remove(anchors[:2])
    answer["v"] = b.Save
    assert page.can_leave() is False and page.doc is not None
    page.undo()
    assert page.can_leave() is True and page.doc is None
    assert TemplateDoc.load(d).template.version == version + 1  # 已保存


def test_can_leave_discard(qtbot, app_home, no_dialogs, tmp_path):
    page = _page(qtbot)
    page.open_editor(_user_copy(tmp_path))
    page.select_refs([RegionRef("field", 0)])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    no_dialogs.question_answer = QMessageBox.StandardButton.Discard
    assert page.can_leave() is True and page.doc is None


# ── 4. 自动识别同步头未检测到时不改文档 ──────────────────────────

def test_auto_sync_none_keeps_existing(monkeypatch):
    from scan_answer.template import doc as module
    doc = TemplateDoc.load(B199)
    before = doc.snapshot()
    monkeypatch.setattr(module, "auto_sync", lambda *a, **k: [])
    msg = doc.auto_align("front", "sync")
    assert "未检测到同步头" in msg and "保留" in msg
    assert doc.snapshot() == before and not doc.dirty


# ── 5. 再画考号 / 缺考标记提示已替换 ─────────────────────────────

def test_redraw_exam_no_reports_replacement(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    page._on_region_drawn("exam_no", (20.0, 20.0, 60.0, 40.0))
    assert "已替换原有的考号" in page.msg_label.text() and "可撤销" in page.msg_label.text()
    page._on_region_drawn("ignore", (20.0, 20.0, 10.0, 10.0))
    assert page.msg_label.text().startswith("已添加忽略区")


# ── 6. 选中项里没有网格时不吸附整面 ─────────────────────────────

def test_auto_snap_with_non_grid_selection(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    sub = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    page.select_refs([sub])
    before = page.doc.snapshot()
    page.auto_snap()
    assert "选中的区域中没有可吸附的网格" in page.msg_label.text()
    assert page.doc.snapshot() == before


# ── 7. 主观题追加目标不再“粘住” ──────────────────────────────────

def test_subjective_target_resets_after_deselect(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    sub = next(r for r in page.doc.regions("front") if r.kind == "subjective")
    key = page.doc.template.subjective[sub.index].key
    page.select_refs([sub])
    assert page.subjective_target.currentData() == key
    page.set_tool("subjective")
    assert f"将接到第 {key} 题" in page.hint_label.text()
    page.set_tool(None)
    page.select_refs([])
    assert page.subjective_target.currentData() is None
    page.set_tool("subjective")
    assert "接到" not in page.hint_label.text()
    page.set_tool(None)
    # 手动在下拉框选的目标：取消选中后保留；切换正背面也保留
    index = page.subjective_target.findData(key)
    page.subjective_target.setCurrentIndex(index)
    page.subjective_target.activated.emit(index)
    page.select_refs([RegionRef("field", 0)])
    page.select_refs([])
    assert page.subjective_target.currentData() == key
    page.select_refs([sub])
    page.set_face("back")
    assert page.subjective_target.currentData() == key


# ── 14. 方向键连续微调合并成一步撤销 ─────────────────────────────

def test_nudges_merge_within_window(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    ref = RegionRef("field", 0)
    r0 = page.doc.rect(ref)
    page.select_refs([ref])
    for _ in range(3):
        QTest.keyClick(page.canvas, Qt.Key.Key_Right)
        qtbot.wait(20)  # 事件循环转过几轮（自动重复的按键也是这样）
    assert page.doc.rect(ref)[0] == pytest.approx(r0[0] + 0.3)
    page.undo()
    assert page.doc.rect(ref) == r0
    assert not page.undo_stack.can_undo()
    # 超过时间窗口的微调各自是一步
    page.NUDGE_MERGE_SECONDS = 0.01
    page.select_refs([ref])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    qtbot.wait(50)
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    page.undo()
    assert page.doc.rect(ref)[0] == pytest.approx(r0[0] + 0.1)


# ── 12 / 13. 撤销回到保存点不脏；无修改保存不升版本 ────────────────

def test_undo_to_saved_point_is_clean_and_noop_save(qtbot, app_home, no_dialogs, tmp_path):
    page = _page(qtbot)
    page.open_editor(_user_copy(tmp_path))
    version = page.doc.template.version
    assert page.save() is True and "没有需要保存的修改" in page.msg_label.text()
    assert page.doc.template.version == version
    page.select_refs([RegionRef("field", 0)])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    assert page.doc.dirty and "●" in page.title_label.text()
    page.undo()
    assert not page.doc.dirty and "●" not in page.title_label.text()
    page.redo()
    assert page.save() is True and page.doc.template.version == version + 1


def test_rotation_undo_back_to_saved_angle_is_clean(app_home):
    doc = TemplateDoc.load(B199)
    stack = UndoStack(doc)
    stack.push("rotate")
    doc.rotate_base("front", 1.0)
    assert doc.dirty
    stack.undo()
    assert not doc.dirty


# ── 15. 状态行定位点文案 ───────────────────────────────────────

def test_anchor_status_text(qtbot, app_home):
    page = _page(qtbot)
    page.open_editor(B199)
    assert page.align_label.text().startswith("正面定位块 4 个") and "/ 4" not in page.align_label.text()
    anchors = [r for r in page.doc.regions("front") if r.kind == "anchor"]
    page._remove(anchors[:1])
    assert "定位块 3 个（3 个按仿射定位，4 个更稳）" in page.align_label.text()


# ── 16 / 17. 校验文案 ─────────────────────────────────────────

def test_validate_messages_are_merged_and_one_based():
    from scan_answer.template.io import load_template
    from scan_answer.template.validate import validate_template
    t, _ = load_template(B199)
    blk = t.objective[0]
    blk.grid.cells = [[(x + 1000, y, w, h) for x, y, w, h in row] for row in blk.grid.cells]
    t.faces["front"].align.anchors[0] = (-5.0, 1.0, 3.0, 3.0)
    t.faces["front"].align.anchors = t.faces["front"].align.anchors[:2]
    msgs = validate_template(t)
    grid_msgs = [m for m in msgs if "客观题块" in m]
    assert len(grid_msgs) == 1 and "超出纸面" in grid_msgs[0], msgs
    assert f"{len(blk.grid.cells) * len(blk.grid.labels)} 个格" in grid_msgs[0]
    assert any("定位块1：超出纸面" in m for m in msgs), msgs
    assert not any("anchors" in m or "frames" in m for m in msgs), msgs
    assert any("定位块方式至少 3 个定位块" in m for m in msgs)


# ── 18. 改名；另存内置模板时名称加后缀 ────────────────────────────

def test_rename_and_copy_suffix(qtbot, app_home, no_dialogs):
    from scan_answer.template.io import load_template
    page = _page(qtbot)
    page.open_editor(B199)
    page.select_refs([RegionRef("field", 0)])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    assert page.save() is True
    assert page.doc.template.name == "管综199" + COPY_SUFFIX
    saved, _ = load_template(page.doc.directory)
    assert saved.name == "管综199" + COPY_SUFFIX
    assert page.rename_template("  新名字 ") is True
    assert page.doc.template.name == "新名字" and page.title_label.text().startswith("新名字")
    assert page.rename_template(" ") is False and page.doc.template.name == "新名字"
    page.undo()
    assert page.doc.template.name == "管综199" + COPY_SUFFIX
    page.redo()
    assert page.save() is True
    assert load_template(page.doc.directory)[0].name == "新名字"


def test_renamed_bundled_copy_keeps_chosen_name(qtbot, app_home, no_dialogs):
    page = _page(qtbot)
    page.open_editor(B199)
    page.rename_template("我的卡")
    assert page.save() is True and page.doc.template.name == "我的卡"


# ── 19. 模板目录：添加空目录提示，移除目录 ─────────────────────────

def test_add_empty_dir_and_remove_dir(qtbot, app_home, no_dialogs, tmp_path):
    from scan_answer.app.settings import load_settings
    page = _page(qtbot)
    empty = tmp_path / "empty"
    empty.mkdir()
    page.add_dir(str(empty))
    assert no_dialogs and "没有发现模板" in no_dialogs[-1][1]
    assert str(empty) in page.removable_dirs()
    assert str(bundled_templates_dir()) not in page.removable_dirs()
    n = len(no_dialogs)
    full = _user_copy(tmp_path)
    page.add_dir(str(full.parent))
    assert len(no_dialogs) == n  # 有模板时不提示
    assert page.remove_dir(str(empty)) is True
    assert str(empty) not in load_settings().template_dirs and empty.exists()
    assert page.remove_dir(str(bundled_templates_dir())) is False


# ── 20. 新建对话框的页数提示与定位方式预览 ─────────────────────────

def _pdf(path: Path, n: int) -> Path:
    from PIL import Image
    pages = [Image.new("L", (600, 800), 255) for _ in range(n)]
    pages[0].save(path, save_all=True, append_images=pages[1:], resolution=72)
    return path


def test_import_files_page_notes(tmp_path):
    from scan_answer.ui.editor.new_dialog import import_files
    three, two, one = _pdf(tmp_path / "3.pdf", 3), _pdf(tmp_path / "2.pdf", 2), _pdf(tmp_path / "1.pdf", 1)
    _faces, notes = import_files(str(three), str(one))
    assert notes == ["正面文件有 3 页，已另选背面文件，正面只用第 1 页"]
    _faces, notes = import_files(str(two), str(one))
    assert notes == ["正面文件有 2 页，已另选背面文件，正面只用第 1 页"]
    _faces, notes = import_files(str(two))
    assert notes == ["PDF 第 2 页作为背面"]
    _faces, notes = import_files(str(three))
    assert notes == ["PDF 有 3 页，只用前 2 页", "PDF 第 2 页作为背面"]


def test_preview_method_matches_new_template(tpl_bases):
    from scan_answer.template.auto import ImportedBase
    from scan_answer.ui.editor.new_dialog import describe
    t, bases = tpl_bases
    # import_base 的判据与实际建模不同：这里直接给错的 method，describe 应按实际检测结果显示
    faces = {"front": ImportedBase(bases["front"], t.paper, 0.0, "frames")}
    from scan_answer.template.doc import initial_alignment
    align = initial_alignment(bases["front"], 300, t.paper)
    doc = TemplateDoc.new("x", faces)
    assert doc.template.faces["front"].align.method == align.method == "anchors"
    from scan_answer.ui.editor import new_dialog
    blank = np.full((800, 600), 255, np.uint8)
    ib = new_dialog.import_base(blank, 72)
    ib_method = initial_alignment(ib.image, 300, ib.paper)
    assert not (ib_method.anchors or ib_method.frames)
    assert "定位方式 定位块" in describe({"front": ImportedBase(bases["front"], t.paper, 0.0, "anchors")})[0]


def test_import_files_uses_real_detection(tmp_path, monkeypatch):
    from scan_answer.template.model import AlignSpec
    from scan_answer.ui.editor import new_dialog
    pdf = _pdf(tmp_path / "1.pdf", 1)
    monkeypatch.setattr(new_dialog, "initial_alignment",
                        lambda *a, **k: AlignSpec(method="frames", frames=[(1.0, 1.0, 50.0, 50.0)]))
    faces, _ = new_dialog.import_files(str(pdf))
    assert faces["front"].method == "frames"
    monkeypatch.setattr(new_dialog, "initial_alignment", lambda *a, **k: AlignSpec(method="anchors"))
    faces, _ = new_dialog.import_files(str(pdf))
    assert faces["front"].method == "none"


@pytest.mark.parametrize("flt", ["new_dialog", "documents"])
def test_file_filters_use_ascii_parentheses(flt):
    """Qt 只从半角括号里取通配符；全角括号会把 *.pdf 粘到标签上，对话框里选不了 PDF。"""
    import re
    from scan_answer.source.documents import DOCUMENT_FILTER
    from scan_answer.ui.editor.new_dialog import FILE_FILTER
    text = {"new_dialog": FILE_FILTER, "documents": DOCUMENT_FILTER}[flt]
    m = re.fullmatch(r"(.*)\(([a-zA-Z0-9_.*? ]*)\)", text)
    assert m and "*.pdf" in m.group(2).split()


# ── 21. 重做快捷键 ────────────────────────────────────────────

def test_redo_shortcuts_include_ctrl_shift_z(qtbot, app_home):
    page = _page(qtbot)
    redo = page.shortcut_keys["redo"]
    assert "Ctrl+Shift+Z" in redo and len(redo) == len(set(redo))
    assert not set(redo) & set(page.shortcut_keys["undo"])


# ── 22. 底图旋转限制 ──────────────────────────────────────────

def test_rotation_limited_to_small_tilt(qtbot, app_home):
    doc = TemplateDoc.load(B199)
    doc.rotate_base("front", 10.0)
    with pytest.raises(ValueError, match="小角度"):
        doc.rotate_base("front", 0.5)
    assert doc.base_angle("front") == 10.0
    doc.rotate_base("front", -10.0)
    doc.rotate_base("front", 180.0)
    doc.rotate_base("front", 1.0)
    assert doc.base_angle("front") == pytest.approx(-179.0)
    page = _page(qtbot)
    page.open_editor(B199)
    for _ in range(10):
        page.rotate_base(1.0)
    page.rotate_base(1.0)
    assert "小角度" in page.msg_label.text() and page.doc.base_angle("front") == pytest.approx(10.0)


# ── 23. 不支持硬链接的文件系统 ─────────────────────────────────────

def test_save_resource_without_hardlinks(tmp_path, monkeypatch):
    import os

    from scan_answer.template import io
    def no_link(*_a, **_k):
        raise PermissionError(1, "Operation not permitted")
    monkeypatch.setattr(io.os, "link", no_link)
    image = np.full((40, 30), 200, np.uint8)
    ref = io._save_resource(tmp_path, "front", image)
    assert (tmp_path / ref).exists()
    assert io._save_resource(tmp_path, "front", image) == ref  # 再次发布同一内容
    assert [p.name for p in tmp_path.iterdir()] == [ref]  # 没有残留临时文件
    # 写入失败时清理半成品
    def bad_fsync(_fd):
        raise OSError("io error")
    monkeypatch.setattr(io.os, "fsync", bad_fsync)
    with pytest.raises(OSError):
        io._save_resource(tmp_path, "back", image)
    assert [p.name for p in tmp_path.iterdir()] == [ref]
    assert os.path.exists(tmp_path / ref)
