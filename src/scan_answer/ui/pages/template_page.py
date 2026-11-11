"""模板页：列表视图（可用模板、新建、编辑、添加目录）+ 编辑视图（区域树、画布、属性面板、试识别）。

编辑视图的约定：
- 修改前保存快照，修改成功后提交撤销点；同一次拖动合并成一步，连续的方向键微调（1 秒内、同一选中集）合并成一步。
- 不缓存 doc.template 及其子对象（撤销会替换对象）；增删改、撤销之后从 doc.regions(face) 重建引用。
- 试识别在后台线程执行，结果经 Qt 信号回到界面线程；退出试识别只恢复底图，不改动模板。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QMenu, QListWidget, QListWidgetItem, QMessageBox,
                               QPushButton, QStackedWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from ...app.library import list_templates, user_templates_dir
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
from ..widgets.common import ElidedLabel, Segmented, set_empty_text, setup_table
from ..widgets.guide import HelpButton

GRID_KINDS = ("objective", "exam_no", "omr_field")

# 区域工具下拉框：先列常用类型，定位块 / 外框（手动）放最后
TOOL_KINDS = [k for k in KINDS if k not in ("anchor", "frame", "sync")] + ["anchor", "frame"]
TOOL_TEXT = {"anchor": "定位块（手动）", "frame": "定位外框（手动）"}
# 区域工具下拉框每一项的悬停说明
TOOL_TIPS = {
    None: "先选一种区域类型，再在画布上拖出框；画完自动回到“选择”",
    "exam_no": "考号：框住准考证号填涂区（只能有一个，再画会替换）",
    "omr_field": "填涂字段：考号以外要识别的填涂，如科目、卷别",
    "barcode": "条码：框住考生条码或试卷条码",
    "absent": "缺考标记：框住监考涂的缺考框（只能有一个）",
    "field": "信息栏：姓名、考场等手写栏，只切图不识别",
    "objective": "客观题：框住一块选择题填涂区，框松一点没关系，画完点“自动吸附”对齐",
    "subjective": "主观题：框住手写作答区；追加到哪道题看左侧“主观题目标”",
    "choose_one": "选做组：几道题选做一道时，框住考生涂选做题号的地方",
    "ignore": "忽略区：框住大块灰底、贴条码处等会干扰程序的地方，自动检测和对齐时不看",
    "identity": "标识区：框住印着科目名的地方（建议两处），用来发现拿错科目的卡",
    "anchor": "定位块（手动）：自动找不到时，框住卡面角上的黑色定位块",
    "frame": "定位外框（手动）：自动检测不到时，框住印刷的外框",
}
FACE_TIPS = {"front": "切换到正面编辑", "back": "切换到背面编辑"}
NO_BACK_TIP = "单面模板，没有背面"
FACE_TEXT = {"front": "正面", "back": "背面"}
IMAGE_FILTER = DOCUMENT_FILTER
HINT = "方向键微调 0.1 mm（Shift 1 mm）　空格拖动画布　Delete 删除"
GRID_HINT = "可点“自动吸附”把网格对齐到印刷的选项框"
TRIAL_ADVICE = "建议先用填好的样卡“试识别…”核对结果"
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


def _same_dir(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a) == str(b)


class TemplatePage(QWidget):
    changed = Signal()
    NUDGE_MERGE_SECONDS = 1.0  # 这段时间内同一选中集的连续方向键微调合并成一步撤销

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self.doc: TemplateDoc | None = None
        self.undo_stack: UndoStack | None = None
        self.face = "front"
        self._dirs: list[Path] = []
        self._push_pending = False  # 本轮事件循环已记录撤销点（同一次拖动合并成一步）
        self._merge_key: tuple | None = None  # 上一个可合并修改的标识（标签 + 选中集）
        self._merge_until: float | None = None  # 方向键微调的合并截止时间（monotonic）
        self._target_auto = False  # 主观题目标是因选中某题而自动设置的（取消选中时恢复为新建）
        self._fit_zoom = 1.0
        self._trial = _TrialSignals()
        self._trial.done.connect(self._trial_done)
        self._trial_token = 0
        self._trial_running = False
        self._trial_results: list = []
        self._trial_template = None
        self._trial_current = None
        self._trial_revision: tuple[TemplateDoc, int] | None = None
        self._tried_revision: tuple[TemplateDoc, int] | None = None  # 最近一次试识别完成时的文档版本
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)
        self.list_view = self._build_list()
        self.editor_view = self._build_editor()
        self.stack.addWidget(self.list_view)
        self.stack.addWidget(self.editor_view)
        self.refresh()

    def refresh_theme(self) -> None:
        """Recolor existing trial feedback without changing the active result."""
        for index, result in enumerate(self._trial_results):
            item = self.trial_list.item(index)
            if item is not None and not result.ok:
                item.setForeground(theme.qcolor(theme.ERR))

    # ══════════ 列表视图 ══════════
    def _build_list(self) -> QWidget:
        view = QWidget()
        outer = QVBoxLayout(view)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QWidget()
        body.setObjectName("work")
        outer.addWidget(body, 1)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 24)
        h = QLabel("模板")
        h.setObjectName("h1")
        lay.addWidget(h)
        head = QHBoxLayout()
        head.setSpacing(8)
        note = QLabel("双击模板进入编辑器；新建模板需要空白答题卡的扫描图片或 PDF。"
                      "随程序提供的模板修改后另存到用户模板目录。")
        theme.set_style(note, "color:$INK2;")
        note.setWordWrap(True)
        head.addWidget(note, 1)
        add = QPushButton("添加模板目录…")
        add.setToolTip("把别处的模板（含 template.json 的文件夹，或包含多个模板的上级文件夹）加进列表；不复制文件")
        add.clicked.connect(self._add)
        remove = QPushButton("移除目录…")
        remove.setToolTip("从模板列表中移除先前添加的目录（只改设置，不删除文件）")
        remove.clicked.connect(self._remove_dialog)
        edit = QPushButton("编辑")
        edit.clicked.connect(lambda: self._open_row(self.table.currentRow()))
        self.edit_btn = edit
        new = QPushButton("新建模板…")
        new.setObjectName("primary")
        new.setToolTip("用空白答题卡的扫描图片或 PDF 制作新模板")
        new.clicked.connect(self._new_dialog)
        for b in (add, remove, edit, new):
            head.addWidget(b)
        lay.addLayout(head)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["名称", "版本", "纸张（mm）", "面数", "题数", "目录"])
        setup_table(self.table, stretch=5)
        set_empty_text(self.table, "没有找到模板。点“新建模板…”用空白答题卡制作，或点“添加模板目录…”导入已有模板。")
        self.table.setToolTip("双击一行打开编辑器")
        self.table.itemSelectionChanged.connect(self._update_list_buttons)
        for column, width in enumerate((220, 64, 120, 64, 64)):
            self.table.setColumnWidth(column, width)
        self.table.cellDoubleClicked.connect(lambda row, _c: self._open_row(row))
        lay.addSpacing(4)
        lay.addWidget(self.table, 1)
        return view

    def _update_list_buttons(self) -> None:
        has = 0 <= self.table.currentRow() < len(self._dirs) and bool(self.table.selectedItems())
        self.edit_btn.setEnabled(has)
        self.edit_btn.setToolTip("打开选中的模板进行修改（也可以双击列表里的一行）" if has
                                 else "先在下面的列表里选中一个模板")

    def refresh(self) -> None:
        ts = list_templates(self.settings.template_dirs)
        self._dirs = [Path(t.directory) for t in ts]
        self.table.setRowCount(len(ts))
        for i, t in enumerate(ts):
            vals = [t.name, str(t.version), f"{t.paper[0]:g} × {t.paper[1]:g}", str(t.faces), str(t.questions),
                    str(t.directory)]
            for j, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if j in (0, 5):
                    it.setToolTip(v)
                self.table.setItem(i, j, it)
        self._update_list_buttons()

    def add_dir(self, d: str) -> None:
        if d not in self.settings.template_dirs:
            self.settings.template_dirs.append(d)
            save_settings(self.settings)
        self.refresh()
        self.changed.emit()
        if not any(self._is_under(t.directory, d) for t in list_templates([d])):
            QMessageBox.information(
                self, "没有发现模板",
                f"{d}\n里没有找到模板（模板目录里应有 template.json）。\n"
                "目录已记住，以后放进去的模板会自动出现在列表里；不需要时可点“移除目录…”。")

    @staticmethod
    def _is_under(path: str | Path, root: str | Path) -> bool:
        try:
            return Path(path).resolve().is_relative_to(Path(root).resolve())
        except (OSError, ValueError):
            return False

    def removable_dirs(self) -> list[str]:
        """设置里记住的、可以移除的模板目录（随程序提供的目录和用户模板目录总会扫描，不在其列）。"""
        fixed = [bundled_templates_dir(), user_templates_dir()]
        return [d for d in self.settings.template_dirs if not any(_same_dir(d, f) for f in fixed)]

    def remove_dir(self, d: str) -> bool:
        """从设置中移除模板目录（不删除任何文件）。"""
        if d not in self.removable_dirs():
            return False
        self.settings.template_dirs.remove(d)
        save_settings(self.settings)
        self.refresh()
        self.changed.emit()
        return True

    def _remove_dialog(self) -> None:
        dirs = self.removable_dirs()
        if not dirs:
            QMessageBox.information(self, "移除目录", "没有可移除的模板目录。随程序提供的模板目录和用户模板目录总会显示。")
            return
        d, ok = QInputDialog.getItem(self, "移除模板目录", "从模板列表中移除（不删除文件）：", dirs, 0, False)
        if ok and d:
            self.remove_dir(d)

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
            # 识别结果放最前：状态行太长时从中间省略，纸张信息比识别结果和“下一步”次要
            self._enter(doc, "；".join([doc.layout_message] + describe(faces)), fresh=True)

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
        cl.setContentsMargins(16, 12, 16, 12)
        cl.setSpacing(6)
        align_label = QLabel("当前面定位方式")
        align_label.setObjectName("muted")
        cl.addLayout(self._label_with_help(align_label, "anchors"))
        self.alignment_combo = QComboBox()
        self.alignment_combo.addItem("定位块", "anchors")
        self.alignment_combo.addItem("定位外框", "frames")
        self.alignment_combo.setItemData(0, "用卡面角上的黑色定位块对准（至少 3 个）", Qt.ItemDataRole.ToolTipRole)
        self.alignment_combo.setItemData(1, "用印刷的外框对准（卡上没有定位块时用）", Qt.ItemDataRole.ToolTipRole)
        self.alignment_combo.setToolTip("当前面靠什么把扫描图对准模板：定位块或印刷外框")
        self.alignment_combo.currentIndexChanged.connect(self._on_alignment)
        cl.addWidget(self.alignment_combo)
        cl.addSpacing(4)
        target_label = QLabel("主观题目标")
        target_label.setObjectName("muted")
        cl.addLayout(self._label_with_help(target_label, "subjective_target"))
        self.subjective_target = QComboBox()
        self.subjective_target.setToolTip("画主观题框时：新建一道题，还是追加到已有的题（一道题可以由多块拼成）")
        self.subjective_target.addItem("新建主观题", None)
        self.subjective_target.setMinimumWidth(0)
        self.subjective_target.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.subjective_target.activated.connect(self._on_target_chosen)
        cl.addWidget(self.subjective_target)
        ll.addWidget(controls)
        self.tree = RegionTree()
        self.tree.setToolTip("当前面的全部区域；点一行在画布上选中它，Ctrl / Shift 多选，Delete 删除")
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
        self.shortcut_keys: dict[str, list[str]] = {}
        for name, std, fn in (("undo", QKeySequence.StandardKey.Undo, self.undo),
                              ("redo", QKeySequence.StandardKey.Redo, self.redo),
                              ("save", QKeySequence.StandardKey.Save, self.save)):
            # 平台标准键可能有多个（Windows 的重做是 Ctrl+Y 和 Ctrl+Shift+Z），逐个注册；去重避免歧义快捷键
            seqs = list(QKeySequence.keyBindings(std))
            if name == "redo":
                seqs.append(QKeySequence("Ctrl+Shift+Z"))
            keys: list[str] = []
            for seq in seqs:
                text = seq.toString(QKeySequence.SequenceFormat.PortableText)
                if not text or text in keys:
                    continue
                keys.append(text)
                sc = QShortcut(seq, view)
                sc.setContext(ctx)
                sc.activated.connect(fn)
            self.shortcut_keys[name] = keys
        self.save_btn.setToolTip(f"保存模板（{self._key_text('save')}）；"
                                 "随程序提供的模板会另存到用户模板目录。保存前建议先试识别")
        self._update_history_tips()
        return view

    def _key_text(self, name: str) -> str:
        keys = self.shortcut_keys.get(name) or []
        return QKeySequence(keys[0]).toString(QKeySequence.SequenceFormat.NativeText) if keys else ""

    @staticmethod
    def _label_with_help(label: QLabel, concept: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(label)
        row.addWidget(HelpButton(concept))
        row.addStretch(1)
        return row

    def _build_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("editorBar")
        theme.set_style(bar, "#editorBar { background: $HEADER; border-bottom: 1px solid $LINE; } "
                             "#editorBar QPushButton#zoomBtn { padding: 6px 0; }")
        v = QVBoxLayout(bar)
        v.setContentsMargins(16, 8, 16, 8)
        v.setSpacing(8)
        row1 = QHBoxLayout()
        # 返回放在最左边（大多数软件“返回”的位置），进编辑器后一眼能找到
        self.back_btn = QPushButton("‹  模板列表")
        self.back_btn.setToolTip("返回模板列表；有未保存的修改时会先问要不要保存")
        self.back_btn.clicked.connect(self._back)
        row1.addWidget(self.back_btn)
        row1.addWidget(_vsep())
        self.title_label = QLabel("")
        self.title_label.setObjectName("h2")
        row1.addWidget(self.title_label)
        self.rename_btn = QPushButton("改名…")
        self.rename_btn.setToolTip("修改模板名称（保存后写入模板）")
        self.rename_btn.clicked.connect(self._rename_dialog)
        row1.addWidget(self.rename_btn)
        row1.addSpacing(10)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("muted")
        row1.addWidget(self.summary_label)
        row1.addStretch(1)
        row1.addSpacing(8)
        self.layout_btn = QPushButton("识别版面")
        self.layout_btn.setToolTip("自动识别考号、选择题和主观题作答区，替换现有的这三类区域（可撤销）")
        self.layout_btn.clicked.connect(self.auto_layout)
        row1.addWidget(self.layout_btn)
        row1.addSpacing(12)
        self.face_seg = Segmented([("front", "正面"), ("back", "背面")], "front")
        for k, b in self.face_seg.buttons.items():
            b.setToolTip(FACE_TIPS[k])
        self.face_seg.changed.connect(self.set_face)
        row1.addWidget(self.face_seg)
        row1.addSpacing(12)
        zoom = QHBoxLayout()
        zoom.setSpacing(0)
        zo = QPushButton("−")
        zo.setObjectName("zoomBtn")
        zo.setFixedWidth(36)
        zo.setToolTip("缩小（也可以在画布上滚动滚轮）")
        zo.clicked.connect(lambda: self._zoom_by(1 / 1.25))
        zoom.addWidget(zo)
        self.zoom_btn = QPushButton("100%")
        self.zoom_btn.setFlat(True)
        self.zoom_btn.setFixedWidth(64)
        self.zoom_btn.setToolTip("当前缩放比例（相对适应窗口）；点一下恢复为适应窗口")
        self.zoom_btn.clicked.connect(self.fit)
        zoom.addWidget(self.zoom_btn)
        zi = QPushButton("+")
        zi.setObjectName("zoomBtn")
        zi.setFixedWidth(36)
        zi.setToolTip("放大（也可以在画布上滚动滚轮）")
        zi.clicked.connect(lambda: self._zoom_by(1.25))
        zoom.addWidget(zi)
        row1.addLayout(zoom)
        row1.addWidget(_vsep())
        self.save_btn = QPushButton("保存模板")
        self.save_btn.setObjectName("primary")
        self.save_btn.clicked.connect(self.save)
        row1.addWidget(self.save_btn)
        v.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        self.auto_btns: dict[str, QPushButton] = {}
        for text, what, tip in (
                ("自动找定位点", "anchors", "在当前面底图上自动找角上的黑色定位块，替换当前面的定位点（可撤销）"),
                ("自动检测外框", "frames", "自动检测当前面印刷的外框，改用外框定位（可撤销）"),
                ("自动识别同步头", "sync", "自动识别卡边一排等距的小黑块（同步头）；卡上没有就不用点")):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, w=what: self.auto_align(w))
            self.auto_btns[what] = b
            row2.addWidget(b)
        row2.addWidget(_vsep())
        self.select_btn = QPushButton("选择")
        self.select_btn.setObjectName("seg")
        self.select_btn.setCheckable(True)
        self.select_btn.setChecked(True)
        self.select_btn.setToolTip("选择工具：点选、框选、拖动或拉伸区域（Esc 也会回到选择）")
        self.select_btn.clicked.connect(lambda: self.set_tool(None))
        row2.addWidget(self.select_btn)
        self.tool_combo = QComboBox()
        self.tool_combo.addItem("画框：选择类型", None)
        self.tool_combo.setItemData(0, TOOL_TIPS[None], Qt.ItemDataRole.ToolTipRole)
        for k in TOOL_KINDS:
            self.tool_combo.addItem(_swatch_icon(theme.REGION[KIND_COLOR[k]]), TOOL_TEXT.get(k, KIND_LABELS[k]), k)
            self.tool_combo.setItemData(self.tool_combo.count() - 1, TOOL_TIPS[k], Qt.ItemDataRole.ToolTipRole)
        self.tool_combo.setIconSize(QSize(10, 10))
        self.tool_combo.setMinimumWidth(160)
        self.tool_combo.setToolTip("区域工具：选一种区域类型，然后在画布上拖出框；Esc 取消")
        self.tool_combo.currentIndexChanged.connect(lambda i: self.set_tool(self.tool_combo.itemData(i)))
        row2.addWidget(self.tool_combo)
        self.regions_help = HelpButton("regions")
        row2.addWidget(self.regions_help)
        row2.addWidget(_vsep())
        self.snap_btn = QPushButton("自动吸附")
        self.snap_btn.setToolTip("把客观题、考号、填涂字段的网格对齐到印刷的选项框："
                                 "有选中时只吸附选中的，什么都没选时吸附当前面的全部网格")
        self.snap_btn.clicked.connect(self.auto_snap)
        row2.addWidget(self.snap_btn)
        row2.addWidget(HelpButton("snap"))
        self.trial_btn = QPushButton("试识别…")
        self.trial_btn.clicked.connect(self._trial_dialog)
        row2.addWidget(self.trial_btn)
        row2.addWidget(HelpButton("trial"))
        self._set_trial_btn(True)
        row2.addWidget(_vsep())
        self.rotate_btn = QPushButton("旋转底图")
        self.rotate_btn.setToolTip("空白卡底图没摆正时手动旋转当前面（±10° 以内，另可整体旋转 180°）；区域位置不随之移动")
        menu = QMenu(self.rotate_btn)
        menu.setToolTipsVisible(True)
        for text, delta in (("逆时针 0.1°", 0.1), ("顺时针 0.1°", -0.1), ("逆时针 1°", 1.0), ("顺时针 1°", -1.0),
                            ("旋转 180°", 180.0)):
            menu.addAction(text, lambda d=delta: self.rotate_base(d))
        menu.addSeparator()
        self.rotate_reset = menu.addAction("恢复为打开时的角度", lambda: self.rotate_base(None))
        self.rotate_reset.setToolTip("把当前面底图转回打开模板时的角度")
        self.rotate_btn.setMenu(menu)
        self.rotate_btn.setStyleSheet("padding-right: 28px;")  # 给下拉箭头留位置
        row2.addWidget(self.rotate_btn)
        row2.addWidget(_vsep())
        self.undo_btn = QPushButton("撤销")
        self.undo_btn.setToolTip("撤销上一步修改")
        self.undo_btn.clicked.connect(self.undo)
        row2.addWidget(self.undo_btn)
        self.redo_btn = QPushButton("重做")
        self.redo_btn.setToolTip("重做刚撤销的修改")
        self.redo_btn.clicked.connect(self.redo)
        row2.addWidget(self.redo_btn)
        row2.addStretch(1)
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
        self.trial_list.setMinimumHeight(48)  # 最小窗口下给说明条留出高度
        self.trial_list.setToolTip("点一张样卡，画布和下方显示它的识别结果")
        self.trial_empty = set_empty_text(self.trial_list, "还没有结果。点工具栏“试识别…”选择填好的样卡")
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
        ex.setToolTip("关掉试识别结果，画布换回空白卡底图；不改动模板")
        ex.clicked.connect(self.exit_trial)
        self.exit_trial_btn = ex
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
        self.msg_label = ElidedLabel("", Qt.TextElideMode.ElideMiddle)
        self.hint_label = QLabel(HINT)
        self.hint_label.setToolTip("画布操作：滚轮缩放，按住空格拖动，方向键微调（Shift 加大），"
                                   "Delete 删除选中，Esc 退出画框")
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
        if not self._aligned(self.face):
            self.msg_label.setText(self.msg_label.text() + "；" + self._align_advice())
        return True

    def _aligned(self, face: str) -> bool:
        """该面已有足够的定位块（≥3）或外框。"""
        al = self.doc.template.faces[face].align if self.doc is not None and face in self.doc.template.faces else None
        if al is None:
            return False
        return bool(al.frames) if al.method == "frames" else len(al.anchors) >= 3

    @staticmethod
    def _align_advice() -> str:
        return "当前面定位还不够：先点“自动找定位点 / 自动检测外框”，找不到就在“画框”里手动画定位块"

    def new_template(self, name: str, files: list[str]) -> bool:
        """不经对话框新建模板（files：正面文件，或正面 + 背面文件）。"""
        try:
            faces, notes = import_files(files[0], files[1] if len(files) > 1 else "")
            doc = TemplateDoc.new(name, faces)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "无法新建模板", str(e))
            return False
        self._enter(doc, "；".join([doc.layout_message] + describe(faces) + notes), fresh=True)
        return True

    def _enter(self, doc: TemplateDoc, msg: str = "", fresh: bool = False) -> None:
        """fresh：刚新建的模板，状态行提示下一步（先核对定位块，再画区域）。"""
        self.exit_trial()
        self._tried_revision = None
        self.doc = doc
        self.subjective_target.setCurrentIndex(0)
        self._target_auto = False
        self.undo_stack = UndoStack(doc)
        self._reset_merge()
        faces = list(doc.template.faces)
        self.face = faces[0] if faces else "front"
        for k, b in self.face_seg.buttons.items():
            b.setEnabled(k in faces)
            b.setToolTip(FACE_TIPS[k] if k in faces else NO_BACK_TIP if k == "back" else f"模板没有{FACE_TEXT[k]}")
        self.face_seg.set_value(self.face)
        self.set_tool(None)
        self.canvas.set_background(None)
        self.canvas.set_doc(doc, self.face)
        self.tree.set_doc(doc, self.face)
        self.props.set_target(doc, None)
        self.stack.setCurrentWidget(self.editor_view)
        self.fit()
        QTimer.singleShot(0, self.fit)  # 首次显示后视口尺寸才确定
        if fresh:
            t = doc.template
            if t.exam_no or t.objective or t.subjective:
                rest = "再逐块核对自动识别出的区域（题号、单选 / 多选），漏掉的用“画框”补上"
            else:
                rest = "再用“画框”画考号、客观题等区域"
            step = ("下一步：先核对四角的定位块框得对不对，" + rest if self._aligned(self.face)
                    else "下一步：" + self._align_advice() + "，" + rest)
            msg = f"{msg}；{step}" if msg else step
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
        """离开编辑器前检查：有未保存的修改时问“保存 / 放弃修改 / 取消”；保存失败（校验不过）时不离开。"""
        if self.doc is None:
            return True
        self.props.commit_pending()
        if not self.doc.dirty:
            return True
        b = QMessageBox.StandardButton
        answer = QMessageBox.question(self, "模板有未保存的修改", "离开前保存对模板的修改吗？",
                                      b.Save | b.Discard | b.Cancel, b.Save)
        if answer == b.Save:
            if not self.save():
                return False
        elif answer != b.Discard:
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
        self.props.commit_pending()  # 属性面板里还没提交的输入也要保存
        tried = self._tried_revision == (self.doc, self.doc.revision)
        old, old_version = self.doc.directory, self.doc.template.version
        try:
            target = self.doc.save()
        except ValueError as e:
            QMessageBox.warning(self, "模板还不能保存", str(e))
            return False
        except OSError as e:
            QMessageBox.warning(self, "保存失败", f"{type(e).__name__}: {e}")
            return False
        # 不阻止保存的提醒（如未设置标识区，无法检出混卡）接在状态行后面
        notes = "".join(f"；{n}" for n in self.doc.notices())
        if old is not None and _same_dir(old, target) and self.doc.template.version == old_version:
            self.msg_label.setText("没有需要保存的修改" + notes)
            self._update_header()
            return True
        if not tried:
            notes += "；" + TRIAL_ADVICE  # 只提醒，不阻止保存
        self.msg_label.setText(f"已保存到 {target}（版本 {self.doc.template.version}）" + notes)
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

    # ══════════ 改名 ══════════
    def _rename_dialog(self) -> None:
        if self.doc is None:
            return
        name, ok = QInputDialog.getText(self, "模板名称", "模板名：", text=self.doc.template.name)
        if ok:
            self.rename_template(name)

    def rename_template(self, name: str) -> bool:
        """修改模板名（可撤销；保存后写入 template.json，不改已有目录名）。"""
        if self.doc is None:
            return False
        try:
            self._mutate("修改模板名", lambda: self.doc.rename(name))
        except ValueError as e:
            self.msg_label.setText(f"不能改名：{e}")
            return False
        self.msg_label.setText(f"模板名改为“{self.doc.template.name}”，保存后生效")
        self._after_change()
        return True

    # ══════════ 编辑操作 ══════════
    def _mutate(self, label: str, operation, coalesce: bool = False, window: float = 0.0):
        """Only successful content changes commit an undo entry or discard redo.

        coalesce：与紧接着的同类修改（同一标签、同一选中集）合并成一步撤销。window=0 时只合并同一轮
        事件循环里的（一次拖动的多个区域）；window>0 时合并该秒数内连续发生的（方向键微调的自动重复）。"""
        before, bases, revision = self.doc.snapshot(), self.doc.base_state(), self.doc.revision
        result = operation()
        if self.doc.revision != revision:
            key = (label, tuple(self.canvas.selected()), window > 0) if coalesce else None
            if not (coalesce and self._can_merge(key)):
                self.undo_stack.push(label, snapshot=before, bases=bases)
            if coalesce:
                self._merge_key = key
                if window > 0:
                    self._merge_until = time.monotonic() + window
                else:
                    self._merge_until = None
                    self._push_pending = True
                    QTimer.singleShot(0, self._clear_push)
            else:
                self._reset_merge()
        return result

    def _can_merge(self, key: tuple) -> bool:
        if key != self._merge_key:
            return False
        if self._merge_until is None:
            return self._push_pending
        return time.monotonic() <= self._merge_until

    def _clear_push(self) -> None:
        self._push_pending = False

    def _reset_merge(self) -> None:
        self._push_pending = False
        self._merge_key = None
        self._merge_until = None

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
            self.props.refresh_rect()  # 只同步位置数值框
            return
        self.props.set_target(self.doc, target)

    def _on_region_edited(self, ref: RegionRef, rect) -> None:
        if self.doc is None:
            return
        window = self.NUDGE_MERGE_SECONDS if self.canvas.nudging else 0.0
        self._mutate("移动区域", lambda: self.doc.set_rect(ref, tuple(rect)), coalesce=True, window=window)
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
        t = self.doc.template
        # 考号区、缺考标记只能有一个：再画会替换已有的（可撤销），要让用户知道
        replaced = (kind == "exam_no" and t.exam_no is not None) or (kind == "absent" and t.absent is not None)
        try:
            if kind == "subjective" and index is not None:
                ref = self._mutate("追加主观题区域",
                                   lambda: self.doc.add_subjective_region(index, self.face, rect))
                self.msg_label.setText(f"已给第 {target_key} 题追加区域（要另建一题，先在左侧“主观题目标”选“新建主观题”）")
            else:
                label = KIND_LABELS.get(kind, kind)
                ref = self._mutate(f"替换{label}" if replaced else f"添加{label}",
                                   lambda: self.doc.add(kind, self.face, rect))
                if replaced:
                    self.msg_label.setText(f"已替换原有的{label}（只能有一个；可撤销）：{self.doc.title(ref)}")
                else:
                    self.msg_label.setText(f"已添加{label}：{self.doc.title(ref)}")
                if kind in GRID_KINDS:
                    self.msg_label.setText(self.msg_label.text() + "；右侧核对题号、题数、选项数后，" + GRID_HINT)
                elif kind == "anchor" and not self._aligned(self.face):
                    n = len(self.doc.template.faces[self.face].align.anchors)
                    self.msg_label.setText(self.msg_label.text() + f"；定位块至少 3 个，还差 {max(0, 3 - n)} 个")
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
        self.props.commit_pending()
        if ref not in self.doc.regions(self.face):
            return
        res = self.doc.snap(ref, apply=False)  # 失败时文档不变，也不留撤销点
        if res.ok:
            self._mutate("吸附", lambda: self.doc.apply_snap(ref, res))
        self.msg_label.setText(f"{self.doc.title(ref)}：{res.message}")
        self._after_change()
        self.props.refresh_rect()  # 位置数值框换成吸附后的外包框，之后改 X/Y 不会把网格缩放回去
        self.props.set_snap_result(res.message, res.ok)

    def auto_snap(self) -> None:
        """对选中的网格逐个吸附；什么都没选中时吸附当前面的全部网格。"""
        if self.doc is None:
            return
        self.props.commit_pending()
        selected = self.canvas.selected()
        refs = [r for r in selected if r.kind in GRID_KINDS]
        if selected and not refs:
            self.msg_label.setText("选中的区域中没有可吸附的网格（客观题、考号、填涂字段）；"
                                   "要吸附当前面的全部网格，先点画布空白处取消选中")
            return
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
        tail = "；吸附好后可用样卡“试识别…”核对" if all(res.ok for _, res in results) else ""
        self.msg_label.setText("；".join(msgs) + tail)
        self.msg_label.setToolTip("\n".join(msgs))
        self._after_change()
        self.props.refresh_rect()
        if len(refs) == 1 and self.canvas.selected() == refs:
            self.props.set_snap_result(last.message, last.ok)

    def auto_align(self, what: str) -> None:
        if self.doc is None:
            return
        label = {"anchors": "自动找定位点", "frames": "自动检测外框", "sync": "自动识别同步头"}[what]
        text = self._mutate(label, lambda: self.doc.auto_align(self.face, what))
        if what != "sync":
            text += ("；核对画布上的定位框位置，然后用“画框”画区域" if self._aligned(self.face)
                     else "；可以在“画框”里选“定位块（手动）/ 定位外框（手动）”自己框")
        self.msg_label.setText(text)
        self._after_change(select=[])

    def auto_layout(self) -> None:
        """重新识别版面；已有考号、客观题或主观题时先确认（会被替换）。"""
        if self.doc is None:
            return
        t = self.doc.template
        if t.exam_no or t.objective or t.subjective:
            b = QMessageBox.StandardButton
            text = "自动识别的结果会替换现有的考号、客观题和主观题区域，可以撤销。"
            if t.choose_one:
                text += "选做组引用的题号可能随之变化，识别后请核对选做组。"
            answer = QMessageBox.question(self, "识别版面", text + "继续吗？", b.Yes | b.No, b.No)
            if answer != b.Yes:
                return
        self.msg_label.setText(self._mutate("识别版面", self.doc.auto_layout))
        self._after_change(select=[])

    def rotate_base(self, delta: float | None) -> None:
        """旋转当前面的底图；delta=None 恢复为打开模板时的角度。"""
        if self.doc is None:
            return
        face = self.face
        angle = self.doc.base_angle(face)
        delta = -angle if delta is None else delta
        if delta == 0:
            return
        try:
            self._mutate("旋转底图", lambda: self.doc.rotate_base(face, delta))
        except ValueError as e:
            self.msg_label.setText(str(e))
            return
        if self._trial_current is None:
            self.canvas.set_background(None)
        self._after_change()
        name = {"front": "正面", "back": "背面"}.get(face, face)
        self.msg_label.setText(f"{name}底图已旋转到 {self.doc.base_angle(face):+.1f}°；"
                               "请重新“自动找定位点 / 检测外框”，并检查已画区域的位置")

    def undo(self) -> None:
        self._navigate_history(redo=False)

    def redo(self) -> None:
        self._navigate_history(redo=True)

    def _navigate_history(self, *, redo: bool) -> None:
        self._reset_merge()
        if self.doc is None or self.undo_stack is None:
            return
        questions = self.doc.template.subjective
        key = self.subjective_target.currentData()
        index = next((i for i, q in enumerate(questions) if q.key == key), None)
        count = len(questions)
        bases = self.doc.base_state()
        changed = self.undo_stack.redo() if redo else self.undo_stack.undo()
        if changed:
            if self.doc.base_state() != bases and self._trial_current is None:
                self.canvas.set_background(None)  # 撤销/重做了底图旋转：重新取底图
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
        self._update_tool_hint()

    def _append_target(self) -> str | None:
        """画主观题时要追加到的题（None = 新建一题）。"""
        key = self.subjective_target.currentData()
        if self.doc is None or not any(q.key == key for q in self.doc.template.subjective):
            return None
        return key

    def _update_tool_hint(self) -> None:
        kind = self.canvas._tool
        target = self._append_target() if kind == "subjective" else None
        if kind is None:
            text = HINT
        elif target is not None:
            # 追加目标会“粘住”：画框前在状态行醒目提示
            text = f"拖出的主观题区域将追加到第 {target} 题（要另建一题，先在左侧选“新建主观题”）；Esc 取消"
        else:
            text = f"在画布上拖出“{TOOL_TEXT.get(kind, KIND_LABELS[kind])}”区域；Esc 取消"
        self.hint_label.setText(text)
        theme.set_style(self.hint_label, "color:$WARN; font-weight:600;" if target is not None else "")

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
            self._target_auto = True
        elif self.doc is not None and not refs and self._target_auto:
            # 因选中而自动设的追加目标，取消选中后恢复为“新建主观题”，免得之后画的框误追加到上一题；
            # 在下拉框里手动选的目标保持不变。切换正背面不发出选中变化，目标保留。
            self._target_auto = False
            self.subjective_target.setCurrentIndex(0)
        self.tree.select(refs)
        self._sync_props()
        self._update_tool_hint()

    def _on_target_chosen(self, _index: int) -> None:
        """用户在“主观题目标”下拉框里手动选择：之后取消选中也不重置。"""
        self._target_auto = False
        self._update_tool_hint()

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
        self._update_history_tips()
        rotated = self.face in d.bases and abs(d.base_angle(self.face)) > 1e-9
        self.rotate_reset.setEnabled(rotated)
        self.rotate_reset.setToolTip("把当前面底图转回打开模板时的角度" if rotated
                                     else "当前面底图没有旋转过，不需要恢复")
        self.region_count.setText(f"{len(d.regions(self.face))} 个区域")
        al = t.faces[self.face].align if self.face in t.faces else None
        if al is None:
            self.align_label.setText("")
        elif al.method == "frames":
            self.align_label.setText(f"{FACE_TEXT.get(self.face, self.face)}外框 {len(al.frames)}")
        else:
            n = len(al.anchors)
            note = "（至少 3 个）" if n < 3 else "（3 个按仿射定位，4 个更稳）" if n == 3 else ""
            self.align_label.setText(f"{FACE_TEXT.get(self.face, self.face)}定位点 {n}{note}")
        n_sync = sum(len(s.marks) for s in t.sync if s.face == self.face)
        if n_sync and al is not None:
            self.align_label.setText(self.align_label.text() + f"，同步头 {n_sync}")
        self.paper_label.setText(f"纸张 {t.paper[0]:g} × {t.paper[1]:g} mm")

    def _update_history_tips(self) -> None:
        if not hasattr(self, "shortcut_keys"):
            return
        u, r = self._key_text("undo"), self._key_text("redo")
        self.undo_btn.setToolTip(f"撤销上一步修改（{u}）" if self.undo_btn.isEnabled()
                                 else f"还没有可撤销的修改（{u}）")
        self.redo_btn.setToolTip(f"重做刚撤销的修改（{r}）" if self.redo_btn.isEnabled()
                                 else f"没有可重做的修改：撤销之后才能重做（{r}）")

    # ══════════ 试识别 ══════════
    def _set_trial_btn(self, enabled: bool) -> None:
        self.trial_btn.setEnabled(enabled)
        self.trial_btn.setToolTip("选几张填好的样卡（图片或 PDF），按当前模板试跑一遍，右侧和画布上显示结果；"
                                  "不创建批次、不写输出目录" if enabled else "正在试识别，请稍候…")

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
        self.props.commit_pending()  # 用面板里刚输入、尚未提交的参数试识别
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
        self.trial_empty.setText("正在试识别，请稍候…")
        self._set_trial_btn(False)

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
        self._set_trial_btn(True)
        if isinstance(res, Exception):
            self.trial_head.setText(f"试识别失败：{type(res).__name__}: {res}")
            self.trial_empty.setText("没有结果。检查样卡文件后再点“试识别…”")
            return
        self._tried_revision = (self.doc, self.doc.revision)
        self._trial_results = list(res)
        n_ok = sum(1 for s in self._trial_results if s.ok)
        self.trial_head.setText(f"处理完成 {n_ok} / {len(self._trial_results)} 张，待核对")
        self.trial_empty.setText("没有识别出答卷，换几张样卡再试")
        if self._trial_results:
            self.msg_label.setText("试识别完成：点右侧每张样卡核对画布上的叠层和下方结果；"
                                   "有问题改模板（结果会失效）后再试，没问题就“保存模板”")
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
                it.setForeground(theme.qcolor(theme.ERR))
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
        self._set_trial_btn(True)
        self.trial_list.blockSignals(True)
        self.trial_list.clear()
        self.trial_list.blockSignals(False)
        self.trial_empty.setText("还没有结果。点工具栏“试识别…”选择填好的样卡")
        self.canvas.set_background(None)
        self.canvas.set_marks([])
        self.props_stack.setCurrentIndex(0)
