"""模板编辑画布：彩色区域图元、8 手柄缩放、框选、方向键微调、画框工具。

场景坐标直接用 mm：底图按 25.4/dpi 缩放，区域矩形就是 mm 值。
画布不直接修改 doc，只发信号。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (QBrush, QColor, QFontMetricsF, QImage, QMouseEvent, QPainter, QPen,
                            QPixmap, QTransform, QWheelEvent)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsPixmapItem,
                                QGraphicsRectItem, QGraphicsScene,
                                QGraphicsSimpleTextItem, QGraphicsView)

from .. import theme
from ...template.doc import KIND_COLOR, TemplateDoc, RegionRef
from ...template.model import Rect

if TYPE_CHECKING:
    from PySide6.QtWidgets import QWidget

# 手柄大小（屏幕像素）
_HANDLE_SIZE = 8
# 方向键微调步长（mm）
_NUDGE_STEP = 0.1
_NUDGE_STEP_SHIFT = 1.0
# 缩放限制
_ZOOM_MIN = 0.05
_ZOOM_MAX = 8.0
_ZOOM_FACTOR = 1.15
# 最小区域尺寸（mm）
_MIN_SIZE = 0.5
# 画框最小尺寸（mm）
_DRAW_MIN_SIZE = 1.0


def _ndarray_to_qimage(img: np.ndarray) -> QImage:
    """灰度或 BGR ndarray → QImage。"""
    if img.ndim == 2:
        h, w = img.shape
        return QImage(img.data, w, h, w, QImage.Format.Format_Grayscale8).copy()
    h, w, ch = img.shape
    if ch == 3:
        # BGR → RGB
        rgb = img[:, :, ::-1].copy()
        return QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
    return QImage(img.data, w, h, w * ch, QImage.Format.Format_ARGB32).copy()


# ── 手柄 ────────────────────────────────────────────────────────


class _HandleItem(QGraphicsRectItem):
    """选中区域的缩放手柄。大小固定 8 屏幕像素，不随缩放变化。"""

    # 手柄位置标识
    POSITIONS = ("tl", "tc", "tr", "ml", "mr", "bl", "bc", "br")

    def __init__(self, pos_name: str, parent: "RegionItem"):
        super().__init__(parent)
        self._handle_pos = pos_name
        self._color = parent._color
        self.setBrush(QBrush(QColor("#ffffff")))
        pen = QPen(self._color)
        pen.setCosmetic(True)
        pen.setWidth(1)
        self.setPen(pen)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self.setCursor(self._cursor_for(pos_name))
        self.setAcceptHoverEvents(True)
        self.setZValue(100)
        self.hide()

    @staticmethod
    def _cursor_for(pos: str) -> Qt.CursorShape:
        diag_tl = (Qt.CursorShape.SizeFDiagCursor)
        diag_tr = (Qt.CursorShape.SizeBDiagCursor)
        h = Qt.CursorShape.SizeHorCursor
        v = Qt.CursorShape.SizeVerCursor
        return {"tl": diag_tl, "br": diag_tl, "tr": diag_tr, "bl": diag_tr,
                "tc": v, "bc": v, "ml": h, "mr": h}[pos]

    def reposition(self, rect: QRectF, view: QGraphicsView) -> None:
        """根据父项矩形和视图变换重新定位手柄。"""
        # 手柄中心在父项边界的哪个位置
        x, y = rect.x(), rect.y()
        w, h = rect.width(), rect.height()
        cx = {"tl": x, "tc": x + w / 2, "tr": x + w,
              "ml": x, "mr": x + w,
              "bl": x, "bc": x + w / 2, "br": x + w}[self._handle_pos]
        cy = {"tl": y, "tc": y, "tr": y,
              "ml": y + h / 2, "mr": y + h / 2,
              "bl": y + h, "bc": y + h, "br": y + h}[self._handle_pos]

        # 手柄在场景坐标中的大小
        hs = _HANDLE_SIZE / 2
        self.setRect(QRectF(-hs, -hs, _HANDLE_SIZE, _HANDLE_SIZE))
        # 设置位置为父坐标系下的场景点
        self.setPos(cx, cy)


# ── 区域图元 ────────────────────────────────────────────────────


class RegionItem(QGraphicsRectItem):
    """一个模板区域的图元：彩色边框 + 标签 + 网格线（如有）+ 8 手柄。"""

    def __init__(self, ref: RegionRef, rect_mm: Rect, color_hex: str,
                 label: str, cells: list | None, parent=None):
        x, y, w, h = rect_mm
        super().__init__(QRectF(x, y, w, h), parent)
        self._ref = ref
        self._color = QColor(color_hex)
        self._label_text = label
        self.setToolTip(label)
        self._cells = cells  # 网格类区域的格子列表

        # 描边和填充
        pen = QPen(self._color, 2)
        pen.setCosmetic(True)
        if ref.kind == "ignore":
            pen.setStyle(Qt.PenStyle.DashLine)
        elif ref.kind == "identity":
            pen.setStyle(Qt.PenStyle.DashDotLine)
        self.setPen(pen)
        self.setBrush(QBrush(theme.qcolor(color_hex, 30)))

        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, False)
        self.setAcceptHoverEvents(True)
        self.setZValue(10)

        # 标签：白字配同色底
        self._tag_bg = QGraphicsRectItem(self)
        self._tag_bg.setBrush(QBrush(self._color))
        self._tag_bg.setPen(QPen(Qt.PenStyle.NoPen))
        self._tag_bg.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self._tag_bg.setZValue(50)

        self._tag_text = QGraphicsSimpleTextItem(label, self)
        self._tag_text.setBrush(QBrush(QColor("#ffffff")))
        self._tag_text.setFont(theme.ui_font(11, bold=True))
        self._tag_text.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations, True)
        self._tag_text.setZValue(51)

        # 手柄（初始隐藏）
        self._handles: list[_HandleItem] = []
        for pos in _HandleItem.POSITIONS:
            h = _HandleItem(pos, self)
            self._handles.append(h)

        # 网格线图元（cosmetic pen）
        self._grid_lines: list[QGraphicsRectItem] = []

        self._set_label_visible(ref.kind != "sync")
        self._update_label_pos()
        self._draw_grid_cells()

    def _set_label_visible(self, visible: bool) -> None:
        self._tag_bg.setVisible(visible)
        self._tag_text.setVisible(visible)

    def update_from_doc(self, rect_mm: Rect, label: str, cells: list | None) -> None:
        """从 doc 同步位置和标签。"""
        x, y, w, h = rect_mm
        self.setRect(QRectF(x, y, w, h))
        self._label_text = label
        self.setToolTip(label)
        self._cells = cells
        self._tag_text.setText(label)
        self._update_label_pos()
        self._draw_grid_cells()

    def _update_label_pos(self) -> None:
        """标签放在区域左上角；超出场景（底图）右边或下边时往左 / 往上挪，保证完整可见。"""
        r = self.rect()
        x, y = r.x(), r.y()
        scene = self.scene()
        views = scene.views() if scene is not None else []
        scale = abs(views[0].transform().m11()) if views else 0.0
        text = self._label_text
        if scale > 0 and r.width() * scale >= 48:
            # 标签比区域宽时截断（完整名称见悬停提示），避免相邻区域的标签互相压住
            text = QFontMetricsF(self._tag_text.font()).elidedText(
                self._label_text, Qt.TextElideMode.ElideRight, r.width() * scale - 6)
        if self._tag_text.text() != text:
            self._tag_text.setText(text)
        br = self._tag_text.boundingRect()
        self._tag_bg.setRect(br.adjusted(-3, -1, 3, 1))
        if scene is not None:
            if scale > 0:
                # 标签忽略缩放（屏幕像素），换算成场景 mm
                w_mm, h_mm = (br.width() + 3) / scale, (br.height() + 1) / scale
                sr = scene.sceneRect()
                x = max(sr.left(), min(x, sr.right() - w_mm))
                y = max(sr.top(), min(y, sr.bottom() - h_mm))
        self._tag_text.setPos(x, y)
        self._tag_bg.setPos(x, y)

    def _draw_grid_cells(self) -> None:
        """画网格线。"""
        scene = self.scene()
        for line in self._grid_lines:
            if scene:
                scene.removeItem(line)
        self._grid_lines.clear()
        if not self._cells:
            return
        pen = QPen(self._color, 1)
        pen.setCosmetic(True)
        # cells 可能是 list[list[Rect]] 或 list[Rect]（choose_one）
        flat: list[Rect] = []
        if self._cells and isinstance(self._cells[0], (list, tuple)):
            first = self._cells[0]
            if first and isinstance(first[0], (list, tuple)):
                # list[list[Rect]]
                for row in self._cells:
                    flat.extend(row)
            else:
                # list[Rect] — 平铺
                flat = list(self._cells)
        for cx, cy, cw, ch in flat:
            item = QGraphicsRectItem(QRectF(cx, cy, cw, ch), self)
            item.setPen(pen)
            item.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            item.setZValue(11)
            self._grid_lines.append(item)

    def set_selected_visual(self, selected: bool, view: QGraphicsView | None = None) -> None:
        """设置选中/取消选中的视觉效果。"""
        if self._ref.kind == "sync":  # 同步头标记多而密，标签只在选中时显示
            self._set_label_visible(selected)
        self._update_label_pos()
        if selected:
            pen = QPen(self._color, 3)
            pen.setCosmetic(True)
            self.setPen(pen)
            for h in self._handles:
                if view:
                    h.reposition(self.rect(), view)
                h.show()
        else:
            pen = QPen(self._color, 2)
            pen.setCosmetic(True)
            self.setPen(pen)
            for h in self._handles:
                h.hide()

    def handle_at(self, scene_pos: QPointF, view: QGraphicsView) -> _HandleItem | None:
        """检测场景坐标 scene_pos 处是否有手柄，返回最近的那个。"""
        t = view.transform()
        scale = t.m11() if t.m11() != 0 else 1.0
        radius = (_HANDLE_SIZE / 2 + 2) / abs(scale)
        best: _HandleItem | None = None
        best_dist = float("inf")
        for h in self._handles:
            if not h.isVisible():
                continue
            # 手柄 pos() 在父坐标系（mm）
            hp = self.mapToScene(h.pos())
            dist = (scene_pos - hp).manhattanLength()
            if dist < radius * 2 and dist < best_dist:
                best = h
                best_dist = dist
        return best


# ── 试识别叠层图元 ──────────────────────────────────────────────


class _MarkItem(QGraphicsRectItem):
    """试识别叠层矩形。"""
    pass


# ── 画布主体 ────────────────────────────────────────────────────


class TemplateCanvas(QGraphicsView):
    """模板编辑画布。场景坐标直接用 mm。"""

    regionEdited = Signal(object, object)   # (RegionRef, Rect)
    regionDrawn = Signal(str, object)       # (kind, Rect)
    selectionChanged = Signal(list)         # list[RegionRef]
    cursorMoved = Signal(float, float)      # mm

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing |
            QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.refresh_theme()
        self.setFrameShape(QGraphicsView.Shape.NoFrame)

        self._doc: TemplateDoc | None = None
        self._face: str = ""
        self._bg_item: QGraphicsPixmapItem | None = None
        self._bg_override: np.ndarray | None = None  # set_background 临时底图
        self._region_items: dict[RegionRef, RegionItem] = {}
        self._mark_items: list[_MarkItem] = []
        self._selected: list[RegionRef] = []
        self._tool: str | None = None  # None = 选择工具

        # 鼠标交互状态
        self._dragging: str | None = None  # "move", "handle", "draw", "rubber", "pan"
        self._drag_handle: _HandleItem | None = None
        self._drag_start_scene: QPointF | None = None
        self._drag_start_rects: dict[RegionRef, Rect] = {}
        self._rubber_rect: QGraphicsRectItem | None = None
        self._draw_rect: QGraphicsRectItem | None = None
        self._pan_last: QPointF | None = None
        self._space_pressed = False
        self.nudging = False  # 正在处理方向键微调（见 keyPressEvent）
        self._rubber_additive = False
        self._bottom_bar: QWidget | None = None
        self._bottom_gap = 0

    # ── 公开接口 ────────────────────────────────────────────────

    def refresh_theme(self) -> None:
        """Update the surroundings without touching paper, annotations or zoom."""
        self.setBackgroundBrush(QBrush(theme.qcolor(theme.CANVAS)))

    def set_bottom_bar(self, bar: QWidget, gap: int = 14) -> None:
        """把视图条挂在画布底部：可视区下方留出一条带放它（下面再空 gap），适应窗口和滚动时卡面不被挡住。

        横向滚动条出现时排在这条带下面，视图条始终在带内、滚动条之上，不会盖住滚动条；留白与画布同色。
        """
        self._bottom_bar = bar
        self._bottom_gap = gap
        bar.setParent(self)
        bar.show()
        self.setViewportMargins(0, 0, 0, bar.sizeHint().height() + gap)
        theme.set_style(self, "QGraphicsView { background: $CANVAS; }")
        self._place_bottom_bar()

    def _place_bottom_bar(self) -> None:
        bar = self._bottom_bar
        if bar is None:
            return
        vp = self.viewport().geometry()  # 已扣除滚动条和底部留白
        w = min(bar.sizeHint().width(), vp.width())
        bar.setGeometry(vp.left() + (vp.width() - w) // 2, vp.bottom() + 1, w, bar.sizeHint().height())
        bar.raise_()

    def viewportEvent(self, event) -> bool:
        # 滚动条出现 / 消失、窗口缩放都会改变可视区尺寸，视图条跟着重新摆放
        if event.type() == QEvent.Type.Resize:
            self._place_bottom_bar()
        return super().viewportEvent(event)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._place_bottom_bar()

    def set_doc(self, doc: TemplateDoc, face: str) -> None:
        """重建全部图元。"""
        self._doc = doc
        self._face = face
        self._selected.clear()
        self._rebuild()

    def refresh(self) -> None:
        """按 doc 当前内容同步图元位置和标签（不重建底图）。"""
        if not self._doc:
            return
        refs = self._doc.regions(self._face)
        ref_set = set(refs)

        # 删除不再存在的图元
        to_remove = [r for r in self._region_items if r not in ref_set]
        for r in to_remove:
            item = self._region_items.pop(r)
            self._scene.removeItem(item)

        # 更新或新增
        for ref in refs:
            rect_mm = self._doc.rect(ref)
            color_hex = theme.REGION[KIND_COLOR[ref.kind]]
            label = self._doc.title(ref)
            cells = self._get_cells(ref)
            if ref in self._region_items:
                self._region_items[ref].update_from_doc(rect_mm, label, cells)
            else:
                item = RegionItem(ref, rect_mm, color_hex, label, cells)
                self._scene.addItem(item)
                self._region_items[ref] = item

        # 更新选中状态：删掉已不存在的 ref
        new_sel = [r for r in self._selected if r in ref_set]
        changed = new_sel != self._selected
        self._selected = new_sel
        # I1: 无论选中集是否变化，都要重新定位手柄（rect 可能已变）
        self._update_selection_visuals()
        if changed:
            self.selectionChanged.emit(list(self._selected))

    def set_tool(self, kind: str | None) -> None:
        """None = 选择工具。画框途中换工具（如按 Esc）会取消正在拖出的框。"""
        if self._dragging == "draw":
            self._dragging = None
            self._drag_start_scene = None
            if self._draw_rect:
                self._scene.removeItem(self._draw_rect)
                self._draw_rect = None
        self._tool = kind
        if kind:
            self.setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.setCursor(Qt.CursorShape.ArrowCursor)

    def select(self, refs: list[RegionRef]) -> None:
        """程序化选中。"""
        old = self._selected[:]
        self._selected = list(refs)
        self._update_selection_visuals()
        if self._selected != old:
            self.selectionChanged.emit(list(self._selected))

    def selected(self) -> list[RegionRef]:
        return list(self._selected)

    def set_background(self, img: np.ndarray | None) -> None:
        """试识别时临时换成样卡对齐图；None 恢复底图。"""
        self._bg_override = img
        self._update_bg()

    def set_marks(self, marks: list[tuple[Rect, str, int]]) -> None:
        """试识别叠层。"""
        for m in self._mark_items:
            self._scene.removeItem(m)
        self._mark_items.clear()
        for rect_mm, color, fill_alpha in marks:
            x, y, w, h = rect_mm
            item = _MarkItem(QRectF(x, y, w, h))
            pen = QPen(QColor(color), 2)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(QBrush(theme.qcolor(color, fill_alpha)))
            item.setZValue(5)
            self._scene.addItem(item)
            self._mark_items.append(item)

    def zoom(self) -> float:
        return self.transform().m11()

    def set_zoom(self, z: float) -> None:
        z = max(_ZOOM_MIN, min(_ZOOM_MAX, z))
        self.setTransform(QTransform.fromScale(z, z))
        self._update_labels()

    def _update_labels(self) -> None:
        """缩放变化后标签的场景尺寸也变了，重新夹到场景内。"""
        for item in self._region_items.values():
            item._update_label_pos()

    def fit(self) -> None:
        if self._bg_item:
            self.fitInView(self._scene.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
            self._update_labels()

    # ── 内部方法 ────────────────────────────────────────────────

    def _rebuild(self) -> None:
        """完全重建场景。"""
        self._scene.clear()
        self._region_items.clear()
        self._mark_items.clear()
        self._bg_item = None

        if not self._doc:
            return

        # 底图
        self._update_bg()

        # 区域图元
        for ref in self._doc.regions(self._face):
            rect_mm = self._doc.rect(ref)
            color_hex = theme.REGION[KIND_COLOR[ref.kind]]
            label = self._doc.title(ref)
            cells = self._get_cells(ref)
            item = RegionItem(ref, rect_mm, color_hex, label, cells)
            self._scene.addItem(item)
            self._region_items[ref] = item

        self._update_selection_visuals()

    def _update_bg(self) -> None:
        """更新底图像素图元。"""
        if not self._doc:
            return
        img = self._bg_override if self._bg_override is not None else self._doc.bases.get(self._face)
        if img is None:
            return
        qimg = _ndarray_to_qimage(img)
        pix = QPixmap.fromImage(qimg)
        scale = 25.4 / self._doc.template.dpi  # 像素 → mm
        if self._bg_item is None:
            self._bg_item = QGraphicsPixmapItem(pix)
            self._bg_item.setZValue(0)
            self._bg_item.setScale(scale)
            self._scene.addItem(self._bg_item)
        else:
            self._bg_item.setPixmap(pix)
            self._bg_item.setScale(scale)

        # 设置场景大小
        w_mm = pix.width() * scale
        h_mm = pix.height() * scale
        self._scene.setSceneRect(QRectF(0, 0, w_mm, h_mm))

    def _get_cells(self, ref: RegionRef) -> list | None:
        """获取网格区域的格子列表。"""
        if not self._doc:
            return None
        t = self._doc.template
        if ref.kind == "objective":
            return t.objective[ref.index].grid.cells
        if ref.kind == "exam_no" and t.exam_no:
            return t.exam_no.grid.cells
        if ref.kind == "omr_field":
            return t.omr_fields[ref.index].grid.cells
        if ref.kind == "choose_one":
            return t.choose_one[ref.index].cells
        return None

    def _update_selection_visuals(self) -> None:
        """更新所有区域的选中/未选中视觉。"""
        sel_set = set(self._selected)
        for ref, item in self._region_items.items():
            item.set_selected_visual(ref in sel_set, self)

    def _region_at(self, scene_pos: QPointF) -> RegionRef | None:
        """找到场景坐标处的区域。"""
        # 从上到下找（z 值高的优先）
        best: RegionItem | None = None
        best_area = float("inf")
        for ref, item in self._region_items.items():
            if item.rect().contains(scene_pos):
                area = item.rect().width() * item.rect().height()
                if area < best_area:
                    best = item
                    best_area = area
        return best._ref if best else None

    def _handle_at(self, scene_pos: QPointF) -> tuple[RegionItem, _HandleItem] | None:
        """检查场景坐标处是否有选中区域的手柄。"""
        for ref in self._selected:
            item = self._region_items.get(ref)
            if item:
                h = item.handle_at(scene_pos, self)
                if h:
                    return (item, h)
        return None

    # ── 鼠标事件 ────────────────────────────────────────────────

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # 中键平移
        if event.button() == Qt.MouseButton.MiddleButton:
            self._dragging = "pan"
            self._pan_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        # 空格 + 拖动平移
        if self._space_pressed and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = "pan"
            self._pan_last = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return

        scene_pos = self.mapToScene(event.position().toPoint())

        # 画框工具
        if self._tool is not None:
            self._dragging = "draw"
            self._drag_start_scene = scene_pos
            pen = QPen(QColor("#555555"), 1, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            self._draw_rect = QGraphicsRectItem()
            self._draw_rect.setPen(pen)
            self._draw_rect.setBrush(QBrush(theme.qcolor("#555555", 20)))
            self._draw_rect.setZValue(200)
            self._scene.addItem(self._draw_rect)
            event.accept()
            return

        # 选择工具：先检查手柄
        hit_handle = self._handle_at(scene_pos)
        if hit_handle:
            item, handle = hit_handle
            self._dragging = "handle"
            self._drag_handle = handle
            self._drag_start_scene = scene_pos
            self._drag_start_rects = {
                item._ref: (item.rect().x(), item.rect().y(),
                            item.rect().width(), item.rect().height())
            }
            event.accept()
            return

        # 检查是否点击在某个区域上
        hit_ref = self._region_at(scene_pos)
        if hit_ref:
            old_sel = self._selected[:]
            mods = event.modifiers()
            if mods & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier):
                # 加选/取消选
                if hit_ref in self._selected:
                    self._selected.remove(hit_ref)
                else:
                    self._selected.append(hit_ref)
            else:
                if hit_ref not in self._selected:
                    self._selected = [hit_ref]
            self._update_selection_visuals()
            if self._selected != old_sel:
                self.selectionChanged.emit(list(self._selected))

            # 开始拖动移动
            self._dragging = "move"
            self._drag_start_scene = scene_pos
            self._drag_start_rects = {}
            for r in self._selected:
                it = self._region_items.get(r)
                if it:
                    self._drag_start_rects[r] = (
                        it.rect().x(), it.rect().y(),
                        it.rect().width(), it.rect().height()
                    )
            event.accept()
            return

        # 空白处：开始橡皮筋框选
        # Shift/Ctrl 框选保留已有选择（additive）
        self._rubber_additive = bool(
            event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
        )
        if not self._rubber_additive:
            old_sel = self._selected[:]
            self._selected.clear()
            self._update_selection_visuals()
            if old_sel:
                self.selectionChanged.emit(list(self._selected))

        self._dragging = "rubber"
        self._drag_start_scene = scene_pos
        pen = QPen(QColor("#3388dd"), 1, Qt.PenStyle.DashLine)
        pen.setCosmetic(True)
        self._rubber_rect = QGraphicsRectItem()
        self._rubber_rect.setPen(pen)
        self._rubber_rect.setBrush(QBrush(theme.qcolor("#3388dd", 30)))
        self._rubber_rect.setZValue(200)
        self._scene.addItem(self._rubber_rect)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        scene_pos = self.mapToScene(event.position().toPoint())
        self.cursorMoved.emit(scene_pos.x(), scene_pos.y())

        if self._dragging == "pan" and self._pan_last is not None:
            delta = event.position() - self._pan_last
            self._pan_last = event.position()
            self.horizontalScrollBar().setValue(
                int(self.horizontalScrollBar().value() - delta.x()))
            self.verticalScrollBar().setValue(
                int(self.verticalScrollBar().value() - delta.y()))
            event.accept()
            return

        if self._dragging == "draw" and self._drag_start_scene and self._draw_rect:
            r = QRectF(self._drag_start_scene, scene_pos).normalized()
            self._draw_rect.setRect(r)
            event.accept()
            return

        if self._dragging == "rubber" and self._drag_start_scene and self._rubber_rect:
            r = QRectF(self._drag_start_scene, scene_pos).normalized()
            self._rubber_rect.setRect(r)
            event.accept()
            return

        if self._dragging == "move" and self._drag_start_scene:
            dx = scene_pos.x() - self._drag_start_scene.x()
            dy = scene_pos.y() - self._drag_start_scene.y()
            for ref, (ox, oy, ow, oh) in self._drag_start_rects.items():
                item = self._region_items.get(ref)
                if item:
                    item.setRect(QRectF(ox + dx, oy + dy, ow, oh))
                    item._update_label_pos()
                    # I2: 拖动过程中重新定位手柄
                    item.set_selected_visual(True, self)
            event.accept()
            return

        if self._dragging == "handle" and self._drag_handle and self._drag_start_scene:
            self._apply_handle_drag(scene_pos)
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if self._dragging == "pan":
            self._dragging = None
            self._pan_last = None
            if self._tool:
                self.setCursor(Qt.CursorShape.CrossCursor)
            elif self._space_pressed:
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return

        if self._dragging == "draw":
            self._dragging = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._draw_rect:
                self._scene.removeItem(self._draw_rect)
                self._draw_rect = None
            if self._drag_start_scene and self._tool:
                r = QRectF(self._drag_start_scene, scene_pos).normalized()
                if r.width() >= _DRAW_MIN_SIZE and r.height() >= _DRAW_MIN_SIZE:
                    rect: Rect = (round(r.x(), 3), round(r.y(), 3),
                                  round(r.width(), 3), round(r.height(), 3))
                    self.regionDrawn.emit(self._tool, rect)
            self._drag_start_scene = None
            event.accept()
            return

        if self._dragging == "rubber":
            self._dragging = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._rubber_rect:
                r = QRectF(self._drag_start_scene, scene_pos).normalized()
                self._scene.removeItem(self._rubber_rect)
                self._rubber_rect = None
                # 框选到的区域
                box_sel = [ref for ref, item in self._region_items.items()
                           if r.intersects(item.rect())]
                old_sel = self._selected[:]
                if self._rubber_additive:
                    # Shift 框选：合并，不重复
                    merged = list(self._selected)
                    existing = set(self._selected)
                    for ref in box_sel:
                        if ref not in existing:
                            merged.append(ref)
                    self._selected = merged
                else:
                    self._selected = box_sel
                self._update_selection_visuals()
                if self._selected != old_sel:
                    self.selectionChanged.emit(list(self._selected))
            self._drag_start_scene = None
            event.accept()
            return

        if self._dragging == "move":
            self._dragging = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._drag_start_scene:
                dx = scene_pos.x() - self._drag_start_scene.x()
                dy = scene_pos.y() - self._drag_start_scene.y()
                if abs(dx) > 1e-6 or abs(dy) > 1e-6:
                    for ref, (ox, oy, ow, oh) in self._drag_start_rects.items():
                        new_rect: Rect = (round(ox + dx, 3), round(oy + dy, 3),
                                          round(ow, 3), round(oh, 3))
                        self.regionEdited.emit(ref, new_rect)
            self._drag_start_scene = None
            self._drag_start_rects.clear()
            event.accept()
            return

        if self._dragging == "handle":
            self._dragging = None
            scene_pos = self.mapToScene(event.position().toPoint())
            if self._drag_handle and self._drag_start_rects:
                ref = list(self._drag_start_rects.keys())[0]
                item = self._region_items.get(ref)
                if item:
                    r = item.rect()
                    new_rect: Rect = (round(r.x(), 3), round(r.y(), 3),
                                      round(r.width(), 3), round(r.height(), 3))
                    self.regionEdited.emit(ref, new_rect)
            self._drag_handle = None
            self._drag_start_scene = None
            self._drag_start_rects.clear()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _apply_handle_drag(self, scene_pos: QPointF) -> None:
        """根据手柄拖动更新区域图元位置。"""
        if not self._drag_handle or not self._drag_start_rects:
            return
        ref = list(self._drag_start_rects.keys())[0]
        ox, oy, ow, oh = self._drag_start_rects[ref]
        item = self._region_items.get(ref)
        if not item:
            return

        dx = scene_pos.x() - self._drag_start_scene.x()
        dy = scene_pos.y() - self._drag_start_scene.y()
        hp = self._drag_handle._handle_pos

        nx, ny, nw, nh = ox, oy, ow, oh
        if "l" in hp:
            nx = ox + dx
            nw = ow - dx
        if "r" in hp:
            nw = ow + dx
        if "t" in hp:
            ny = oy + dy
            nh = oh - dy
        if "b" in hp:
            nh = oh + dy

        # 最小尺寸限制
        if nw < _MIN_SIZE:
            if "l" in hp:
                nx = ox + ow - _MIN_SIZE
            nw = _MIN_SIZE
        if nh < _MIN_SIZE:
            if "t" in hp:
                ny = oy + oh - _MIN_SIZE
            nh = _MIN_SIZE

        item.setRect(QRectF(nx, ny, nw, nh))
        item._update_label_pos()
        item.set_selected_visual(True, self)

    # ── 键盘事件 ────────────────────────────────────────────────

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_pressed = True
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return

        if not self._selected:
            super().keyPressEvent(event)
            return

        step = _NUDGE_STEP_SHIFT if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else _NUDGE_STEP
        dx, dy = 0.0, 0.0
        if event.key() == Qt.Key.Key_Left:
            dx = -step
        elif event.key() == Qt.Key.Key_Right:
            dx = step
        elif event.key() == Qt.Key.Key_Up:
            dy = -step
        elif event.key() == Qt.Key.Key_Down:
            dy = step
        else:
            super().keyPressEvent(event)
            return

        # 方向键微调期间置 nudging，接收方据此把连续微调（含按住不放的自动重复）合并成一步撤销
        self.nudging = True
        try:
            for ref in list(self._selected):
                item = self._region_items.get(ref)
                if item:
                    r = item.rect()
                    new_r = QRectF(r.x() + dx, r.y() + dy, r.width(), r.height())
                    item.setRect(new_r)
                    item._update_label_pos()
                    rect_out: Rect = (round(new_r.x(), 3), round(new_r.y(), 3),
                                      round(new_r.width(), 3), round(new_r.height(), 3))
                    self.regionEdited.emit(ref, rect_out)
        finally:
            self.nudging = False
        event.accept()

    def keyReleaseEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Space:
            self._space_pressed = False
            if self._tool:
                self.setCursor(Qt.CursorShape.CrossCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().keyReleaseEvent(event)

    # ── 滚轮缩放 ───────────────────────────────────────────────

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = _ZOOM_FACTOR if event.angleDelta().y() > 0 else 1.0 / _ZOOM_FACTOR
        new_zoom = self.zoom() * factor
        if _ZOOM_MIN <= new_zoom <= _ZOOM_MAX:
            self.scale(factor, factor)
            self._update_labels()
        event.accept()
