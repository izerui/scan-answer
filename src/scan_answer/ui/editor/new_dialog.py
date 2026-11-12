"""新建模板对话框：模板名、正面文件、背面文件（可选）；导入后预览纸张尺寸、纠偏角度、定位方式。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QApplication, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ...template.auto import ImportedBase, import_base, load_pages
from ...template.doc import TemplateDoc, initial_alignment
from .. import theme
from ..widgets.common import form_layout

FILE_FILTER = "答题卡底图 (*.pdf *.png *.jpg *.jpeg *.tif *.tiff *.bmp)"
METHOD_TEXT = {"anchors": "定位块", "frames": "定位外框", "none": "未检测到（需在编辑器中手动画定位块或外框）"}
FACE_TEXT = {"front": "正面", "back": "背面"}


def import_files(front: str, back: str = "") -> tuple[dict[str, ImportedBase], list[str]]:
    """读取正面（和背面）文件，返回 ({面: 底图}, 提示)。

    正面是多页 PDF 且没有指定背面时，第 2 页作为背面，超过 2 页时只用前 2 页；另选了背面文件时正面只用第 1 页。
    预览的定位方式按新建模板时的实际检测（initial_alignment）填写。读取失败抛 ValueError。"""
    notes: list[str] = []
    pages = load_pages(front)
    if not pages:
        raise ValueError(f"无法读取 {Path(front).name}：文件里没有页面")
    raw = {"front": pages[0]}
    if back:
        if len(pages) > 1:
            notes.append(f"正面文件有 {len(pages)} 页，已另选背面文件，正面只用第 1 页")
        bpages = load_pages(back)
        if not bpages:
            raise ValueError(f"无法读取 {Path(back).name}：文件里没有页面")
        if len(bpages) > 1:
            notes.append(f"背面文件有 {len(bpages)} 页，只用第 1 页")
        raw["back"] = bpages[0]
    elif len(pages) >= 2:
        if len(pages) > 2:
            notes.append(f"PDF 有 {len(pages)} 页，只用前 2 页")
        raw["back"] = pages[1]
        notes.append("PDF 第 2 页作为背面")
    faces = {face: import_base(gray, dpi) for face, (gray, dpi) in raw.items()}
    for ib in faces.values():
        align = initial_alignment(ib.image, 300, ib.paper)
        ib.method = align.method if (align.anchors or align.frames) else "none"
    return faces, notes


def describe(faces: dict[str, ImportedBase]) -> list[str]:
    """每面一行预览：纸张尺寸、自动摆正的角度、定位方式。"""
    return [f"{FACE_TEXT.get(f, f)}：纸张 {ib.paper[0]:g} × {ib.paper[1]:g} mm，已自动摆正 {abs(ib.skew_deg):.2f}°，"
            f"定位方式 {METHOD_TEXT.get(ib.method, ib.method)}" for f, ib in faces.items()]


class NewTemplateDialog(QDialog):
    """点“创建”后用 result_value() 取 (模板名, {面: ImportedBase})。读取失败时在对话框内显示错误，不关闭。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("新建模板")
        self.setMinimumWidth(560)
        self.faces: dict[str, ImportedBase] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 20, 20, 16)
        lay.setSpacing(12)
        form = form_layout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：期中考试 A3 答题卡")
        self.name_edit.setToolTip("模板的名字，新建考试时按它选择；不填时用正面文件名，之后也可以在编辑器里改名")
        form.addRow("模板名", self.name_edit)
        self.front_edit = QLineEdit()
        self.front_edit.setPlaceholderText("空白答题卡的扫描图片或 PDF")
        self.front_edit.setToolTip("没填过的空白答题卡正面，扫描图片或 PDF 都行；"
                                   "双面 PDF 可以只选这一个文件，第 2 页自动作为背面")
        form.addRow("正面文件", self._file_row(self.front_edit, "选择空白答题卡正面的图片或 PDF"))
        self.back_edit = QLineEdit()
        self.back_edit.setPlaceholderText("可选；不选时正面多页 PDF 的第 2 页作背面")
        self.back_edit.setToolTip("双面答题卡的背面（可选）。单面卡留空；正面是双面 PDF 时也可以留空")
        form.addRow("背面文件", self._file_row(self.back_edit, "选择空白答题卡背面的图片或 PDF（可选）"))
        lay.addLayout(form)
        self.preview = QLabel("选择文件后显示纸张尺寸、自动摆正的角度和定位方式。")
        self.preview.setObjectName("muted")
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        self.error = QLabel("")
        self.error.setWordWrap(True)
        theme.set_style(self.error, "color:$ERR;")
        lay.addWidget(self.error)
        btns = QHBoxLayout()
        btns.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setToolTip("不新建模板，关闭对话框")
        cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        self.create_btn = QPushButton("创建")
        self.create_btn.setObjectName("primary")
        self.create_btn.setToolTip("读取文件并打开编辑器；之后在编辑器里核对定位块、画区域")
        self.create_btn.clicked.connect(self._create)
        btns.addWidget(self.create_btn)
        lay.addLayout(btns)
        self.front_edit.editingFinished.connect(self.load)
        self.back_edit.editingFinished.connect(self.load)
        self._loaded_key: tuple[str, str] | None = None

    def _file_row(self, edit: QLineEdit, tip: str = "") -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(edit, 1)
        b = QPushButton("浏览…")
        b.setToolTip(tip or "选择文件")
        b.clicked.connect(lambda: self._browse(edit))
        h.addWidget(b)
        return w

    def _browse(self, edit: QLineEdit) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "选择底图文件", "", FILE_FILTER)
        if f:
            edit.setText(f)
            self.load()

    def load(self) -> bool:
        """按当前填写的文件导入底图并刷新预览；失败时显示错误。"""
        front, back = self.front_edit.text().strip(), self.back_edit.text().strip()
        if (front, back) == self._loaded_key and self.faces:
            return True
        self.faces, self._loaded_key = {}, None
        self.error.setText("")
        if not front:
            self.preview.setText("请选择正面文件。")
            return False
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            faces, notes = import_files(front, back)
        except ValueError as e:
            self.error.setText(str(e))
            self.preview.setText("")
            return False
        except Exception as e:  # noqa: BLE001 — 任何读取失败都留在对话框里显示
            self.error.setText(f"无法读取文件：{type(e).__name__}: {e}")
            self.preview.setText("")
            return False
        finally:
            QApplication.restoreOverrideCursor()
        self.faces, self._loaded_key = faces, (front, back)
        if not self.name_edit.text().strip():
            self.name_edit.setText(Path(front).stem)
        self.preview.setText("\n".join(describe(faces) + notes))
        return True

    def _create(self) -> None:
        if not self.load():
            return
        if not self.name_edit.text().strip():
            self.error.setText("请填写模板名。")
            return
        try:
            TemplateDoc.validate_imported_faces(self.faces)
        except ValueError as e:
            self.error.setText(str(e))
            return
        self.accept()

    def result_value(self) -> tuple[str, dict[str, ImportedBase]]:
        return self.name_edit.text().strip(), self.faces
