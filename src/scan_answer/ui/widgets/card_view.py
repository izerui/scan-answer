"""卡面查看：整页图 + 识别叠层（彩色框），可缩放；窗口变化时自动适配。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from .. import theme


@dataclass
class Overlay:
    rect: QRectF  # 图像像素坐标
    color: str
    fill_alpha: int = 40
    label: str = ""
    width: float = 2.0


class CardView(QGraphicsView):
    def __init__(self, parent=None, placeholder: str = "暂无图像"):
        super().__init__(parent)
        self.placeholder = placeholder
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.refresh_theme()
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self._pix: QGraphicsPixmapItem | None = None
        self._auto_fit = True

    def refresh_theme(self) -> None:
        self.setBackgroundBrush(QBrush(theme.qcolor("CANVAS")))

    def clear(self) -> None:
        self.scene().clear()
        self._pix = None

    def set_image(self, path: str | Path | None) -> bool:
        self.clear()
        if not path or not Path(path).exists():
            return False
        pm = QPixmap(str(path))
        if pm.isNull():
            return False
        return self._set_pixmap(pm)

    def set_array(self, image: np.ndarray | None) -> bool:
        """Display an owned copy of a grayscale evidence image."""
        self.clear()
        if image is None or image.ndim != 2 or not image.size:
            return False
        array = np.ascontiguousarray(image, dtype=np.uint8)
        h, w = array.shape
        qimage = QImage(array.data, w, h, array.strides[0], QImage.Format.Format_Grayscale8).copy()
        return self._set_pixmap(QPixmap.fromImage(qimage))

    def _set_pixmap(self, pm: QPixmap) -> bool:
        self._pix = self.scene().addPixmap(pm)
        self.scene().setSceneRect(QRectF(pm.rect()))
        self._auto_fit = True
        self.fit()
        return True

    def image_size(self) -> tuple[int, int]:
        if not self._pix:
            return (0, 0)
        return (self._pix.pixmap().width(), self._pix.pixmap().height())

    def set_overlays(self, overlays: list[Overlay]) -> None:
        for it in [i for i in self.scene().items() if i is not self._pix]:
            self.scene().removeItem(it)
        for o in overlays:
            item = QGraphicsRectItem(o.rect)
            pen = QPen(theme.qcolor(o.color), o.width)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setBrush(theme.qcolor(o.color, o.fill_alpha))
            self.scene().addItem(item)
            if o.label:
                t = QGraphicsSimpleTextItem(o.label)
                t.setBrush(theme.qcolor("#ffffff"))
                t.setFont(theme.ui_font(max(12, int(o.rect.height() * 0.6)), bold=True))
                bg = QGraphicsRectItem(t.boundingRect().adjusted(-4, -2, 4, 2))
                bg.setBrush(theme.qcolor(o.color))
                bg.setPen(QPen(Qt.PenStyle.NoPen))
                bg.setPos(o.rect.left(), o.rect.top() - t.boundingRect().height() - 4)
                t.setPos(o.rect.left(), o.rect.top() - t.boundingRect().height() - 4)
                self.scene().addItem(bg)
                self.scene().addItem(t)

    def set_placeholder(self, text: str) -> None:
        self.placeholder = text
        self.viewport().update()

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        if self._pix is None and self.placeholder:  # 没有图时给出说明，而不是一块空底色
            p = QPainter(self.viewport())
            p.setPen(theme.qcolor("INK3"))
            p.setFont(theme.ui_font(13))
            p.drawText(self.viewport().rect().adjusted(16, 16, -16, -16),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.placeholder)
            p.end()

    def fit(self) -> None:
        if self._pix:
            self.fitInView(self.scene().sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        if self._auto_fit:
            self.fit()

    def wheelEvent(self, e) -> None:
        self._auto_fit = False
        f = 1.15 if e.angleDelta().y() > 0 else 1 / 1.15
        self.scale(f, f)

    def mouseDoubleClickEvent(self, e) -> None:
        self._auto_fit = True
        self.fit()
