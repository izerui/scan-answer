"""手动定位对话框：自动定位失败的卡，由操作员在原图上依次点出模板的参照点（定位块中心 / 外框四角）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QFont, QPen
from PySide6.QtWidgets import (QComboBox, QDialog, QGraphicsEllipseItem, QGraphicsItem, QGraphicsSimpleTextItem,
                               QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget)

from ...engine.align import px_per_mm, reference_points
from ...imageio import imread_gray
from ...template.model import Template
from .. import theme
from .card_view import CardView, Overlay

FACE_TEXT = {"front": "正面", "back": "背面"}
MIN_POINTS = 3
_UNSET = object()  # 撤销记录里表示“这个点之前没处理过”


def snap_to_blob(gray: np.ndarray, x: float, y: float, size_px: float) -> tuple[float, float]:
    """把点击位置吸附到附近深色块（定位块）的中心；附近没有合适的块时保持原位。"""
    import cv2

    r = int(max(8, size_px * 1.5))
    x0, y0 = max(0, int(x) - r), max(0, int(y) - r)
    patch = gray[y0:int(y) + r, x0:int(x) + r]
    if patch.size == 0:
        return x, y
    n, _labels, stats, centers = cv2.connectedComponentsWithStats((patch < 140).astype(np.uint8))
    best, best_d = None, None
    for k in range(1, n):
        area = stats[k][4]
        if area < 0.3 * size_px * size_px or area > 4 * size_px * size_px:
            continue
        cx, cy = centers[k][0] + x0, centers[k][1] + y0
        d = (cx - x) ** 2 + (cy - y) ** 2
        if best_d is None or d < best_d:
            best, best_d = (float(cx), float(cy)), d
    return best if best is not None else (x, y)


class ClickView(CardView):
    """单击放点、拖动平移、滚轮缩放；看全图用独立按钮（双击的第一下已是一次放点，不能再兼作适配视图）。"""
    clicked = Signal(float, float)  # 图像像素坐标

    def __init__(self, parent=None):
        super().__init__(parent, placeholder="无法读取这张图")
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)
        self._press = None
        self._marks: list[QGraphicsItem] = []

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MouseButton.LeftButton:
            self._press = e.position()
        super().mousePressEvent(e)

    def mouseReleaseEvent(self, e) -> None:
        super().mouseReleaseEvent(e)
        if e.button() == Qt.MouseButton.LeftButton and self._press is not None:
            moved = (e.position() - self._press).manhattanLength()
            self._press = None
            if moved < 5 and self._pix is not None:  # 拖动是平移，只有原地单击才放点
                p = self.mapToScene(e.position().toPoint())
                if self.scene().sceneRect().contains(p):
                    self.clicked.emit(p.x(), p.y())
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def mouseDoubleClickEvent(self, e) -> None:
        e.accept()  # 不继承 CardView 的“双击适应窗口”

    def show_all(self) -> None:
        self._auto_fit = True
        self.fit()

    def set_marks(self, points: list, current: int) -> None:
        for item in self._marks:
            self.scene().removeItem(item)
        self._marks.clear()
        font = QFont(theme.ui_font(12, bold=True))
        for k, pt in enumerate(points):
            if pt is None:
                continue
            color = theme.qcolor(theme.PAPER_ERR if k == current else theme.REGION["obj"])
            dot = QGraphicsEllipseItem(-7, -7, 14, 14)
            dot.setPen(QPen(color, 2))
            dot.setBrush(QBrush(Qt.BrushStyle.NoBrush))
            dot.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIgnoresTransformations)
            dot.setPos(QPointF(*pt))
            dot.setZValue(10)
            label = QGraphicsSimpleTextItem(str(k + 1), dot)  # 挂在圆点下：随圆点一起忽略缩放
            label.setFont(font)
            label.setBrush(color)
            label.setPos(9, -9)
            self.scene().addItem(dot)
            self._marks.append(dot)


class _ImageTab(QWidget):
    """一张原图：选择它对应模板的哪一面（或自动定位），并在图上点参照点。"""
    changed = Signal()

    def __init__(self, path: Path, faces: list[str], default: str | None, parent=None):
        super().__init__(parent)
        self.gray = imread_gray(path)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 8, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("这张图是"))
        self.face_combo = QComboBox()
        for f in faces:
            self.face_combo.addItem(FACE_TEXT.get(f, f), f)
        self.face_combo.addItem("自动定位（不用点）", None)
        self.face_combo.setCurrentIndex(self.face_combo.findData(default))
        self.face_combo.setToolTip("这张图是答题卡的哪一面；选“自动定位”则交给程序自己找定位块，这张图不用点")
        self.face_combo.currentIndexChanged.connect(lambda _i: self.changed.emit())
        row.addWidget(self.face_combo)
        row.addStretch(1)
        name = QLabel(path.name)
        name.setObjectName("muted")
        row.addWidget(name)
        row.addSpacing(8)
        self.fit_btn = QPushButton("看全图")
        self.fit_btn.setToolTip("把整张图缩放到窗口大小；拖动平移、滚轮缩放后用它复位")
        row.addWidget(self.fit_btn)
        lay.addLayout(row)
        self.view = ClickView()
        self.fit_btn.clicked.connect(self.view.show_all)
        if self.gray is not None:
            self.view.set_array(self.gray)
        lay.addWidget(self.view, 1)

    def face(self) -> str | None:
        return self.face_combo.currentData()


class ManualAlignDialog(QDialog):
    """返回 manual 参数：{面: {"image": 原图序号, "points": [[x, y] | None, ...]}}。"""

    def __init__(self, seq: int, template: Template, bases: dict[str, np.ndarray], raw_paths: list[Path],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"手动定位 #{seq:04d}")
        self.resize(1180, 780)
        self.tpl = template
        self.bases = bases
        faces = list(template.faces)
        # 每面的点击结果：参照点序号 -> 像素坐标 / None（跳过）；未出现的序号表示还没处理
        self.points: dict[int, dict[str, dict[int, list | None]]] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)
        lead = QLabel("这张卡没能自动定位。请在图上依次单击右侧列出的参照点：点错了可以撤销，"
                      "被遮住或印出纸边的点可以跳过，每面至少点 3 个。拖动平移、滚轮缩放，点“看全图”恢复。")
        lead.setWordWrap(True)
        theme.set_style(lead, "color:$INK2;")
        root.addWidget(lead)

        body = QHBoxLayout()
        body.setSpacing(16)
        self.tabs = QTabWidget()
        self.image_tabs: list[_ImageTab] = []
        for i, path in enumerate(raw_paths):
            tab = _ImageTab(path, faces, faces[i] if i < len(faces) else None)
            tab.view.clicked.connect(lambda x, y, i=i: self._place(i, x, y))
            tab.changed.connect(self._refresh)
            self.image_tabs.append(tab)
            self.points[i] = {f: {} for f in faces}
            self.tabs.addTab(tab, f"第 {i + 1} 张图")
        for i in range(self.tabs.count()):
            self.tabs.setTabToolTip(i, "每张原图一页：分别选它是哪一面，再在图上点参照点")
        self.tabs.currentChanged.connect(lambda _i: self._refresh())
        body.addWidget(self.tabs, 1)

        side = QVBoxLayout()
        side.setSpacing(8)
        self.step_label = QLabel("")
        self.step_label.setWordWrap(True)
        self.step_label.setStyleSheet("font-weight:600;")
        side.addWidget(self.step_label)
        self.preview = CardView(placeholder="自动定位的图不用点")
        self.preview.setFixedHeight(200)
        side.addWidget(self.preview)
        self.ref_list = QListWidget()
        self.ref_list.setToolTip("要点的参照点清单，按顺序点。点错了可以选中那一行，再在图上单击重新放这个点")
        self.ref_list.currentRowChanged.connect(self._select_row)
        side.addWidget(self.ref_list, 1)
        acts = QHBoxLayout()
        self.undo_btn = QPushButton("撤销")
        self.undo_btn.clicked.connect(self._undo)
        self.skip_btn = QPushButton("跳过此点")
        self.skip_btn.clicked.connect(self._skip)
        self.clear_btn = QPushButton("清空")
        self.clear_btn.clicked.connect(self._clear)
        for b in (self.undo_btn, self.skip_btn, self.clear_btn):
            acts.addWidget(b)
        side.addLayout(acts)
        sw = QWidget()
        sw.setFixedWidth(300)
        sw.setLayout(side)
        body.addWidget(sw)
        root.addLayout(body, 1)

        bottom = QHBoxLayout()
        self.status = QLabel("")
        self.status.setObjectName("muted")
        bottom.addWidget(self.status, 1)
        cancel = QPushButton("取消")
        cancel.setToolTip("放弃手动定位，这张卡保持原样，仍在异常列表里")
        cancel.clicked.connect(self.reject)
        bottom.addWidget(cancel)
        self.ok_btn = QPushButton("重新识别")
        self.ok_btn.setObjectName("primary")
        self.ok_btn.clicked.connect(self.accept)
        bottom.addWidget(self.ok_btn)
        root.addLayout(bottom)
        self._history: list[tuple[int, str, int, object]] = []  # 撤销栈：(原图序号, 面, 参照点序号, 修改前的值)
        self._current: dict[tuple[int, str], int] = {}
        self._refresh()

    # ---------- 状态 ----------
    def _tab(self) -> int:
        return self.tabs.currentIndex()

    def _refs(self, face: str) -> list[tuple[str, tuple[float, float]]]:
        return reference_points(self.tpl.faces[face].align)

    def _next_index(self, i: int, face: str) -> int | None:
        done = self.points[i][face]
        return next((k for k in range(len(self._refs(face))) if k not in done), None)

    def current_index(self) -> int | None:
        i = self._tab()
        face = self.image_tabs[i].face() if i >= 0 else None
        if face is None:
            return None
        k = self._current.get((i, face))
        return k if k is not None else self._next_index(i, face)

    def result_value(self) -> dict:
        out = {}
        for i, tab in enumerate(self.image_tabs):
            face = tab.face()
            if face is None:
                continue
            done = self.points[i][face]
            out[face] = {"image": i, "points": [done.get(k) for k in range(len(self._refs(face)))]}
        return out

    def problems(self) -> list[str]:
        faces = [t.face() for t in self.image_tabs if t.face() is not None]
        msgs = []
        if not faces:
            msgs.append("至少要手动定位一张图")
        if len(faces) != len(set(faces)):
            msgs.append("两张图不能选同一面")
        for i, tab in enumerate(self.image_tabs):
            face = tab.face()
            if face is not None and sum(v is not None for v in self.points[i][face].values()) < MIN_POINTS:
                msgs.append(f"第 {i + 1} 张图还需要点满 {MIN_POINTS} 个点")
        return msgs

    # ---------- 操作 ----------
    def _place(self, i: int, x: float, y: float) -> None:
        face = self.image_tabs[i].face()
        if face is None or i != self._tab():
            return
        k = self.current_index()
        if k is None:
            return
        spec = self.tpl.faces[face].align
        tab = self.image_tabs[i]
        if spec.method == "anchors" and tab.gray is not None:
            w, h = spec.anchors[k][2], spec.anchors[k][3]
            px_mm = max(tab.gray.shape) / max(self.tpl.paper)  # 原图含扫描背板，略偏大，吸附容差足够
            x, y = snap_to_blob(tab.gray, x, y, max(w, h) * px_mm)
        self._record(i, face, k, [x, y])

    def _skip(self) -> None:
        i, k = self._tab(), self.current_index()
        face = self.image_tabs[i].face()
        if face is None or k is None:
            return
        self._record(i, face, k, None)

    def _record(self, i: int, face: str, k: int, value: list | None) -> None:
        done = self.points[i][face]
        self._history.append((i, face, k, done.get(k, _UNSET)))
        done[k] = value
        self._current.pop((i, face), None)
        self._refresh()

    def _undo(self) -> None:
        if not self._history:
            return
        i, face, k, before = self._history.pop()
        if before is _UNSET:  # 首次处理的点：撤销后回到未处理，下一次单击重新放它
            self.points[i][face].pop(k, None)
            self._current[(i, face)] = k
        else:  # 重新放置或改成跳过的点：恢复修改前的坐标 / 跳过状态
            self.points[i][face][k] = before
            self._current.pop((i, face), None)
        self.tabs.setCurrentIndex(i)
        self._refresh()

    def _clear(self) -> None:
        i = self._tab()
        face = self.image_tabs[i].face()
        if face is None:
            return
        self.points[i][face].clear()
        self._history = [h for h in self._history if h[:2] != (i, face)]
        self._current.pop((i, face), None)
        self._refresh()

    def _select_row(self, row: int) -> None:
        i = self._tab()
        face = self.image_tabs[i].face() if i >= 0 else None
        if face is None or row < 0 or self._refreshing:
            return
        self._current[(i, face)] = row  # 选中某一行后，下一次单击重新放这个点
        self._refresh()

    _refreshing = False

    def _refresh(self) -> None:
        self._refreshing = True
        try:
            i = self._tab()
            tab = self.image_tabs[i]
            face = tab.face()
            k = self.current_index()
            self.ref_list.clear()
            if face is None:
                self.step_label.setText("这张图交给程序自动定位，不需要点。")
                self.preview.clear()
                tab.view.set_marks([], -1)
            else:
                refs = self._refs(face)
                done = self.points[i][face]
                for n, (label, _mm) in enumerate(refs):
                    state = ("已点" if done[n] is not None else "已跳过") if n in done else ""
                    item = QListWidgetItem(f"{n + 1}. {label}" + (f"　{state}" if state else ""))
                    if n in done:  # 清单每次刷新都重建，直接按当前主题着色
                        item.setForeground(theme.qcolor("SUCCESS" if done[n] is not None else "INK3"))
                    self.ref_list.addItem(item)
                if k is not None:
                    self.ref_list.setCurrentRow(k)
                    self.step_label.setText(f"请单击：{FACE_TEXT.get(face, face)} {refs[k][0]}（右图红框处）")
                else:
                    self.step_label.setText(f"{FACE_TEXT.get(face, face)}的参照点都处理完了。")
                self._show_preview(face, k)
                tab.view.set_marks([done.get(n) for n in range(len(refs))], -1 if k is None else k)
            has_points = face is not None and bool(self.points[i][face])
            self.undo_btn.setEnabled(bool(self._history))
            self.skip_btn.setEnabled(face is not None and k is not None)
            self.clear_btn.setEnabled(has_points)
            msgs = self.problems()
            self.status.setText("；".join(msgs) if msgs else "可以重新识别了")
            self.ok_btn.setEnabled(not msgs)
            self._refresh_tips(face, k, msgs)
        finally:
            self._refreshing = False

    def _refresh_tips(self, face: str | None, k: int | None, msgs: list[str]) -> None:
        """按钮提示：做什么；不能点时说明原因。"""
        def tip(desc: str, reason: str | None) -> str:
            return f"{desc}\n现在不能点：{reason}" if reason else desc

        self.undo_btn.setToolTip(tip("撤销上一次放点或跳过", None if self.undo_btn.isEnabled() else "还没有可撤销的操作"))
        skip_reason = None
        if face is None:
            skip_reason = "这张图选了自动定位，不用点"
        elif k is None:
            skip_reason = "这一面的点都处理完了；要改某个点，先在右侧清单里选中它"
        self.skip_btn.setToolTip(tip(f"这个点被遮住、污损或不在图里时跳过，接着点下一个；每面至少要点到 {MIN_POINTS} 个",
                                     skip_reason))
        self.clear_btn.setToolTip(tip("清掉这张图上已点的所有点，从第 1 个重新点",
                                      None if self.clear_btn.isEnabled() else "这张图还没点任何点"))
        self.ok_btn.setToolTip(tip("按你点的位置重新识别这张卡", "；".join(msgs) if msgs else None))

    def _show_preview(self, face: str, current: int | None) -> None:
        base = self.bases.get(face)
        if base is None or not self.preview.set_array(base):
            return
        s = px_per_mm(self.tpl.dpi)
        r = 6 * s
        overlays = []
        for n, (_label, (x, y)) in enumerate(self._refs(face)):
            color = theme.PAPER_ERR if n == current else theme.REGION["info"]
            overlays.append(Overlay(QRectF(x * s - r, y * s - r, 2 * r, 2 * r), color,
                                    fill_alpha=90 if n == current else 20, width=3 if n == current else 1))
        self.preview.set_overlays(overlays)
