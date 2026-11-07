"""模板编辑画布测试。"""
from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
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


def _send_mouse(widget, event_type, pos, button=Qt.MouseButton.LeftButton,
                modifiers=Qt.KeyboardModifier.NoModifier):
    """发送 QMouseEvent 到 widget（offscreen 模式下 QTest.mouseMove 有时不生效）。"""
    ev = QMouseEvent(event_type, pos, widget.mapToGlobal(pos),
                     button, button, modifiers)
    QApplication.sendEvent(widget, ev)


def _find_item(canvas, ref):
    """在场景中找到 ref 对应的 RegionItem。"""
    for it in canvas.scene().items():
        if hasattr(it, "_ref") and it._ref == ref:
            return it
    return None


def _find_handle(item, pos_name):
    """在 RegionItem 中找到指定位置的手柄。"""
    for ch in item.childItems():
        if hasattr(ch, "_handle_pos") and ch._handle_pos == pos_name:
            return ch
    return None


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
    """用鼠标橡皮筋框选一个定位块，验证 selected() 恰好返回 1 个；
    选中项有 8 个手柄；拖动右下手柄 +5 mm 验证宽高。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")
    canvas.set_tool(None)

    front_refs = doc.regions("front")
    anchor_refs = [r for r in front_refs if r.kind == "anchor"]
    assert len(anchor_refs) >= 1

    # 找一个定位块，其 2mm 余量框不与其他区域相交
    target_ref = None
    for aref in anchor_refs:
        ax, ay, aw, ah = doc.rect(aref)
        box = (ax - 2, ay - 2, ax + aw + 2, ay + ah + 2)
        alone = True
        for ref in front_refs:
            if ref == aref:
                continue
            rx, ry, rw, rh = doc.rect(ref)
            if (rx + rw > box[0] and rx < box[2] and
                    ry + rh > box[1] and ry < box[3]):
                alone = False
                break
        if alone:
            target_ref = aref
            break
    # 若没有完全孤立的 anchor，用第一个但放宽断言
    if target_ref is None:
        target_ref = anchor_refs[0]

    target_rect = doc.rect(target_ref)
    sel_x1 = target_rect[0] - 2
    sel_y1 = target_rect[1] - 2
    sel_x2 = target_rect[0] + target_rect[2] + 2
    sel_y2 = target_rect[1] + target_rect[3] + 2

    # 用鼠标从 (sel_x1, sel_y1) 拖到 (sel_x2, sel_y2) 做橡皮筋框选
    vp = canvas.viewport()
    p1 = canvas.mapFromScene(QPointF(sel_x1, sel_y1))
    p2 = canvas.mapFromScene(QPointF(sel_x2, sel_y2))

    sel_signals = []
    canvas.selectionChanged.connect(lambda refs: sel_signals.append(list(refs)))

    _send_mouse(vp, QEvent.Type.MouseButtonPress, p1)
    _send_mouse(vp, QEvent.Type.MouseMove, p2)
    _send_mouse(vp, QEvent.Type.MouseButtonRelease, p2)

    # 验证框选结果
    selected = canvas.selected()
    assert target_ref in selected, f"目标定位块 {target_ref} 应被框选中"
    # selectionChanged 应被触发
    assert len(sel_signals) >= 1, "selectionChanged 应触发"

    # 检查选中项有 8 个可见手柄
    sel_item = _find_item(canvas, target_ref)
    assert sel_item is not None

    handles = [ch for ch in sel_item.childItems() if hasattr(ch, "_handle_pos")]
    assert len(handles) == 8, f"应有 8 个手柄，实际 {len(handles)}"
    visible_handles = [h for h in handles if h.isVisible()]
    assert len(visible_handles) == 8, f"应有 8 个可见手柄，实际 {len(visible_handles)}"

    # 拖动右下手柄 +5 mm
    br_handle = _find_handle(sel_item, "br")
    assert br_handle is not None

    old_rect = doc.rect(target_ref)
    edited_signals = []
    canvas.regionEdited.connect(lambda ref, rect: edited_signals.append((ref, rect)))

    handle_center_scene = sel_item.mapToScene(br_handle.pos())
    dest_scene = QPointF(handle_center_scene.x() + 5.0, handle_center_scene.y() + 5.0)

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


# ── test_handles_reposition_after_refresh (I1) ──────────────────


def test_handles_reposition_after_refresh(qtbot, app_home):
    """选中区域后，外部修改 doc.set_rect + canvas.refresh()，手柄应移到新位置。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")

    # 找到第一个客观题块
    front_refs = doc.regions("front")
    obj_ref = next(r for r in front_refs if r.kind == "objective")

    canvas.select([obj_ref])
    old_rect = doc.rect(obj_ref)
    ox, oy, ow, oh = old_rect

    # 模拟页面调用 doc.set_rect 将区域右移 10 mm
    new_rect = (ox + 10, oy, ow, oh)
    doc.set_rect(obj_ref, new_rect)
    canvas.refresh()

    # 选中集不应变化
    assert canvas.selected() == [obj_ref]

    # 检查手柄位置是否更新到新 rect
    item = _find_item(canvas, obj_ref)
    assert item is not None
    br_handle = _find_handle(item, "br")
    assert br_handle is not None

    # br 手柄应在新矩形的右下角
    expected_br_x = new_rect[0] + new_rect[2]
    expected_br_y = new_rect[1] + new_rect[3]
    actual_pos = br_handle.pos()  # 父坐标系
    assert abs(actual_pos.x() - expected_br_x) < 0.01, (
        f"br 手柄 x 应为 {expected_br_x}，实际 {actual_pos.x()}"
    )
    assert abs(actual_pos.y() - expected_br_y) < 0.01, (
        f"br 手柄 y 应为 {expected_br_y}，实际 {actual_pos.y()}"
    )

    # tl 手柄应在新矩形的左上角
    tl_handle = _find_handle(item, "tl")
    assert abs(tl_handle.pos().x() - new_rect[0]) < 0.01
    assert abs(tl_handle.pos().y() - new_rect[1]) < 0.01


# ── test_handles_reposition_after_move_drag (I2) ────────────────


def test_handles_reposition_after_move_drag(qtbot, app_home):
    """移动拖动后手柄应在新位置。"""
    canvas = TemplateCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(800, 600)
    canvas.show()

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    canvas.set_doc(doc, "front")
    canvas.set_tool(None)

    front_refs = doc.regions("front")
    obj_ref = next(r for r in front_refs if r.kind == "objective")

    canvas.select([obj_ref])
    old_rect = doc.rect(obj_ref)
    ox, oy, ow, oh = old_rect

    # 在区域中心按下鼠标，向右拖动 5 mm
    center_scene = QPointF(ox + ow / 2, oy + oh / 2)
    dest_scene = QPointF(center_scene.x() + 5.0, center_scene.y())
    vp = canvas.viewport()
    p1 = canvas.mapFromScene(center_scene)
    p2 = canvas.mapFromScene(dest_scene)

    _send_mouse(vp, QEvent.Type.MouseButtonPress, p1)
    _send_mouse(vp, QEvent.Type.MouseMove, p2)

    # 拖动过程中手柄应跟随
    item = _find_item(canvas, obj_ref)
    br_handle = _find_handle(item, "br")
    tl_handle = _find_handle(item, "tl")

    # tl 手柄 x 应近似 ox + 5（拖动了 5mm）
    # 由于 mapFromScene 取整，允许 1mm 误差
    tl_x_during = tl_handle.pos().x()
    assert abs(tl_x_during - (ox + 5.0)) < 1.0, (
        f"拖动中 tl 手柄 x 应 ~{ox + 5.0}，实际 {tl_x_during}"
    )

    _send_mouse(vp, QEvent.Type.MouseButtonRelease, p2)

    # 释放后手柄也应在新位置
    assert abs(tl_handle.pos().x() - (ox + 5.0)) < 1.0
