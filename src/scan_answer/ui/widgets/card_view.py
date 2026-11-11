"""卡面查看：整页图 + 识别叠层（彩色框），可缩放；窗口变化时自动适配。"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsPixmapItem, QGraphicsRectItem, QGraphicsScene, QGraphicsSimpleTextItem, QGraphicsView

from .. import theme
from ..background import Latest


@dataclass
class Overlay:
    rect: QRectF  # 图像像素坐标
    color: str
    fill_alpha: int = 40
    label: str = ""
    width: float = 2.0


_decoding: dict[tuple, Future] = {}  # 正在解码的文件（路径、修改时间、大小） → 解码结果
_decoding_lock = threading.Lock()


def _decode(path: str, key: tuple | None) -> QImage:
    """在后台线程解码图片文件。同一文件正在被解码时（如卡面与证据预览同时加载同一张整页图）等它的结果，不重复解码。"""
    if key is None:
        return QImage(path)
    with _decoding_lock:
        pending = _decoding.get(key)
        owner = pending is None
        if owner:
            pending = _decoding[key] = Future()
    if not owner:
        return pending.result()  # 被等的解码已在执行，不会互相等待
    try:
        image = QImage(path)
        pending.set_result(image)
        return image
    except BaseException as e:
        pending.set_exception(e)
        raise
    finally:
        with _decoding_lock:
            _decoding.pop(key, None)


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
        self._source: tuple | None = None  # 当前图片来自的文件（路径、修改时间、大小），供其他视图共用
        self._loader = Latest(self)  # 后台解码：连续加载只显示最后一张
        self._loading = False
        self._auto_fit = True

    def refresh_theme(self) -> None:
        self.setBackgroundBrush(QBrush(theme.qcolor("CANVAS")))

    def clear(self) -> None:
        """清空图片，并作废还没显示的后台加载（之后显示的内容以最后一次调用为准）。"""
        self._loader.cancel()
        self._loading = False
        self.scene().clear()
        self._pix = None
        self._source = None
        self.viewport().update()

    @staticmethod
    def _file_key(path: Path) -> tuple | None:
        try:
            st = path.stat()
            return (str(path.resolve()), st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def set_image(self, path: str | Path | None, share: CardView | None = None) -> bool:
        """显示图片文件。share 已显示同一文件（路径、修改时间、大小都相同）时直接共用它解码好的图，不再解码一次。"""
        self.clear()
        if not path or not Path(path).exists():
            return False
        key = self._file_key(Path(path))
        if share is not None and share._pix is not None and key is not None and share._source == key:
            pm = share._pix.pixmap()  # QPixmap 隐式共享：不复制像素，两边都不修改图片
        else:
            pm = QPixmap(str(path))
        if pm.isNull():
            return False
        self._set_pixmap(pm)
        self._source = key
        return True

    def load_image(self, path: str | Path | None, done: Callable[[bool], None] | None = None,
                   share: CardView | None = None) -> None:
        """在后台解码图片文件，解码完在界面线程显示，期间显示“加载中…”。

        连续调用（如快速翻学生）只显示最后一次；done(是否显示成功) 在显示后于界面线程调用，
        被后来的调用作废的加载不会回调。share 已显示同一文件时直接共用，不再解码。"""
        self.clear()
        finish = done or (lambda _ok: None)
        if not path or not Path(path).exists():
            finish(False)
            return
        key = self._file_key(Path(path))
        if share is not None and share._pix is not None and key is not None and share._source == key:
            self._set_pixmap(share._pix.pixmap())  # QPixmap 隐式共享：不复制像素，两边都不修改图片
            self._source = key
            finish(True)
            return
        self._loading = True
        self.viewport().update()

        def shown(image: QImage) -> None:
            self._loading = False
            if image.isNull():
                self.viewport().update()
                finish(False)
                return
            self._set_pixmap(QPixmap.fromImage(image))  # QPixmap 只能在界面线程创建
            self._source = key
            finish(True)

        def failed(_e: BaseException) -> None:
            self._loading = False
            self.viewport().update()
            finish(False)

        self._loader.submit(lambda: _decode(str(path), key), shown, failed)

    def loading(self) -> bool:
        return self._loading

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
        text = "加载中…" if self._loading else self.placeholder
        if self._pix is None and text:  # 没有图时给出说明，而不是一块空底色
            p = QPainter(self.viewport())
            p.setPen(theme.qcolor("INK3"))
            p.setFont(theme.ui_font(13))
            p.drawText(self.viewport().rect().adjusted(16, 16, -16, -16),
                       Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, text)
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
