"""模板底图手动旋转：从原始底图按累计角度旋转，可撤销，保存后生效。"""
import cv2
import numpy as np
import pytest

from scan_answer.engine.align import align_primary
from scan_answer.template.doc import TemplateDoc, UndoStack

from conftest import TEMPLATE_DIR


def _skewed_doc(deg):
    """把 199 模板正面底图转歪 deg 度，模拟自动摆正失败的空白卡。"""
    doc = TemplateDoc.load(TEMPLATE_DIR)
    img = doc.bases["front"]
    h, w = img.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    doc.bases["front"] = cv2.warpAffine(img, M, (w, h), borderValue=255)
    return TemplateDoc(doc.template, doc.bases, None)


def test_rotate_base_straightens_skewed_blank_card():
    doc = _skewed_doc(1.5)
    skew = align_primary(doc.bases["front"], doc.template.faces["front"].align, 300).skew_deg
    assert abs(skew + 1.5) < 0.2
    rev = doc.revision
    doc.rotate_base("front", -1.5)
    assert doc.base_angle("front") == pytest.approx(-1.5) and doc.revision == rev + 1 and doc.dirty
    res = align_primary(doc.bases["front"], doc.template.faces["front"].align, 300)
    assert res.ok and abs(res.skew_deg) < 0.15


def test_small_steps_do_not_accumulate_blur():
    doc = _skewed_doc(0.0)
    original = doc.bases["front"].copy()
    for _ in range(10):
        doc.rotate_base("front", 0.1)
    for _ in range(10):
        doc.rotate_base("front", -0.1)
    assert doc.base_angle("front") == pytest.approx(0.0, abs=1e-9)
    assert np.array_equal(doc.bases["front"], original)  # 回到 0° 就是原图，没有多次插值的损失


def test_rotate_180_is_exact_and_size_preserved():
    doc = _skewed_doc(0.0)
    original = doc.bases["front"].copy()
    doc.rotate_base("front", 180)
    assert doc.bases["front"].shape == original.shape
    assert np.array_equal(doc.bases["front"], cv2.rotate(original, cv2.ROTATE_180))


def test_rotation_is_undoable_with_other_edits():
    doc = _skewed_doc(0.0)
    original = doc.bases["front"].copy()
    stack = UndoStack(doc)
    stack.push("旋转底图")
    doc.rotate_base("front", 2.0)
    stack.push("添加字段")
    doc.add("field", "front", (100, 100, 20, 10))
    stack.undo()
    assert doc.base_angle("front") == pytest.approx(2.0)
    stack.undo()
    assert doc.base_angle("front") == 0 and np.array_equal(doc.bases["front"], original)
    stack.redo()
    assert doc.base_angle("front") == pytest.approx(2.0)
    assert not np.array_equal(doc.bases["front"], original)


def test_saved_template_uses_rotated_base(tmp_path):
    doc = _skewed_doc(0.0)
    doc.rotate_base("front", 180)
    target = doc.save(tmp_path / "rotated")
    reloaded = TemplateDoc.load(target)
    assert np.array_equal(reloaded.bases["front"], doc.bases["front"])
    assert reloaded.base_angle("front") == 0  # 保存后的底图成为新的原图


def test_unknown_face_is_rejected():
    doc = _skewed_doc(0.0)
    with pytest.raises(KeyError):
        doc.rotate_base("middle", 1.0)


@pytest.fixture
def page(qtbot, app_home, no_dialogs):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.template_page import TemplatePage

    widget = TemplatePage(AppSettings())
    qtbot.addWidget(widget)
    assert widget.open_editor(TEMPLATE_DIR)
    return widget


def _canvas_pixels(page):
    from PySide6.QtGui import QImage
    img = page.canvas._bg_item.pixmap().toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    return np.array(img.constBits(), dtype=np.uint8).reshape(img.height(), img.bytesPerLine())[:, :img.width()]


def test_editor_rotation_menu_updates_canvas_and_undo(page):
    original = page.doc.bases["front"].copy()
    actions = {a.text(): a for a in page.rotate_btn.menu().actions()}
    actions["旋转 180°"].trigger()
    assert page.doc.base_angle("front") == pytest.approx(-180)
    assert np.array_equal(_canvas_pixels(page), page.doc.bases["front"])
    assert "底图已旋转" in page.msg_label.text() and page.doc.dirty
    page.undo()
    assert page.doc.base_angle("front") == 0
    assert np.array_equal(_canvas_pixels(page), original)
    page.redo()
    actions["逆时针 1°"].trigger()
    actions["恢复为打开时的角度"].trigger()
    assert page.doc.base_angle("front") == 0 and np.array_equal(page.doc.bases["front"], original)
    page.set_face("back")
    actions["顺时针 0.1°"].trigger()
    assert page.doc.base_angle("back") == pytest.approx(-0.1) and page.doc.base_angle("front") == 0
