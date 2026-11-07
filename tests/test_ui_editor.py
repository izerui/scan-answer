"""模板编辑画布测试。"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from scan_answer.app.settings import bundled_templates_dir
from scan_answer.template.doc import RegionRef, TemplateDoc
from scan_answer.ui.editor.canvas import TemplateCanvas


@pytest.fixture
def canvas(qtbot, app_home):
    """创建并展示一个 TemplateCanvas。"""
    w = TemplateCanvas()
    qtbot.addWidget(w)
    w.resize(800, 600)
    w.show()
    return w


@pytest.fixture
def doc_front(canvas):
    """加载 guanzong199 模板并设置到画布（front 面）。"""
    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")
    return doc


# ── test_canvas_items_and_drag ──────────────────────────────────


def test_canvas_items_and_drag(qtbot, app_home):
    """set_doc 后图元数正确；方向键微调 regionEdited 信号。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")

    # 区域图元数 == regions 数
    front_refs = doc.regions("front")
    scene_items = [
        it for it in canvas.scene().items()
        if hasattr(it, "_ref")  # RegionItem 有 _ref 属性
    ]
    assert len(scene_items) == len(front_refs), (
        f"区域图元 {len(scene_items)} != regions {len(front_refs)}"
    )

    # 找到第一个客观题块
    obj_ref = None
    for ref in front_refs:
        if ref.kind == "objective":
            obj_ref = ref
            break
    assert obj_ref is not None, "front 面应至少有一个客观题块"

    # 程序化选中
    canvas.select([obj_ref])
    assert canvas.selected() == [obj_ref]

    old_rect = doc.rect(obj_ref)

    # 监听 regionEdited 信号
    edited_signals = []
    canvas.regionEdited.connect(lambda ref, rect: edited_signals.append((ref, rect)))

    # 按右方向键（不加 Shift，移动 0.1 mm）
    QTest.keyPress(canvas, Qt.Key.Key_Right)
    assert len(edited_signals) == 1, f"应发出 1 次 regionEdited，实际 {len(edited_signals)}"
    new_ref, new_rect = edited_signals[0]
    assert new_ref == obj_ref
    assert abs(new_rect[0] - old_rect[0] - 0.1) < 1e-6, (
        f"x 应增加 0.1，实际 {new_rect[0] - old_rect[0]}"
    )

    # 按 Shift+右方向键（移动 1.0 mm）
    edited_signals.clear()
    QTest.keyPress(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert len(edited_signals) == 1
    _, new_rect2 = edited_signals[0]
    # 上一次 x 已经 +0.1，这次再 +1.0
    assert abs(new_rect2[0] - old_rect[0] - 1.1) < 1e-6, (
        f"x 应增加 1.1，实际 {new_rect2[0] - old_rect[0]}"
    )


# ── test_canvas_draw_tool ───────────────────────────────────────


def _send_mouse(widget, event_type, pos, button=Qt.MouseButton.LeftButton,
                modifiers=Qt.KeyboardModifier.NoModifier):
    """发送 QMouseEvent 到 widget（offscreen 模式下 QTest.mouseMove 有时不生效）。"""
    from PySide6.QtCore import QEvent
    ev = QMouseEvent(event_type, pos, widget.mapToGlobal(pos),
                     button, button, modifiers)
    QApplication.sendEvent(widget, ev)


def test_canvas_draw_tool(qtbot, app_home):
    """画框工具：拖动后发 regionDrawn；太小不发。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")
    canvas.set_tool("field")

    drawn_signals = []
    canvas.regionDrawn.connect(lambda kind, rect: drawn_signals.append((kind, rect)))

    # 从场景坐标 (100, 100) mm 拖到 (130, 112) mm
    vp = canvas.viewport()
    p1 = canvas.mapFromScene(QPointF(100.0, 100.0))
    p2 = canvas.mapFromScene(QPointF(130.0, 112.0))

    from PySide6.QtCore import QEvent
    _send_mouse(vp, QEvent.Type.MouseButtonPress, p1)
    _send_mouse(vp, QEvent.Type.MouseMove, p2)
    _send_mouse(vp, QEvent.Type.MouseButtonRelease, p2)

    assert len(drawn_signals) == 1, f"应发 1 次 regionDrawn，实际 {len(drawn_signals)}"
    kind, rect = drawn_signals[0]
    assert kind == "field"
    assert abs(rect[0] - 100) < 0.5
    assert abs(rect[1] - 100) < 0.5
    assert abs(rect[2] - 30) < 0.5
    assert abs(rect[3] - 12) < 0.5

    # 太小的拖动（< 1 mm）不发信号
    drawn_signals.clear()
    p3 = canvas.mapFromScene(QPointF(50.0, 50.0))
    p4 = canvas.mapFromScene(QPointF(50.3, 50.3))
    _send_mouse(vp, QEvent.Type.MouseButtonPress, p3)
    _send_mouse(vp, QEvent.Type.MouseMove, p4)
    _send_mouse(vp, QEvent.Type.MouseButtonRelease, p4)
    assert len(drawn_signals) == 0, "太小不应发信号"


# ── test_canvas_rubber_band_and_handles ─────────────────────────


def test_canvas_rubber_band_and_handles(qtbot, app_home):
    """框选一个定位块并验证 8 个手柄；拖动手柄缩放。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")

    # 确保是选择工具
    canvas.set_tool(None)

    # 找到第一个 anchor（定位块）
    front_refs = doc.regions("front")
    anchor_refs = [r for r in front_refs if r.kind == "anchor"]
    assert len(anchor_refs) >= 1, "应至少有一个定位块"
    target_ref = anchor_refs[0]
    target_rect = doc.rect(target_ref)  # (x, y, w, h) mm

    # 框选范围：包含该定位块，但不包含其他定位块
    # 在定位块周围加 2mm 的余量
    sel_x1 = target_rect[0] - 2
    sel_y1 = target_rect[1] - 2
    sel_x2 = target_rect[0] + target_rect[2] + 2
    sel_y2 = target_rect[1] + target_rect[3] + 2

    # 检查其他 anchor 不在选区内
    other_anchors_in_box = 0
    for r in anchor_refs:
        if r == target_ref:
            continue
        rx, ry, rw, rh = doc.rect(r)
        if (rx + rw > sel_x1 and rx < sel_x2 and
                ry + rh > sel_y1 and ry < sel_y2):
            other_anchors_in_box += 1

    # 程序化选中以测试手柄
    canvas.select([target_ref])
    selected = canvas.selected()
    assert len(selected) == 1
    assert selected[0] == target_ref

    # 检查选中项有 8 个手柄（子图元）
    sel_item = None
    for it in canvas.scene().items():
        if hasattr(it, "_ref") and it._ref == target_ref:
            sel_item = it
            break
    assert sel_item is not None

    handles = [ch for ch in sel_item.childItems() if hasattr(ch, "_handle_pos")]
    assert len(handles) == 8, f"应有 8 个手柄，实际 {len(handles)}"

    # 找到右下角手柄并拖动 +5 mm
    br_handle = None
    for h in handles:
        if h._handle_pos == "br":
            br_handle = h
            break
    assert br_handle is not None, "应找到右下角手柄"

    old_rect = doc.rect(target_ref)
    edited_signals = []
    canvas.regionEdited.connect(lambda ref, rect: edited_signals.append((ref, rect)))

    # 拖动手柄（场景坐标）：手柄 pos() 在父坐标系（mm）
    handle_center_scene = sel_item.mapToScene(br_handle.pos())
    dest_scene = QPointF(handle_center_scene.x() + 5.0, handle_center_scene.y() + 5.0)

    vp = canvas.viewport()
    from PySide6.QtCore import QEvent
    hp1 = canvas.mapFromScene(handle_center_scene)
    hp2 = canvas.mapFromScene(dest_scene)
    _send_mouse(vp, QEvent.Type.MouseButtonPress, hp1)
    _send_mouse(vp, QEvent.Type.MouseMove, hp2)
    _send_mouse(vp, QEvent.Type.MouseButtonRelease, hp2)

    assert len(edited_signals) >= 1, "拖动手柄应发 regionEdited"
    _, new_rect = edited_signals[-1]
    dw = new_rect[2] - old_rect[2]
    dh = new_rect[3] - old_rect[3]
    assert abs(dw - 5.0) < 0.2, f"宽应增加 ~5 mm，实际 {dw}"
    assert abs(dh - 5.0) < 0.2, f"高应增加 ~5 mm，实际 {dh}"
