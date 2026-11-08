"""模板页：列表视图（可用模板、新建、编辑、添加目录）+ 编辑视图（区域树、画布、属性面板、试识别）。

编辑视图的约定：
- 修改前保存快照，修改成功后提交撤销点；连续的拖动 / 微调合并成一步。
- 不缓存 doc.template 及其子对象（撤销会替换对象）；增删改、撤销之后从 doc.regions(face) 重建引用。
- 试识别在后台线程执行，结果经 Qt 信号回到界面线程；退出试识别只恢复底图，不改动模板。
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...app.library import list_templates
from ...app.results import face_overlays
from ...app.settings import AppSettings, bundled_templates_dir, save_settings
from ...source.documents import DOCUMENT_FILTER
from ...template.doc import KIND_COLOR, KIND_LABELS, KINDS, RegionRef, TemplateDoc, UndoStack
from ...template.io import template_from_dict
from .. import theme
from ..editor.canvas import TemplateCanvas
from ..editor.new_dialog import NewTemplateDialog, describe, import_files
from ..editor.props import PropsPanel
from ..editor.trial_preview import TrialPreview
from ..editor.tree import RegionTree
from ..widgets.common import Segmented

GRID_KINDS = ("objective", "exam_no", "omr_field")

# 区域工具下拉框：先列常用类型，定位块 / 外框（手动）放最后
TOOL_KINDS = [k for k in KINDS if k not in ("anchor", "frame", "sync")] + ["anchor", "frame"]
TOOL_TEXT = {"anchor": "定位块（手动）", "frame": "定位外框（手动）"}
FACE_TEXT = {"front": "正面", "back": "背面"}
IMAGE_FILTER = DOCUMENT_FILTER
HINT = "方向键微调 0.1 mm（Shift 1 mm）　空格拖动画布　Delete 删除"
# 试识别叠层：face_overlays 的类型 → (色码, 填充透明度)
TRIAL_MARK = {"answer": (theme.REGION["obj"], 90), "bad": (theme.PAPER_ERR, 45), "exam": (theme.REGION["id"], 90),
              "exam_bad": (theme.PAPER_ERR, 45), "omr": (theme.REGION["id"], 90), "choice": (theme.REGION["sub"], 90)}


def _swatch_icon(color: str) -> QIcon:
    pm = QPixmap(10, 10)
    pm.fill(QColor(color))
    return QIcon(pm)


def _vsep() -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.Shape.VLine)
    theme.set_style(f, "color:$LINE;")
    return f


class _TrialSignals(QObject):
    done = Signal(int, object)  # (批号, 结果列表或异常)


class TemplatePage(QWidget):
    changed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.doc: TemplateDoc | None = None
        self.undo_stack: UndoStack | None = None
        self.face = "front"
        self._dirs: list[Path] = []
        self._push_pending = False  # 本轮事件循环已记录撤销点（连续微调合并成一步）
        self._fit_zoom = 1.0
        self._trial = _TrialSignals()
        self._trial.done.connect(self._trial_done)
        self._trial_token = 0
        self._trial_running = False
        self._trial_results: list = []
        self._trial_template = None
        self._trial_current = None
        self._trial_revision: tuple[TemplateDoc, int] | None = None
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.list_view = self._build_list()
        self.editor_view = self._build_editor()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.editor_view)
        self.refresh()

    # ══════════ 列表视图 ══════════
    def _build_list(self) -> QWidget:
        body = QWidget()
        body.setObjectName("work")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 24)
        h = QLabel("模板")
        h.setObjectName("h1")
        lay.addWidget(h)
        note = QLabel("双击模板进入编辑器；新建模板需要空白答题卡的扫描图片或 PDF。"
                      "随程序提供的模板修改后另存到用户模板目录。")
        theme.set_style(note, "color:$INK2;")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["名称", "版本", "纸张（mm）", "面数", "题数", "目录"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setShowGrid(False)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open_row(row))
        lay.addWidget(self.table, 1)
        acts = QHBoxLayout()
        new = QPushButton("新建模板…")
        new.setObjectName("primary")
        new.clicked.connect(self._new_dialog)
        acts.addWidget(new)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda: self._open_row(self.table.currentRow()))
        acts.addWidget(edit)
        add = QPushButton("添加模板目录…")
        add.clicked.connect(self._add)
        acts.addWidget(add)
        acts.addStretch(1)
        lay.addLayout(acts)
        return body

    def refresh(self) -> None:
        ts = list_templates(self.settings.template_dirs)
        self._dirs = [Path(t.directory) for t in ts]
        self.table.setRowCount(len(ts))
        for i, t in enumerate(ts):
            vals = [t.name, str(t.version), f"{t.paper[0]:g} × {t.paper[1]:g}", str(t.faces), str(t.questions),
                    str(t.directory)]
            for j, v in enumerate(vals):
                self.table.setItem(i, j, QTableWidgetItem(v))

    def add_dir(self, d: str) -> None:
        if d not in self.settings.template_dirs:
            self.settings.template_dirs.append(d)
            save_settings(self.settings)
        self.refresh()
        self.changed.emit()

    def _add(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择模板目录（或包含多个模板的上级目录）")
        if d:
            self.add_dir(d)

    def _open_row(self, row: int) -> None:
        if 0 <= row < len(self._dirs):
            self.open_editor(self._dirs[row])

    def _new_dialog(self) -> None:
        dlg = NewTemplateDialog(self)
        if dlg.exec():
            name, faces = dlg.result_value()
            try:
                doc = TemplateDoc.new(name, faces)
            except ValueError as e:
                QMessageBox.warning(self, "无法新建模板", str(e))
                return
            self._enter(doc, "；".join(describe(faces)))

    # ══════════ 编辑视图：布局 ══════════
    def _build_editor(self) -> QWidget:
        view = QWidget()
        root = QVBoxLayout(view)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_bar())
        body = QHBoxLayout()
        body.setSpacing(0)
        root.addLayout(body, 1)

        left = QWidget()
        left.setObjectName("panel")
        left.setFixedWidth(272)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(0)
        head = QWidget()
        head.setObjectName("treeHead")
        theme.set_style(head, "#treeHead { border-bottom: 1px solid $LINE; }")
        hl = QHBoxLayout(head)
        hl.setContentsMargins(16, 10, 16, 10)
        t = QLabel("区域")
        t.setStyleSheet("font-weight:600;")
        hl.addWidget(t)
        hl.addStretch(1)
        self.region_count = QLabel("")
        self.region_count.setObjectName("muted")
        hl.addWidget(self.region_count)
        ll.addWidget(head)
        controls = QWidget()
        cl = QVBoxLayout(controls)
        cl.setContentsMargins(12, 8, 12, 8)
        cl.addWidget(QLabel("当前面定位方式"))
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItem("定位块", "anchors")
        self.alignment_combo.addItem("定位外框", "frames")
        self.alignment_combo.currentIndexChanged.connect(self._on_alignment)
        cl.addWidget(self.alignment_combo)
        cl.addWidget(QLabel("主观题目标"))
        self.subjective_target = QComboBox()
        self.subjective_target.addItem("新建主观题", None)
        self.subjective_target.setMinimumWidth(0)
        self.subjective_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        cl.addWidget(self.subjective_target)
        ll.addWidget(controls)
        self.tree = RegionTree()
        ll.addWidget(self.tree, 1)
        body.addWidget(left)

        self.canvas = TemplateCanvas()
        body.addWidget(self.canvas, 1)

        right = QWidget()
        right.setObjectName("propsSide")
        right.setFixedWidth(336)
        theme.set_style(right, "#propsSide { background: $WORK; border-left: 1px solid $LINE; }")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.props_stack = QStackedWidget()
        self.props = PropsPanel()
        self.props_stack.addWidget(self.props)
        self.props_stack.addWidget(self._build_trial_panel())
        rl.addWidget(self.props_stack)
        body.addWidget(right)

        root.addWidget(self._build_status())

        # 信号
        self.canvas.regionEdited.connect(self._on_region_edited)
        self.canvas.regionDrawn.connect(self._on_region_drawn)
        self.canvas.selectionChanged.connect(self._on_canvas_selection)
        self.canvas.cursorMoved.connect(lambda x, y: self.cursor_label.setText(f"光标 x {x:.1f} mm，y {y:.1f} mm"))
        self.tree.regionSelectionChanged.connect(self._on_tree_selection)
        self.props.paramsChanged.connect(self._on_params)
        self.props.rectChanged.connect(self._on_props_rect)
        self.props.snapRequested.connect(self._on_snap_requested)
        self.props.regionRemoveRequested.connect(lambda ref: self._remove([ref]))
        self.props.regionMoveRequested.connect(self._on_region_move)
        self.canvas.installEventFilter(self)
        self.canvas.viewport().installEventFilter(self)
        self.tree.installEventFilter(self)
        ctx = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for seq, fn in ((QKeySequence.StandardKey.Undo, self.undo), (QKeySequence.StandardKey.Redo, self.redo),
                        (QKeySequence.StandardKey.Save, self.save)):
            sc = QShortcut(QKeySequence(seq), view)
            sc.setContext(ctx)
            sc.activated.connect(fn)
        return view

    def _build_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("editorBar")
        theme.set_style(bar, "#editorBar { background: $HEADER; border-bottom: 1px solid $LINE; } "
                             "#editorBar QPushButton#zoomBtn { padding: 6px 0; }")
        v = QVBoxLayout(bar)
        v.setContentsMargins(16, 8, 16, 8)
        v.setSpacing(8)
        row1 = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setObjectName("h2")
        row1.addWidget(self.title_label)
        row1.addSpacing(10)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        row1.addWidget(self.summary_label)
        row1.addStretch(1)
        self.back_btn = QPushButton("返回列表")
        self.back_btn.clicked.connect(self._back)
        row1.addWidget(self.back_btn)
        self.save_btn = QPushButton("保存模板")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        row1.addWidget(self.save_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        for text, what in (("自动找定位点", "anchors"), ("自动检测外框", "frames"), ("自动识别同步头", "sync")):
            b = QPushButton(text)
            b.clicked.connect(lambda _c=False, w=what: self.auto_align(w))
            row2.addWidget(b)
        row2.addWidget(_vsep())
        self.select_btn = QPushButton("选择")
        self.select_btn.setObjectName("seg")
        self.select_btn.setCheckable(True)
        self.select_btn.setChecked(True)
        self.select_btn.clicked.connect(lambda: self.set_tool(None))
        row2.addWidget(self.select_btn)
        self.tool_combo = QComboBox()
        self.tool_combo.addItem("画框：选择类型", None)
        for k in TOOL_KINDS:
            self.tool_combo.addItem(_swatch_icon(theme.REGION[KIND_COLOR[k]]), TOOL_TEXT.get(k, KIND_LABELS[k]), k)
        self.tool_combo.setIconSize(QSize(10, 10))
        self.tool_combo.setMinimumWidth(200)
        self.tool_combo.currentIndexChanged.connect(lambda i: self.set_tool(self.tool_combo.itemData(i)))
        row2.addWidget(self.tool_combo)
        row2.addWidget(_vsep())
        self.snap_btn = QPushButton("自动吸附")
        self.snap_btn.setToolTip("对选中的网格逐个吸附；没有选中时吸附当前面的全部网格")
        self.snap_btn.clicked.connect(self.auto_snap)
        row2.addWidget(self.snap_btn)
        self.trial_btn = QPushButton("试识别…")
        self.trial_btn.clicked.connect(self._trial_dialog)
        row2.addWidget(self.trial_btn)
        row2.addWidget(_vsep())
        self.undo_btn = QPushButton("撤销")
        self.undo_btn.clicked.connect(self.undo)
        row2.addWidget(self.undo_btn)
        self.redo_btn = QPushButton("重做")
        self.redo_btn.clicked.connect(self.redo)
        row2.addWidget(self.redo_btn)
        row2.addStretch(1)
        self.face_seg = Segmented([("front", "正面"), ("back", "背面")], "front")
        self.face_seg.changed.connect(self.set_face)
        row2.addWidget(self.face_seg)
        row2.addSpacing(10)
        zo = QPushButton("−")
        zo.setObjectName("zoomBtn")
        zo.setFixedWidth(36)
        zo.clicked.connect(lambda: self._zoom_by(1 / 1.25))
        row2.addWidget(zo)
        self.zoom_btn = QPushButton("100%")
        self.zoom_btn.setFlat(True)
        self.zoom_btn.setFixedWidth(64)
        self.zoom_btn.setToolTip("适应窗口")
        self.zoom_btn.clicked.connect(self.fit)
        row2.addWidget(self.zoom_btn)
        zi = QPushButton("+")
        zi.setObjectName("zoomBtn")
        zi.setFixedWidth(36)
        zi.clicked.connect(lambda: self._zoom_by(1.25))
        row2.addWidget(zi)
        v.addLayout(row2)
        return bar

    def _build_trial_panel(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QWidget()
        head.setObjectName("trialHead")
        theme.set_style(head, "#trialHead { border-bottom: 1px solid $LINE; }")
        hl = QVBoxLayout(head)
        hl.setContentsMargins(16, 12, 16, 10)
        t = QLabel("试识别结果")
        t.setObjectName("h2")
        hl.addWidget(t)
        self.trial_head = QLabel("")
        self.trial_head.setObjectName("muted")
        self.trial_head.setWordWrap(True)
        hl.addWidget(self.trial_head)
        lay.addWidget(head)
        self.trial_list = QListWidget()
        self.trial_list.setObjectName("trialList")
        self.trial_list.setWordWrap(True)
        self.trial_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.trial_list.setStyleSheet("QListWidget#trialList { border: none; } "
                                      "QListWidget#trialList::item { padding: 6px 12px; }")
        self.trial_list.currentRowChanged.connect(self._trial_select)
        self.trial_list.setMaximumHeight(180)
        lay.addWidget(self.trial_list)
        self.trial_preview = TrialPreview()
        lay.addWidget(self.trial_preview, 1)
        legend = QLabel("叠层：绿 = 选中的选项　红 = 状态不是正常的题　蓝 = 考号 / 填涂字段　橙 = 选做组")
        legend.setObjectName("muted")
        legend.setWordWrap(True)
        legend.setContentsMargins(16, 8, 16, 8)
        lay.addWidget(legend)
        bl = QHBoxLayout()
        bl.setContentsMargins(16, 8, 16, 12)
        ex = QPushButton("退出试识别")
        ex.clicked.connect(self.exit_trial)
        bl.addWidget(ex)
        bl.addStretch(1)
        lay.addLayout(bl)
        return w

    def _build_status(self) -> QWidget:
        w = QWidget()
        w.setObjectName("editorStatus")
        w.setFixedHeight(30)
        theme.set_style(w, "#editorStatus { background: $HEADER; border-top: 1px solid $LINE; } "
                           "#editorStatus QLabel { color: $INK2; font-size: 12px; }")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(18)
        self.cursor_label = QLabel("光标 —")
        self.align_label = QLabel("")
        self.paper_label = QLabel("")
        self.msg_label = QLabel("")
        self.msg_label.setMinimumWidth(80)
        self.hint_label = QLabel(HINT)
        for lab in (self.cursor_label, self.align_label, self.paper_label):
            lay.addWidget(lab)
        lay.addWidget(self.msg_label, 1)
        lay.addWidget(self.hint_label)
        return w

    # ══════════ 打开 / 新建 / 关闭 ══════════
    def open_editor(self, directory: str | Path) -> bool:
        try:
            doc = TemplateDoc.load(directory)
        except Exception as e:  # noqa: BLE001 — 模板损坏：提示后留在列表
            QMessageBox.warning(self, "无法打开模板", f"{directory}\n{type(e).__name__}: {e}")
            return False
        self._enter(doc, f"已打开 {doc.directory}")
        return True

    def new_template(self, name: str, files: list[str]) -> bool:
        """不经对话框新建模板（files：正面文件，或正面 + 背面文件）。"""
        try:
            faces, notes = import_files(files[0], files[1] if len(files) > 1 else "")
            doc = TemplateDoc.new(name, faces)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "无法新建模板", str(e))
            return False
        self._enter(doc, "；".join(describe(faces) + notes))
        return True

    def _enter(self, doc: TemplateDoc, msg: str = "") -> None:
        self.exit_trial()
        self.doc = doc
        self.subjective_target.setCurrentIndex(0)
        self.undo_stack = UndoStack(doc)
        self._push_pending = False
        faces = list(doc.template.faces)
        self.face = faces[0] if faces else "front"
        for k, b in self.face_seg.buttons.items():
            b.setEnabled(k in faces)
        self.face_seg.set_value(self.face)
        self.set_tool(None)
        self.canvas.set_background(None)
        self.canvas.set_doc(doc, self.face)
        self.tree.set_doc(doc, self.face)
        self.props.set_target(doc, None)
        self.stack.setCurrentWidget(self.editor_view)
        self.fit()
        QTimer.singleShot(0, self.fit)  # 首次显示后视口尺寸才确定
        self.msg_label.setText(msg)
        self._update_header()
        self.canvas.setFocus()

    def close_editor(self) -> None:
        """丢弃当前文档（不询问），回到列表。"""
        self.exit_trial()
        self.doc = None
        self.undo_stack = None
        self.stack.setCurrentWidget(self.list_view)
        self.refresh()

    def can_leave(self) -> bool:
        if self.doc is None or not self.doc.dirty:
            return True
        if QMessageBox.question(self, "模板有未保存的修改", "放弃修改并离开？") != QMessageBox.StandardButton.Yes:
            return False
        self.close_editor()
        return True

    def _back(self) -> None:
        if self.can_leave():
            self.close_editor()

    # ══════════ 保存 ══════════
    def save(self) -> bool:
        if self.doc is None:
            return False
        old = self.doc.directory
        try:
            target = self.doc.save()
        except ValueError as e:
            QMessageBox.warning(self, "模板还不能保存", str(e))
            return False
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        self.msg_label.setText(f"已保存到 {target}（版本 {self.doc.template.version}）")
        self._update_header()
        self.refresh()
        self.changed.emit()
        if old is not None and Path(old).resolve() != Path(target).resolve():
            try:
                bundled = Path(old).resolve().is_relative_to(bundled_templates_dir().resolve())
            except (OSError, ValueError):
                bundled = False
            reason = "随程序提供的模板不能直接修改" if bundled else "原模板目录不可写"
            QMessageBox.information(self, "已另存到用户模板目录",
                                    f"{reason}，已另存到：\n{target}\n之后的修改都保存在这个目录。")
        return True

    # ══════════ 编辑操作 ══════════
    def _mutate(self, label: str, operation, coalesce: bool = False):
        """Only successful content changes commit an undo entry or discard redo."""
        before, revision = self.doc.snapshot(), self.doc.revision
        result = operation()
        if self.doc.revision != revision and not (coalesce and self._push_pending):
            self.undo_stack.push(label, snapshot=before)
            if coalesce:
                self._push_pending = True
                QTimer.singleShot(0, self._clear_push)
        return result

    def _clear_push(self) -> None:
        self._push_pending = False

    def _valid(self, refs: list[RegionRef]) -> list[RegionRef]:
        alive = set(self.doc.regions(self.face))
        return [r for r in refs if r in alive]

    def _after_change(self, select: list[RegionRef] | None = None, props: bool = True) -> None:
        """doc 改动后同步画布、树、属性面板、标题和状态行。"""
        if self._trial_revision is not None and self._trial_revision != (self.doc, self.doc.revision):
            self.exit_trial()
            self.msg_label.setText("模板已修改，原试识别结果已失效")
        sel = self._valid(select if select is not None else self.canvas.selected())
        self.canvas.refresh()
        self.canvas.select(sel)
        self.tree.refresh()
        self.tree.select(sel)
        if props:
            self._sync_props()
        self._update_header()

    def _sync_props(self) -> None:
        sel = self.canvas.selected()
        target = sel[0] if len(sel) == 1 else None
        fw = QApplication.focusWidget()
        # 正在属性面板的数值框里输入时不重建（重建会丢失焦点和输入）
        if (target is not None and target == self.props.target() and isinstance(fw, QAbstractSpinBox)
                and self.props.isAncestorOf(fw)):
            return
        self.props.set_target(self.doc, target)

    def _on_region_edited(self, ref: RegionRef, rect) -> None:
        if self.doc is None:
            return
        self._mutate("移动区域", lambda: self.doc.set_rect(ref, tuple(rect)), coalesce=True)
        self._after_change()

    def _on_props_rect(self, ref: RegionRef, rect) -> None:
        if self.doc is None:
            return
        self._mutate("修改位置", lambda: self.doc.set_rect(ref, tuple(rect)), coalesce=True)
        self._after_change(props=False)

    def _on_params(self, ref: RegionRef, p: dict) -> None:
        if self.doc is None:
            return
        target_key = self.subjective_target.currentData()
        old_key = self.doc.template.subjective[ref.index].key if ref.kind == "subjective" else None
        try:
            self._mutate("修改参数", lambda: self.doc.set_params(ref, p), coalesce=True)
        except (ValueError, IndexError, KeyError) as e:
            self.msg_label.setText(f"参数无效：{e}")
            self.props.set_target(self.doc, ref)
            return
        if old_key is not None and target_key == old_key:
            self.subjective_target.setItemData(
                self.subjective_target.currentIndex(), self.doc.template.subjective[ref.index].key)
        self._after_change()

    def _on_region_drawn(self, kind: str, rect) -> None:
        if self.doc is None:
            return
        rect = tuple(rect)
        target_key = self.subjective_target.currentData()
        index = next((i for i, s in enumerate(self.doc.template.subjective) if s.key == target_key), None)
        try:
            if kind == "subjective" and index is not None:
                ref = self._mutate("追加主观题区域",
                                   lambda: self.doc.add_subjective_region(index, self.face, rect))
                self.msg_label.setText(f"已给第 {target_key} 题追加区域")
            else:
                ref = self._mutate(f"添加{KIND_LABELS.get(kind, kind)}",
                                   lambda: self.doc.add(kind, self.face, rect))
                self.msg_label.setText(f"已添加{KIND_LABELS.get(kind, kind)}：{self.doc.title(ref)}")
        except (ValueError, IndexError, KeyError) as e:
            self.msg_label.setText(f"无法添加：{e}")
            self.set_tool(None)
            return
        self.set_tool(None)
        self._after_change(select=[ref])

    def _on_alignment(self, *_args) -> None:
        if self.doc is None:
            return
        method = self.alignment_combo.currentData()
        if method == self.doc.template.faces[self.face].align.method:
            return
        self._mutate("切换定位方式", lambda: self.doc.set_alignment_method(self.face, method))
        self._after_change(select=[])

    def _on_region_move(self, ref: RegionRef, destination: int) -> None:
        if self.doc is None:
            return
        moved = self._mutate("调整主观题区域顺序",
                             lambda: self.doc.move_subjective_region(ref.index, ref.sub, destination))
        self.set_face(self.doc.face_of(moved))
        self._after_change(select=[moved])

    def _remove(self, refs: list[RegionRef]) -> None:
        if self.doc is None or not refs:
            return
        try:
            self._mutate("删除区域", lambda: self.doc.remove(refs))
        except ValueError as e:
            self.msg_label.setText(f"不能删除：{e}")
            return
        self.msg_label.setText(f"已删除 {len(refs)} 个区域")
        self._after_change(select=[])

    def delete_selected(self) -> None:
        self._remove(self.canvas.selected())

    def _on_snap_requested(self, ref: RegionRef) -> None:
        if self.doc is None:
            return
        res = self.doc.snap(ref, apply=False)  # 失败时文档不变，也不留撤销点
        if res.ok:
            self._mutate("吸附", lambda: self.doc.apply_snap(ref, res))
        self.msg_label.setText(f"{self.doc.title(ref)}：{res.message}")
        self._after_change(props=False)
        self.props.set_snap_result(res.message, res.ok)

    def auto_snap(self) -> None:
        """对选中的网格逐个吸附；没有选中时吸附当前面的全部网格。"""
        if self.doc is None:
            return
        refs = [r for r in self.canvas.selected() if r.kind in GRID_KINDS]
        if not refs:
            refs = [r for r in self.doc.regions(self.face) if r.kind in GRID_KINDS]
        if not refs:
            self.msg_label.setText("当前面没有可吸附的网格（客观题、考号、填涂字段）")
            return
        results = [(r, self.doc.snap(r, apply=False)) for r in refs]
        if any(res.ok for _, res in results):  # 只在有改动时记撤销点
            def apply_all() -> None:
                for r, res in results:
                    self.doc.apply_snap(r, res)
            self._mutate("自动吸附", apply_all)
        msgs = [f"{self.doc.title(r)}：{res.message}" for r, res in results]
        last = results[-1][1]
        self.msg_label.setText("；".join(msgs))
        self.msg_label.setToolTip("\n".join(msgs))
        self._after_change()
        if len(refs) == 1 and self.canvas.selected() == refs:
            self.props.set_snap_result(last.message, last.ok)

    def auto_align(self, what: str) -> None:
        if self.doc is None:
            return
        label = {"anchors": "自动找定位点", "frames": "自动检测外框", "sync": "自动识别同步头"}[what]
        self.msg_label.setText(self._mutate(label, lambda: self.doc.auto_align(self.face, what)))
        self._after_change(select=[])

    def undo(self) -> None:
        self._navigate_history(redo=False)

    def redo(self) -> None:
        self._navigate_history(redo=True)

    def _navigate_history(self, *, redo: bool) -> None:
        self._push_pending = False
        if self.doc is None or self.undo_stack is None:
            return
        questions = self.doc.template.subjective
        key = self.subjective_target.currentData()
        index = next((i for i, q in enumerate(questions) if q.key == key), None)
        count = len(questions)
        changed = self.undo_stack.redo() if redo else self.undo_stack.undo()
        if changed:
            questions = self.doc.template.subjective
            # A rename preserves list identity; additions/deletions must not retarget by index.
            if (index is not None and count == len(questions)
                    and not any(q.key == key for q in questions)):
                self.subjective_target.setItemData(self.subjective_target.currentIndex(), questions[index].key)
            self.msg_label.setText("已重做" if redo else "已撤销")
            self._after_change()

    def set_tool(self, kind: str | None) -> None:
        self.canvas.set_tool(kind)
        self.select_btn.setChecked(kind is None)
        if kind is None:
            self.tool_combo.blockSignals(True)
            self.tool_combo.setCurrentIndex(0)
            self.tool_combo.blockSignals(False)
            self.hint_label.setText(HINT)
        else:
            self.hint_label.setText(f"在画布上拖出“{TOOL_TEXT.get(kind, KIND_LABELS[kind])}”区域；Esc 取消")

    def set_face(self, face: str) -> None:
        if self.doc is None or face not in self.doc.template.faces:
            self.face_seg.set_value(self.face)
            return
        self.face = face
        self.face_seg.set_value(face)
        cur = self._trial_current
        self.canvas.set_background(cur.warped.get(face) if cur is not None else None)
        self.canvas.set_doc(self.doc, face)
        self.tree.set_doc(self.doc, face)
        self.props.set_target(self.doc, None)
        self._apply_trial_marks()
        self._update_header()

    # ---------- 选中同步 ----------
    def _on_canvas_selection(self, refs: list) -> None:
        if self.doc is not None and len(refs) == 1 and refs[0].kind == "subjective":
            key = self.doc.template.subjective[refs[0].index].key
            self._refresh_edit_controls(key)
        self.tree.select(refs)
        self._sync_props()

    def _on_tree_selection(self, refs: list) -> None:
        self.canvas.select(refs)

    def select_refs(self, refs: list[RegionRef]) -> None:
        """程序化选中（画布选中变化时会经信号同步树和属性面板）。"""
        if self.canvas.selected() == list(refs):
            self._on_canvas_selection(list(refs))
        else:
            self.canvas.select(refs)

    # ---------- 缩放 ----------
    def fit(self) -> None:
        self.canvas.fit()
        self._fit_zoom = self.canvas.zoom() or 1.0
        self._update_zoom_label()

    def _zoom_by(self, f: float) -> None:
        self.canvas.set_zoom(self.canvas.zoom() * f)
        self._update_zoom_label()

    def _update_zoom_label(self) -> None:
        self.zoom_btn.setText(f"{round(self.canvas.zoom() / self._fit_zoom * 100)}%")

    def eventFilter(self, obj, e) -> bool:
        if e.type() == QEvent.Type.KeyPress and obj in (self.canvas, self.tree):
            if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
                self.delete_selected()
                return True
            if e.key() == Qt.Key.Key_Escape and obj is self.canvas:
                self.set_tool(None)
                return True
        elif e.type() == QEvent.Type.Wheel and obj is self.canvas.viewport():
            QTimer.singleShot(0, self._update_zoom_label)
        return super().eventFilter(obj, e)

    # ---------- 标题与状态行 ----------
    def _refresh_edit_controls(self, target_key: str | None = None) -> None:
        if self.doc is None:
            return
        if target_key is None:
            target_key = self.subjective_target.currentData()
        self.subjective_target.blockSignals(True)
        self.subjective_target.clear()
        self.subjective_target.addItem("新建主观题", None)
        for question in self.doc.template.subjective:
            self.subjective_target.addItem(f"追加至 {question.key}", question.key)
        self.subjective_target.setCurrentIndex(max(0, self.subjective_target.findData(target_key)))
        self.subjective_target.blockSignals(False)
        self.alignment_combo.blockSignals(True)
        self.alignment_combo.setCurrentIndex(
            self.alignment_combo.findData(self.doc.template.faces[self.face].align.method))
        self.alignment_combo.blockSignals(False)

    def _update_header(self) -> None:
        d = self.doc
        if d is None:
            return
        self._refresh_edit_controls()
        t = d.template
        self.title_label.setText(t.name + ("  ●" if d.dirty else ""))
        n = len(t.faces)
        self.summary_label.setText(f"{t.paper[0]:g} × {t.paper[1]:g} mm，{'双面' if n >= 2 else '单面'}，"
                                   f"版本 {t.version}" + ("，未保存" if d.dirty else ""))
        self.undo_btn.setEnabled(bool(self.undo_stack and self.undo_stack.can_undo()))
        self.redo_btn.setEnabled(bool(self.undo_stack and self.undo_stack.can_redo()))
        self.region_count.setText(f"{len(d.regions(self.face))} 个区域")
        al = t.faces[self.face].align if self.face in t.faces else None
        if al is None:
            self.align_label.setText("")
        elif al.method == "frames":
            self.align_label.setText(f"{FACE_TEXT.get(self.face, self.face)}外框 {len(al.frames)}")
        else:
            self.align_label.setText(f"{FACE_TEXT.get(self.face, self.face)}定位点 {len(al.anchors)} / 4")
        n_sync = sum(len(s.marks) for s in t.sync if s.face == self.face)
        if n_sync and al is not None:
            self.align_label.setText(self.align_label.text() + f"，同步头 {n_sync}")
        self.paper_label.setText(f"纸张 {t.paper[0]:g} × {t.paper[1]:g} mm")

    # ══════════ 试识别 ══════════
    def _trial_dialog(self) -> None:
        if self.doc is None:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择样卡图片或 PDF（建议 3–5 张卡）", "", IMAGE_FILTER)
        if files:
            self.run_trial(files)

    def trial_running(self) -> bool:
        return self._trial_running

    def run_trial(self, files: list[str]) -> None:
        """按扫描时的规则配对后，在后台线程试识别；结果显示在右侧列表。"""
        if self.doc is None or not files:
            return
        from ...app.trial import trial_documents
        t = template_from_dict(self.doc.snapshot())  # 快照：试识别期间继续编辑不影响
        bases = dict(self.doc.bases)
        self.exit_trial()
        self._trial_token += 1
        token = self._trial_token
        self._trial_running = True
        self._trial_template = t
        self._trial_revision = (self.doc, self.doc.revision)
        self.props_stack.setCurrentIndex(1)
        self.trial_head.setText(f"试识别中…（{len(files)} 个文件）")
        self.trial_btn.setEnabled(False)

        def run() -> None:
            try:
                res: object = trial_documents(t, bases, files)
            except Exception as e:  # noqa: BLE001 — 任何失败都显示给用户
                res = e
            self._trial.done.emit(token, res)

        threading.Thread(target=run, daemon=True, name="trial").start()

    def _trial_done(self, token: int, res: object) -> None:
        if token != self._trial_token:
            return  # 已退出或重新开始
        if self.doc is None or self._trial_revision != (self.doc, self.doc.revision):
            self.exit_trial()
            return
        self._trial_running = False
        self.trial_btn.setEnabled(True)
        if isinstance(res, Exception):
            self.trial_head.setText(f"试识别失败：{type(res).__name__}: {res}")
            return
        self._trial_results = list(res)
        n_ok = sum(1 for s in self._trial_results if s.ok)
        self.trial_head.setText(f"处理完成 {n_ok} / {len(self._trial_results)} 张，待核对")
        self.trial_list.blockSignals(True)
        self.trial_list.clear()
        for i, s in enumerate(self._trial_results, start=1):
            names = " + ".join(Path(p).name for p in s.sources)
            if s.ok:
                exam = (s.decision.exam_no or {}).get("value") if s.decision else None
                n_warn = len(s.decision.warnings) if s.decision else 0
                text = f"{i}. {names}\n处理完成，待核对"
                if exam:
                    text += f"\n考号 {exam}"
                if n_warn:
                    text += f"\n警告 {n_warn}"
            else:
                code, _, msg = s.message.partition(": ")
                reason = f"{msg}（{code}）" if msg and code.isupper() else s.message
                text = f"{i}. {names}\n失败：{reason}"
            it = QListWidgetItem(text)
            it.setToolTip(text)
            if not s.ok:
                theme.set_item_colors(it, foreground="ERR")
            self.trial_list.addItem(it)
        self.trial_list.blockSignals(False)
        if self._trial_results:
            self.trial_list.setCurrentRow(0)

    def _trial_select(self, row: int) -> None:
        s = self._trial_results[row] if 0 <= row < len(self._trial_results) else None
        self._trial_current = s if s is not None and s.ok else None
        cur = self._trial_current
        self.trial_preview.set_sheet(cur)
        self.canvas.set_background(cur.warped.get(self.face) if cur is not None else None)
        self._apply_trial_marks()
        if s is not None and not s.ok:
            self.msg_label.setText(f"第 {row + 1} 张识别失败：{s.message}")

    def _apply_trial_marks(self) -> None:
        cur = self._trial_current
        if cur is None or cur.decision is None or self._trial_template is None:
            self.canvas.set_marks([])
            return
        d = cur.decision
        result = {"objective": d.objective, "choose_one": d.choose_one,
                  "student": {"exam_no": d.exam_no, "omr_fields": d.omr_fields}}
        self.canvas.set_marks([(o.rect, *TRIAL_MARK[o.kind])
                               for o in face_overlays(self._trial_template, result, self.face)
                               if o.kind in TRIAL_MARK])

    def exit_trial(self) -> None:
        """退出试识别：恢复底图、清空叠层；不改动模板。"""
        self._trial_token += 1  # 丢弃尚未返回的结果
        self._trial_running = False
        self._trial_results = []
        self._trial_current = None
        self._trial_template = None
        self._trial_revision = None
        self.trial_preview.set_sheet(None)
        self.trial_btn.setEnabled(True)
        self.trial_list.blockSignals(True)
        self.trial_list.clear()
        self.trial_list.blockSignals(False)
        self.canvas.set_background(None)
        self.canvas.set_marks([])
        self.props_stack.setCurrentIndex(0)
