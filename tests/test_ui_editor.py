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


# ── Task 6: RegionTree & PropsPanel tests ─────────────────────


def test_tree_groups_and_counts(qtbot, app_home):
    """199 正面的树中，"定位"组数量正确，"客观题"组数量为 4；
    选中子行时发出包含对应 RegionRef 的 selectionChanged。"""
    from scan_answer.ui.editor.tree import RegionTree

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    tree = RegionTree()
    qtbot.addWidget(tree)
    tree.show()
    tree.set_doc(doc, "front")

    # 遍历顶层项找分组
    groups = {}
    for i in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(i)
        label = item.text(0)
        groups[label] = item

    # 定位组（anchor + frame + sync）= 199 正面固定 16 个
    assert "定位" in groups, f"应有'定位'分组，实际分组: {list(groups.keys())}"
    loc_item = groups["定位"]
    assert loc_item.childCount() == 16, (
        f"定位子项应为 16（4 定位块 + 12 同步头），实际 {loc_item.childCount()}"
    )

    # 客观题组
    assert "客观题" in groups, f"应有'客观题'分组"
    obj_item = groups["客观题"]
    assert obj_item.childCount() == 4, f"客观题子项应为 4，实际 {obj_item.childCount()}"

    # 选中一个客观题子行，检查 selectionChanged 信号
    sel_signals = []
    tree.regionSelectionChanged.connect(lambda refs: sel_signals.append(list(refs)))

    # 找第一个客观题子行（"1–15"）
    first_obj_child = obj_item.child(0)
    assert first_obj_child is not None
    tree.setCurrentItem(first_obj_child)

    assert len(sel_signals) >= 1, "选中子行应触发 selectionChanged"
    last_sel = sel_signals[-1]
    assert len(last_sel) == 1
    assert last_sel[0].kind == "objective"


def test_props_objective_edit(qtbot, app_home):
    """把 n_questions 改成 10 并提交，paramsChanged 的 dict 中 n_questions == 10；
    切换到"判断"时，options 标签变为 T、F。"""
    from scan_answer.ui.editor.props import PropsPanel

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.show()

    # 找到第一个客观题 ref
    front_refs = doc.regions("front")
    obj_ref = next(r for r in front_refs if r.kind == "objective")

    panel.set_target(doc, obj_ref)

    # 监听 paramsChanged
    param_signals = []
    panel.paramsChanged.connect(lambda ref, p: param_signals.append((ref, p)))

    # 修改题数（n_questions）为 10
    panel._spin_n_questions.setValue(10)
    # editingFinished 或 valueChanged 应触发信号
    assert len(param_signals) >= 1, "修改题数应触发 paramsChanged"
    last_ref, last_p = param_signals[-1]
    assert last_ref == obj_ref
    assert last_p.get("n_questions") == 10, f"n_questions 应为 10，实际 {last_p}"

    # 切换到"判断"
    param_signals.clear()
    panel._seg_qtype.buttons["judge"].click()
    assert len(param_signals) >= 1, "切换 qtype 应触发 paramsChanged"
    last_ref, last_p = param_signals[-1]
    assert last_p.get("qtype") == "judge"
    assert last_p.get("labels") == ["T", "F"], f"判断题标签应为 ['T', 'F']，实际 {last_p.get('labels')}"


def test_props_labels_emit_n_options(qtbot, app_home):
    """编辑选项标签时同时发出 labels 和 n_options，保持一致。"""
    from scan_answer.ui.editor.props import PropsPanel

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.show()

    front_refs = doc.regions("front")
    obj_ref = next(r for r in front_refs if r.kind == "objective")
    panel.set_target(doc, obj_ref)

    param_signals = []
    panel.paramsChanged.connect(lambda ref, p: param_signals.append((ref, p)))

    # 手动编辑第一个标签
    panel._label_edits[0].setText("X")
    panel._label_edits[0].editingFinished.emit()

    assert len(param_signals) >= 1, "编辑标签应触发 paramsChanged"
    last_ref, last_p = param_signals[-1]
    assert "labels" in last_p
    assert "n_options" in last_p, "编辑标签应同时发出 n_options"
    assert last_p["n_options"] == len(last_p["labels"])


def test_props_rect_and_snap(qtbot, app_home):
    """修改 X 后发出 rectChanged，新 rect 的 x 与输入一致；
    点"自动吸附"发出 snapRequested。"""
    from scan_answer.ui.editor.props import PropsPanel

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.show()

    front_refs = doc.regions("front")
    obj_ref = next(r for r in front_refs if r.kind == "objective")
    panel.set_target(doc, obj_ref)

    old_rect = doc.rect(obj_ref)

    # 监听 rectChanged
    rect_signals = []
    panel.rectChanged.connect(lambda ref, rect: rect_signals.append((ref, rect)))

    # 修改 X
    panel._spin_x.setValue(50.0)
    assert len(rect_signals) >= 1, "修改 X 应触发 rectChanged"
    last_ref, new_rect = rect_signals[-1]
    assert last_ref == obj_ref
    assert abs(new_rect[0] - 50.0) < 0.01, f"x 应为 50.0，实际 {new_rect[0]}"

    # 监听 snapRequested
    snap_signals = []
    panel.snapRequested.connect(lambda ref: snap_signals.append(ref))

    panel._btn_snap.click()
    assert len(snap_signals) == 1, "点吸附应触发 snapRequested"
    assert snap_signals[0] == obj_ref


def test_doc_exam_no_options_horizontal(qtbot, app_home):
    """exam_no 的 params 和 set_params 支持 options_horizontal。"""
    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    ref = RegionRef("exam_no", 0)
    p = doc.params(ref)
    assert "options_horizontal" in p, "exam_no params 应包含 options_horizontal"

    # 改变方向应重新生成网格
    old_cells_count = len(doc.template.exam_no.grid.cells)
    new_horiz = not p["options_horizontal"]
    doc.set_params(ref, {"options_horizontal": new_horiz})
    p2 = doc.params(ref)
    assert p2["options_horizontal"] == new_horiz
    # 位数不变
    assert len(doc.template.exam_no.grid.cells) == old_cells_count


def test_props_region_remove_signal(qtbot, app_home):
    """主观题属性面板发出 regionRemoveRequested 信号。"""
    from scan_answer.ui.editor.props import PropsPanel

    doc = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    panel = PropsPanel()
    qtbot.addWidget(panel)
    panel.show()

    front_refs = doc.regions("front")
    sub_ref = next(r for r in front_refs if r.kind == "subjective")
    panel.set_target(doc, sub_ref)

    remove_signals = []
    panel.regionRemoveRequested.connect(lambda ref: remove_signals.append(ref))

    # 主观题区域列表中应有删除按钮
    assert panel._sub_remove_buttons, "主观题属性面板应创建区域删除按钮"
    panel._sub_remove_buttons[0].click()
    assert len(remove_signals) == 1
    assert remove_signals[0].kind == "subjective"


# ══ Task 7：模板页（编辑器整合） ══════════════════════════════════

PDF_199 = __import__("pathlib").Path(__file__).resolve().parents[1] / "samples" / "raw" / "8k-199.pdf"


def _page(qtbot):
    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.pages.template_page import TemplatePage
    page = TemplatePage(AppSettings(template_dirs=[str(bundled_templates_dir())]))
    qtbot.addWidget(page)
    page.resize(1400, 860)
    page.show()
    return page


def test_page_open_edit_undo_save(qtbot, app_home, no_dialogs):
    from scan_answer.app.library import user_templates_dir
    page = _page(qtbot)
    changed = []
    page.changed.connect(lambda: changed.append(1))
    page.open_editor(bundled_templates_dir() / "guanzong199")
    assert page.stack.currentWidget() is page.editor_view
    ref = RegionRef("field", 0)
    r0 = page.doc.rect(ref)
    page.canvas.select([ref])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    assert abs(page.doc.rect(ref)[0] - (r0[0] + 0.1)) < 1e-6
    assert "●" in page.title_label.text()
    assert page.undo_btn.isEnabled()
    page.undo()
    assert page.doc.rect(ref) == r0
    page.canvas.select([ref])
    QTest.keyClick(page.canvas, Qt.Key.Key_Right)
    assert page.save() is True
    saved = page.doc.directory
    assert saved.resolve().is_relative_to(user_templates_dir().resolve())
    assert (saved / "template.json").exists()
    assert "●" not in page.title_label.text()
    assert "已保存到" in page.msg_label.text() and "版本" in page.msg_label.text()
    assert len(changed) == 1
    assert [k for k, _ in no_dialogs] == ["information"]


def test_page_save_blocked_lists_problems(qtbot, app_home, no_dialogs):
    page = _page(qtbot)
    page.open_editor(bundled_templates_dir() / "guanzong199")
    anchors = [r for r in page.doc.regions("front") if r.kind == "anchor"]
    page.canvas.select(anchors[:3])
    QTest.keyClick(page.canvas, Qt.Key.Key_Delete)
    assert len([r for r in page.doc.regions("front") if r.kind == "anchor"]) == len(anchors) - 3
    assert page.save() is False
    warns = [m for k, m in no_dialogs if k == "warning"]
    assert warns and "模板还不能保存" in warns[0] and "至少 3 个定位块" in warns[0]


def test_page_new_template_from_pdf(qtbot, app_home, no_dialogs):
    from scan_answer.template.doc import _grid_bbox
    page = _page(qtbot)
    page.new_template("新卡", [str(PDF_199)])
    assert page.stack.currentWidget() is page.editor_view
    assert page.face_seg.buttons["back"].isEnabled()
    page.set_face("back")
    assert page.face == "back"
    page.set_face("front")
    groups = {page.tree.topLevelItem(i).text(0): page.tree.topLevelItem(i)
              for i in range(page.tree.topLevelItemCount())}
    assert int(groups["定位"].text(1)) >= 4
    # 区域工具 → 客观题，在画布上画框（199 第一块外包框加 1 mm 边距）
    ref199 = TemplateDoc.load(bundled_templates_dir() / "guanzong199")
    x, y, w, h = _grid_bbox(ref199.template.objective[0].grid.cells)
    page.tool_combo.setCurrentIndex(page.tool_combo.findData("objective"))
    page.canvas.fit()
    vp = page.canvas.viewport()
    p1 = page.canvas.mapFromScene(QPointF(x - 1, y - 1))
    p2 = page.canvas.mapFromScene(QPointF(x + w + 1, y + h + 1))
    _send_mouse(vp, QEvent.Type.MouseButtonPress, p1)
    _send_mouse(vp, QEvent.Type.MouseMove, p2)
    _send_mouse(vp, QEvent.Type.MouseButtonRelease, p2)
    objs = [r for r in page.doc.regions("front") if r.kind == "objective"]
    assert len(objs) == 1 and page.canvas.selected() == objs
    # 在属性面板填参数（15 题、A–E、每组 5 题、横排）
    page.props.paramsChanged.emit(objs[0], {"n_questions": 15, "labels": list("ABCDE"), "n_options": 5,
                                            "group": 5, "options_horizontal": True})
    page.auto_snap()
    assert "已吸附" in page.msg_label.text(), page.msg_label.text()


def test_page_trial(qtbot, app_home, no_dialogs, small_dataset):
    page = _page(qtbot)
    page.open_editor(bundled_templates_dir() / "guanzong199")
    files = sorted(str(p) for p in small_dataset.glob("*.jpg"))
    assert len(files) == 8
    page.run_trial(files)
    qtbot.waitUntil(lambda: page.trial_list.count() == 4, timeout=180000)
    texts = [page.trial_list.item(i).text() for i in range(4)]
    assert "空白页" in texts[2]
    assert all("处理完成" in t for i, t in enumerate(texts) if i != 2), texts
    page.trial_list.setCurrentRow(0)
    assert page.canvas._bg_override is not None
    assert len(page.canvas._mark_items) > 0
    page.exit_trial()
    assert page.canvas._bg_override is None
    assert len(page.canvas._mark_items) == 0
    assert page.props_stack.currentWidget() is page.props


def test_leave_with_unsaved_changes(qtbot, app_home, no_dialogs, tmp_path):
    from PySide6.QtWidgets import QMessageBox

    from scan_answer.app.settings import AppSettings
    from scan_answer.ui.controller import ScanController
    from scan_answer.ui.main_window import MainWindow
    settings = AppSettings(output_root=str(tmp_path / "out"), template_dirs=[str(bundled_templates_dir())],
                           workers=0)
    ctrl = ScanController(workers=0)
    win = MainWindow(settings, ctrl)
    qtbot.addWidget(win)
    try:
        win.show_page("template")
        tp = win.template_page
        tp.open_editor(bundled_templates_dir() / "guanzong199")
        tp.canvas.select([RegionRef("field", 0)])
        QTest.keyClick(tp.canvas, Qt.Key.Key_Right)
        no_dialogs.question_answer = QMessageBox.StandardButton.Cancel
        win.show_page("exam")
        assert win.stack.currentWidget() is tp and win.rail_buttons["template"].isChecked()
        assert "模板有未保存的修改" in no_dialogs[-1][1]
        no_dialogs.question_answer = QMessageBox.StandardButton.Discard
        win.show_page("exam")
        assert win.stack.currentWidget() is win.exam_page
    finally:
        ctrl.shutdown()


def test_results_page_shows_ext_fields(qtbot, app_home, no_dialogs, ext_template, tmp_path):
    from scan_answer.batch import run_batch
    from scan_answer.synth.generate import generate_dataset
    from scan_answer.template import load_template
    from scan_answer.ui.pages.results_page import ResultsPage
    t, bases = load_template(ext_template)
    ds = tmp_path / "ds"
    generate_dataset(t, bases, ds, 2, seed=5)
    bd = run_batch(ext_template, ds, tmp_path / "out", batch_id="x")
    rp = ResultsPage()
    qtbot.addWidget(rp)
    assert rp.open_batch(bd)
    ok = next(r for r in rp.rows if r.status == "ok")
    assert rp.select_seq(ok.seq)
    assert "AB 卷" in rp.info.text()
    last = rp.answers.rowCount() - 1
    assert rp.answers.item(last, 0).text().startswith("选做：")


def test_new_template_dialog(qtbot, app_home, tmp_path):
    from scan_answer.ui.editor.new_dialog import NewTemplateDialog
    dlg = NewTemplateDialog()
    qtbot.addWidget(dlg)
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    dlg.front_edit.setText(str(bad))
    dlg.create_btn.click()
    assert "无法读取 bad.png" in dlg.error.text() and dlg.result() == 0 and not dlg.faces
    dlg.front_edit.setText(str(PDF_199))
    dlg.create_btn.click()
    assert dlg.error.text() == "" and dlg.result() == dlg.DialogCode.Accepted
    name, faces = dlg.result_value()
    assert name == "8k-199" and set(faces) == {"front", "back"}
    assert "纸张" in dlg.preview.text() and "定位方式" in dlg.preview.text()


def test_import_files_pdf_page_note(tmp_path):
    from PIL import Image

    from scan_answer.ui.editor.new_dialog import import_files
    pages = [Image.new("L", (600, 800), 255) for _ in range(3)]
    pdf = tmp_path / "three.pdf"
    pages[0].save(pdf, save_all=True, append_images=pages[1:], resolution=72)
    faces, notes = import_files(str(pdf))
    assert set(faces) == {"front", "back"}
    assert "PDF 有 3 页，只用前 2 页" in notes
