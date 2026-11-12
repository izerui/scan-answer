"""属性面板：显示和编辑选中区域的参数、位置、吸附结果。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QStyle, QVBoxLayout, QWidget)

from ...template.doc import KIND_LABELS, RegionRef, TemplateDoc
from .. import theme
from ..widgets.common import Segmented, form_layout, hline, section_header
from ..widgets.guide import HelpButton

# 默认选项标签
_DEFAULT_LABELS = {
    "single": list("ABCDEFGH"),
    "multiple": list("ABCDEFGH"),
    "judge": ["T", "F"],
}
# 标签固定的题型：切换到这些题型时换成固定标签，选项数也随之固定
_FIXED_LABELS = {"judge": ["T", "F"]}

# 主观题 qtype 映射
_SUBJ_QTYPE = [("fill", "填空"), ("short", "问答"), ("essay", "作文")]
_SUBJ_CONTENT = [("zh", "中文"), ("en", "英文"), ("math", "数学")]

EMPTY_TEXT = ("点选画布上的区域查看和修改属性（也可以在左侧列表里点）。\n\n"
              "画新区域：在左侧的“添加区域”工具列点类型，然后在画布上拖出框。\n"
              "按住 Ctrl / Shift 可多选，一起拖动或删除。")
# 选中这些区域时，标题旁显示“?”说明
KIND_CONCEPT = {"identity": "identity", "ignore": "ignore", "anchor": "anchors", "frame": "anchors",
                "subjective": "subjective_target"}
# 没有参数（或参数之外需要说明用途）的区域：面板里的一句说明
KIND_NOTE = {
    "identity": ("识别时把卡面这一块与底图比对，对不上判为混卡（WRONG_CARD）。"
                 "应框住印有科目 / 卷别名称、不会被书写的印刷文字，"
                 "如“英语（一）”“思想政治理论”；各科目答题卡相同的部分不要框。"),
    "ignore": ("自动找定位块 / 外框和识别对齐时把这块当白纸，不看它；不影响填涂识别和切图。"
               "用来框大块灰底、贴条码的位置等会干扰程序的地方。"),
    "anchor": "程序靠定位块把扫描图对准模板：框紧角上的整个黑块。至少 3 个，4 个更稳。",
    "frame": "程序靠印刷外框把扫描图对准模板：框住整个外框。",
    "sync": "同步头：卡边一排等距的小黑块，用来校正行位置；一般用“自动识别同步头”生成。",
    "absent": "缺考标记：监考老师涂的缺考框，涂了结果里记为缺考。只能有一个。",
}
SPIN_TIPS = {
    "X": "区域左边到纸张左边的距离",
    "Y": "区域上边到纸张上边的距离",
    "宽": "区域宽度；网格类区域改宽高会按比例缩放网格",
    "高": "区域高度；网格类区域改宽高会按比例缩放网格",
}
DIRECTION_TIPS = {"horizontal": "横排：同一题（同一位）的选项左右排开",
                  "vertical": "竖排：同一题（同一位）的选项上下排开"}


def _tip(widget: QWidget, text: str | dict[str, str]) -> QWidget:
    """设置悬停说明；分段按钮可以给每个选项单独写（dict：值 → 说明）。"""
    if isinstance(widget, Segmented):
        texts = text if isinstance(text, dict) else {}
        whole = text if isinstance(text, str) else "；".join(texts.values())
        widget.setToolTip(whole)
        for key, b in widget.buttons.items():
            b.setToolTip(texts.get(key, whole))
    else:
        widget.setToolTip(text)
    return widget


def _mm_spin(value: float = 0.0, lo: float = 0.0, hi: float = 999.0,
             step: float = 0.1) -> QDoubleSpinBox:
    """创建 mm 单位的 QDoubleSpinBox。"""
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(1)
    sb.setSingleStep(step)
    sb.setSuffix(" mm")
    sb.setMinimumWidth(80)  # 默认最小宽度按 999.0 mm 计算，两列并排会撑破属性面板
    sb.setValue(value)
    sb.setKeyboardTracking(False)
    return sb


def _int_spin(value: int = 1, lo: int = 1, hi: int = 200) -> QSpinBox:
    """创建整数 QSpinBox。"""
    sb = QSpinBox()
    sb.setRange(lo, hi)
    sb.setValue(value)
    sb.setKeyboardTracking(False)
    return sb


class PropsPanel(QWidget):
    """区域属性面板。"""

    # 信号
    paramsChanged = Signal(object, dict)      # (RegionRef, 参数字典)
    rectChanged = Signal(object, object)       # (RegionRef, Rect)
    snapRequested = Signal(object)             # (RegionRef,)
    regionRemoveRequested = Signal(object)     # (RegionRef,) — 主观题区域删除
    regionMoveRequested = Signal(object, int)  # (RegionRef, destination)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("propsPanel")
        self._doc: TemplateDoc | None = None
        self._ref: RegionRef | None = None
        self._suppress = False

        # 主滚动区
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._scroll.setWidget(self._content)
        self._content.setAutoFillBackground(False)  # setWidget 会打开自动填充，露出窗口底色

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        # 标题（难懂的区域类型旁带“?”）
        self._header_row = QWidget()
        hr = QHBoxLayout(self._header_row)
        hr.setContentsMargins(0, 0, 16, 0)
        hr.setSpacing(4)
        self._header = QLabel()
        self._header.setObjectName("h2")
        self._header.setStyleSheet("font-size:16px; font-weight:600; padding:12px 16px 8px 12px;")
        self._header.setWordWrap(True)
        hr.addWidget(self._header, 1)
        self._help: HelpButton | None = None
        self._help_concept: str | None = None
        self._help_slot = hr
        self._layout.addWidget(self._header_row)

        # 未选中时的说明
        self._empty = QLabel(EMPTY_TEXT)
        self._empty.setObjectName("muted")
        self._empty.setWordWrap(True)
        self._empty.setContentsMargins(16, 16, 16, 16)
        self._layout.addWidget(self._empty)

        # 参数区容器
        self._params_container = QWidget()
        self._params_layout = QVBoxLayout(self._params_container)
        self._params_layout.setContentsMargins(16, 8, 16, 8)
        self._params_layout.setSpacing(6)
        self._layout.addWidget(self._params_container)

        # 位置区
        self._pos_line = hline()
        self._layout.addWidget(self._pos_line)
        self._pos_header = section_header("位置（毫米，相对定位点校正后）")
        self._layout.addWidget(self._pos_header)

        pos_w = QWidget()
        pos_grid = QGridLayout(pos_w)
        pos_grid.setContentsMargins(16, 10, 16, 10)
        pos_grid.setHorizontalSpacing(8)
        pos_grid.setVerticalSpacing(10)

        self._spin_x = _mm_spin()
        self._spin_y = _mm_spin()
        self._spin_w = _mm_spin()
        self._spin_h = _mm_spin()

        for i, (label, spin) in enumerate((("X", self._spin_x), ("Y", self._spin_y),
                                           ("宽", self._spin_w), ("高", self._spin_h))):
            spin.setToolTip(SPIN_TIPS[label] + "（毫米，按 0.1 mm 调；画布上也可以拖动或用方向键微调）")
            pos_grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            pos_grid.addWidget(spin, i // 2, (i % 2) * 2 + 1)
        pos_grid.setColumnStretch(1, 1)
        pos_grid.setColumnStretch(3, 1)
        pos_grid.setColumnMinimumWidth(2, 28)

        self._layout.addWidget(pos_w)
        self._pos_w = pos_w

        # 吸附区（仅网格类可见）
        self._snap_container = QWidget()
        snap_lay = QVBoxLayout(self._snap_container)
        snap_lay.setContentsMargins(16, 8, 16, 8)
        snap_lay.setSpacing(6)

        self._btn_snap = QPushButton("自动吸附")
        self._btn_snap.setToolTip("把这块网格对齐到底图上印刷的选项框；框画得松一点没关系，吸附后位置会更新")
        snap_row = QHBoxLayout()
        snap_row.setContentsMargins(0, 0, 0, 0)
        snap_row.setSpacing(4)
        snap_row.addWidget(self._btn_snap, 1)
        snap_row.addWidget(HelpButton("snap"))
        snap_lay.addLayout(snap_row)
        self._snap_label = QLabel()
        self._snap_label.setWordWrap(True)
        snap_lay.addWidget(self._snap_label)

        self._layout.addWidget(self._snap_container)

        # 弹簧
        self._layout.addStretch(1)

        # 连接位置 spin 信号（只替换实际改动的分量）
        for i, spin in enumerate((self._spin_x, self._spin_y, self._spin_w, self._spin_h)):
            spin.valueChanged.connect(lambda v, i=i: self._on_rect_changed(i, v))

        self._btn_snap.clicked.connect(self._on_snap)

        # 动态控件引用
        self._spin_n_questions: QSpinBox | None = None
        self._seg_qtype: Segmented | None = None
        self._sub_remove_buttons: list[QPushButton] = []

        # 初始隐藏
        self._show_sections(False)

    def set_target(self, doc: TemplateDoc, ref: RegionRef | None) -> None:
        """设置目标区域。"""
        self._doc = doc
        self._ref = ref
        self._suppress = True
        self._rebuild()
        self._suppress = False

    def target(self) -> RegionRef | None:
        """当前显示的区域。"""
        return self._ref

    def refresh_rect(self) -> None:
        """只按文档刷新位置数值框（吸附、拖动后），不重建参数控件。"""
        if self._ref is None or self._doc is None:
            return
        try:
            rect = self._doc.rect(self._ref)
        except (IndexError, KeyError, ValueError, AttributeError):
            return
        self._suppress = True
        try:
            for spin, v in zip((self._spin_x, self._spin_y, self._spin_w, self._spin_h), rect):
                if abs(spin.value() - v) >= 0.05:  # 数值框按 0.1 mm 显示，差不到半格不动
                    spin.setValue(v)
        finally:
            self._suppress = False

    def commit_pending(self) -> None:
        """提交正在输入、尚未按回车或离开焦点的内容（保存、试识别、离开页面前调用）。"""
        if self._ref is None:
            return
        for spin in self.findChildren(QAbstractSpinBox):
            spin.interpretText()  # 文字与当前值不同时发出 valueChanged
        for edit in self.findChildren(QLineEdit):
            if isinstance(edit.parent(), QAbstractSpinBox):
                continue
            if edit.isModified():
                edit.setModified(False)
                edit.editingFinished.emit()

    def set_snap_result(self, msg: str, ok: bool) -> None:
        """显示吸附结果。"""
        color = "$INK" if ok else "$ERR"
        theme.set_style(self._snap_label, f"color:{color}; font-size:12px;")
        self._snap_label.setText(msg)

    def _rebuild(self) -> None:
        """重建面板内容。"""
        # 清理旧参数控件
        self._clear_params()
        self._sub_remove_buttons.clear()

        if self._ref is None or self._doc is None:
            self._show_sections(False)
            return
        self._show_sections(True)

        doc = self._doc
        ref = self._ref

        # 标题
        kind_label = KIND_LABELS.get(ref.kind, ref.kind)
        title = doc.title(ref)
        self._header.setText(f"{kind_label} {title}")
        self._header_row.show()
        self._set_help(KIND_CONCEPT.get(ref.kind))

        # 参数区
        params = doc.params(ref)
        self._build_params(ref, params)

        # 位置
        rect = doc.rect(ref)
        self._spin_x.setValue(rect[0])
        self._spin_y.setValue(rect[1])
        self._spin_w.setValue(rect[2])
        self._spin_h.setValue(rect[3])
        self._pos_header.show()

        # 吸附（仅网格类）
        if ref.kind in ("objective", "exam_no", "omr_field"):
            self._snap_container.show()
            self._snap_label.clear()
        else:
            self._snap_container.hide()

    def _show_sections(self, selected: bool) -> None:
        """有选中区域时显示标题、参数、位置；没有时只显示说明。"""
        self._empty.setVisible(not selected)
        for w in (self._header_row, self._pos_line, self._pos_header, self._pos_w):
            w.setVisible(selected)
        if not selected:
            self._set_help(None)
        if not selected:
            self._params_container.hide()
            self._snap_container.hide()

    def _set_help(self, concept: str | None) -> None:
        """标题旁的“?”换成对应概念的说明（None = 不显示）。"""
        if concept == self._help_concept:
            return
        self._help_concept = concept
        if self._help is not None:
            self._help_slot.removeWidget(self._help)
            self._help.deleteLater()
            self._help = None
        if concept:
            self._help = HelpButton(concept)
            self._help_slot.addWidget(self._help, 0, Qt.AlignmentFlag.AlignVCenter)
            self._help.show()

    def _clear_params(self) -> None:
        """清理参数区的动态控件。"""
        self._spin_n_questions = None
        self._seg_qtype = None
        while self._params_layout.count():
            item = self._params_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def _build_params(self, ref: RegionRef, params: dict) -> None:
        """根据区域类型构建参数编辑控件。"""
        if ref.kind in KIND_NOTE:
            self._params_container.show()
            note = QLabel(KIND_NOTE[ref.kind])
            note.setObjectName("muted")
            note.setWordWrap(True)
            self._params_layout.addWidget(note)
            if not params:
                return
        if not params:
            self._params_container.hide()
            return

        self._params_container.show()
        if ref.kind == "objective":
            self._build_objective_params(params)
        elif ref.kind == "exam_no":
            self._build_exam_no_params(params)
        elif ref.kind == "omr_field":
            self._build_omr_field_params(params)
        elif ref.kind == "subjective":
            self._build_subjective_params(params)
        elif ref.kind == "choose_one":
            self._build_choose_one_params(params)
        elif ref.kind == "field":
            self._build_field_params(params)
        elif ref.kind == "barcode":
            self._build_barcode_params(params)
        elif ref.kind not in KIND_NOTE:
            self._params_container.hide()

    def _build_objective_params(self, params: dict) -> None:
        """客观题参数。"""
        lay = self._params_layout

        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        # 起始题号
        spin_start = _int_spin(params.get("start", 1), 1, 9999)
        spin_start.valueChanged.connect(lambda v: self._emit_params({"start": v}))
        _tip(spin_start, "这块第一道题的题号，后面的题依次加 1")
        fl.addRow("起始题号", spin_start)

        # 题数
        self._spin_n_questions = _int_spin(params.get("n_questions", 5), 1, 200)
        self._spin_n_questions.valueChanged.connect(
            lambda v: self._emit_params({"n_questions": v}))
        _tip(self._spin_n_questions, "这块一共几道题；改了会在当前范围里重新排网格，之后再点“自动吸附”对齐")
        fl.addRow("题数", self._spin_n_questions)

        # 选项数（判断题固定 2 个）
        qtype = params.get("qtype", "single")
        spin_opts = _int_spin(params.get("n_options", 4), 2, 8)
        if qtype in _FIXED_LABELS:
            spin_opts.setRange(len(_FIXED_LABELS[qtype]), len(_FIXED_LABELS[qtype]))
            spin_opts.setEnabled(False)
            spin_opts.setToolTip("判断题固定 2 个选项（T / F），不能改")
        else:
            spin_opts.setToolTip("每道题几个选项（2–8 个）；增加时自动补上字母")
        spin_opts.valueChanged.connect(lambda v: self._on_n_options_changed(v))
        fl.addRow("选项数", spin_opts)

        # 每组题数
        spin_group = _int_spin(params.get("group", 0), 0, 50)
        spin_group.valueChanged.connect(lambda v: self._emit_params({"group": v}))
        _tip(spin_group, "卡上每几道题空出一段间隔（如每 5 题一组）；0 = 不分组、均匀排列")
        fl.addRow("每组题数", spin_group)

        # 选项标签
        labels = params.get("labels", ["A", "B", "C", "D"])
        labels_row = QHBoxLayout()
        labels_row.setContentsMargins(0, 0, 0, 0)
        labels_row.setSpacing(4)
        self._label_edits: list[QLineEdit] = []
        for i, lb in enumerate(labels):
            le = QLineEdit(lb)
            le.setFixedWidth(36)
            le.setAlignment(Qt.AlignmentFlag.AlignCenter)
            le.setToolTip(f"第 {i + 1} 个选项的名字，识别结果里用它（默认 A、B、C…，可以改）")
            le.editingFinished.connect(self._on_labels_changed)
            self._label_edits.append(le)
            labels_row.addWidget(le)
        labels_row.addStretch(1)
        lw = QWidget()
        lw.setLayout(labels_row)
        fl.addRow("选项", lw)

        # 类型（单选/多选/判断）
        self._seg_qtype = Segmented(
            [("single", "单选"), ("multiple", "多选"), ("judge", "判断")], qtype)
        _tip(self._seg_qtype, {"single": "单选：每题只能选一个", "multiple": "多选：每题可以选多个",
                               "judge": "判断：两个选项 T / F"})
        self._seg_qtype.changed.connect(self._on_qtype_changed)
        fl.addRow("类型", self._left(self._seg_qtype))

        # 方向（横排/竖排）
        horiz = params.get("options_horizontal", True)
        self._seg_direction = Segmented(
            [("horizontal", "横排"), ("vertical", "竖排")],
            "horizontal" if horiz else "vertical")
        _tip(self._seg_direction, DIRECTION_TIPS)
        self._seg_direction.changed.connect(self._on_direction_changed)
        fl.addRow("方向", self._left(self._seg_direction))

        lay.addWidget(form)

    @staticmethod
    def _left(widget: QWidget) -> QWidget:
        """表单字段默认撑满整行，分段按钮保持自身宽度靠左。"""
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addStretch(1)
        return w

    def _build_exam_no_params(self, params: dict) -> None:
        """考号参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        spin_digits = _int_spin(params.get("n_digits", 10), 1, 30)
        _tip(spin_digits, "考号有几位数字（每位一组 0–9 填涂框）")
        spin_digits.valueChanged.connect(lambda v: self._emit_params({"n_digits": v}))
        fl.addRow("位数", spin_digits)

        horiz = params.get("options_horizontal", True)
        seg = Segmented([("horizontal", "横排"), ("vertical", "竖排")],
                        "horizontal" if horiz else "vertical")
        _tip(seg, {"horizontal": "横排：每一位的 0–9 左右排开", "vertical": "竖排：每一位的 0–9 上下排开（常见）"})
        seg.changed.connect(self._on_direction_changed)
        self._seg_direction = seg
        fl.addRow("方向", self._left(seg))

        lay.addWidget(form)

    def _build_omr_field_params(self, params: dict) -> None:
        """自定义填涂字段参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        le_key = QLineEdit(params.get("key", ""))
        _tip(le_key, "结果里的字段名，用英文字母，如 subject；同一模板里不能重复")
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        _tip(le_label, "给人看的名字，如“科目代码”")
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        spin_digits = _int_spin(params.get("n_digits", 1), 1, 30)
        _tip(spin_digits, "这个字段有几位（每位一组填涂框）")
        spin_digits.valueChanged.connect(lambda v: self._emit_params({"n_digits": v}))
        fl.addRow("位数", spin_digits)

        le_values = QLineEdit(",".join(params.get("labels", [])))
        _tip(le_values, "每位可涂的值，按卡上顺序写，每个值一个字符，如 0123456789 或 ABCD")
        le_values.editingFinished.connect(
            lambda: self._emit_params({"labels": list(le_values.text().replace(",", ""))}))
        fl.addRow("值", le_values)

        horiz = params.get("options_horizontal", True)
        seg = Segmented([("horizontal", "横排"), ("vertical", "竖排")],
                        "horizontal" if horiz else "vertical")
        _tip(seg, {"horizontal": "横排：每一位的可选值左右排开", "vertical": "竖排：每一位的可选值上下排开"})
        seg.changed.connect(self._on_direction_changed)
        self._seg_direction = seg
        fl.addRow("方向", self._left(seg))

        lay.addWidget(form)

    def _build_subjective_params(self, params: dict) -> None:
        """主观题参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        le_no = QLineEdit(params.get("no", ""))
        _tip(le_no, "这道题的题号，如 41；结果里按题号（加小题号）区分")
        le_no.editingFinished.connect(lambda: self._emit_params({"no": le_no.text()}))
        fl.addRow("题号", le_no)

        le_sub = QLineEdit(params.get("sub", ""))
        le_sub.setPlaceholderText("可空，如 1、(2)、a")
        _tip(le_sub, "一道大题分小题切图时填，如 1、(2)、a；不分小题留空")
        le_sub.editingFinished.connect(lambda: self._emit_params({"sub": le_sub.text().strip()}))
        fl.addRow("小题号", le_sub)

        seg_qtype = Segmented(_SUBJ_QTYPE, params.get("qtype", "essay"))
        _tip(seg_qtype, {"fill": "填空：短答案", "short": "问答：几行到一段的解答", "essay": "作文：整段长文"})
        seg_qtype.changed.connect(lambda v: self._emit_params({"qtype": v}))
        fl.addRow("题型", self._left(seg_qtype))

        seg_content = Segmented(_SUBJ_CONTENT, params.get("content", "zh"))
        _tip(seg_content, {"zh": "内容类型：中文作答", "en": "内容类型：英文作答",
                           "math": "内容类型：数学公式、演算"})
        seg_content.changed.connect(lambda v: self._emit_params({"content": v}))
        fl.addRow("内容", self._left(seg_content))

        lay.addWidget(form)

        # 区域列表（如果有多个区域）
        if self._ref and self._doc:
            s = self._doc.template.subjective[self._ref.index]
            if len(s.regions) > 0:
                reg_header = QLabel("区域列表")
                reg_header.setContentsMargins(0, 8, 0, 2)
                theme.set_style(reg_header, "font-weight:600; font-size:13px; color:$INK2;")
                lay.addWidget(reg_header)
                for ri, reg in enumerate(s.regions):
                    row = QHBoxLayout()
                    row.setContentsMargins(0, 2, 0, 2)
                    row.setSpacing(6)
                    face_label = "正面" if reg.face == "front" else "背面"
                    row.addWidget(QLabel(f"区域 {ri + 1}（{face_label}）"))
                    row.addStretch(1)
                    sub_ref = RegionRef("subjective", self._ref.index, ri)
                    for delta, icon, tooltip, edge in (
                        (-1, QStyle.StandardPixmap.SP_ArrowUp, "区域上移", "已经是第一块，不能再上移"),
                        (1, QStyle.StandardPixmap.SP_ArrowDown, "区域下移", "已经是最后一块，不能再下移"),
                    ):
                        move = QPushButton()
                        move.setIcon(self.style().standardIcon(icon))
                        movable = 0 <= ri + delta < len(s.regions)
                        move.setToolTip(f"{tooltip}：切图按区域顺序拼接" if movable else edge)
                        move.setAccessibleName(tooltip)
                        move.setFixedSize(28, 28)
                        move.setEnabled(movable)
                        move.clicked.connect(
                            lambda _c=False, r=sub_ref, dest=ri + delta: self.regionMoveRequested.emit(r, dest))
                        row.addWidget(move)
                    btn_del = QPushButton("删除")
                    btn_del.setObjectName("danger")
                    btn_del.setToolTip("删除这一块区域（可撤销）" if len(s.regions) > 1
                                       else "删除这一块区域；这是唯一的一块，整道题会一起删除（可撤销）")
                    btn_del.clicked.connect(
                        lambda _c=False, r=sub_ref: self.regionRemoveRequested.emit(r))
                    self._sub_remove_buttons.append(btn_del)
                    row.addWidget(btn_del)
                    rw = QWidget()
                    rw.setLayout(row)
                    lay.addWidget(rw)

    def _build_choose_one_params(self, params: dict) -> None:
        """选做组参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        le_key = QLineEdit(params.get("key", ""))
        _tip(le_key, "结果里这个选做组的名字，用英文字母，如 choose1；不能重复")
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        _tip(le_label, "给人看的名字，如“选做题”")
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        questions = params.get("questions", [])
        le_questions = QLineEdit(",".join(questions))
        _tip(le_questions, "这组里可选做的题号，用英文逗号分隔，如 22,23")
        le_questions.editingFinished.connect(
            lambda: self._emit_params({"questions": [q.strip() for q in le_questions.text().split(",") if q.strip()]}))
        fl.addRow("题号", le_questions)

        lay.addWidget(form)

    def _build_field_params(self, params: dict) -> None:
        """信息栏参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        le_key = QLineEdit(params.get("key", ""))
        _tip(le_key, "结果里这一栏的字段名，用英文字母，如 name；不能重复")
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        _tip(le_label, "给人看的名字，如“姓名”")
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        lay.addWidget(form)

    def _build_barcode_params(self, params: dict) -> None:
        """条码参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = form_layout(form, margins=(0, 4, 0, 4))

        le_key = QLineEdit(params.get("key", ""))
        _tip(le_key, "结果里这个条码的字段名，如 student_barcode；不能重复")
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        lay.addWidget(form)

    # ── 信号转发 ──────────────────────────────────────────────

    def _emit_params(self, p: dict) -> None:
        """发出参数变化信号。"""
        if self._suppress or self._ref is None:
            return
        self.paramsChanged.emit(self._ref, p)

    def _on_rect_changed(self, index: int, value: float) -> None:
        """位置 spin 变化 → 发出 rectChanged。

        数值框只显示 0.1 mm；只替换实际改动的分量，其余分量用文档原值，避免网格被取整微量缩放。"""
        if self._suppress or self._ref is None or self._doc is None:
            return
        new_rect = list(self._doc.rect(self._ref))
        new_rect[index] = round(value, 1)
        self.rectChanged.emit(self._ref, tuple(new_rect))

    def _on_snap(self) -> None:
        """吸附按钮点击。"""
        if self._ref is not None:
            self.snapRequested.emit(self._ref)

    def _current_labels(self) -> list[str]:
        if self._doc is not None and self._ref is not None:
            return list(self._doc.params(self._ref).get("labels", []))
        return []

    def _on_qtype_changed(self, qtype: str) -> None:
        """客观题类型切换：只有标签固定的题型（判断）才换标签；单选/多选之间保留选项数和自定义标签。"""
        labels = self._current_labels()
        if qtype in _FIXED_LABELS:
            fixed = _FIXED_LABELS[qtype][:]
            self._emit_params({"qtype": qtype, "labels": fixed, "n_options": len(fixed)})
            return
        if any(labels == fixed for fixed in _FIXED_LABELS.values()):
            # 从判断题切回：T/F 不适合单选/多选，换成同样个数的 A、B…
            new = _DEFAULT_LABELS[qtype][:len(labels)]
            self._emit_params({"qtype": qtype, "labels": new, "n_options": len(new)})
            return
        self._emit_params({"qtype": qtype})

    def _on_n_options_changed(self, n: int) -> None:
        """选项数变化 → 保留已有（自定义）标签，增加时用未占用的默认字母补齐。"""
        qtype = self._seg_qtype.value() if self._seg_qtype else "single"
        if qtype in _FIXED_LABELS:
            return
        labels = self._current_labels()[:n]
        for candidate in _DEFAULT_LABELS.get(qtype, list("ABCDEFGH")) + [chr(ord("I") + i) for i in range(18)]:
            if len(labels) >= n:
                break
            if candidate not in labels:
                labels.append(candidate)
        self._emit_params({"n_options": n, "labels": labels})

    def _on_labels_changed(self) -> None:
        """标签编辑完成。"""
        if not hasattr(self, "_label_edits"):
            return
        labels = [le.text() for le in self._label_edits]
        self._emit_params({"labels": labels, "n_options": len(labels)})

    def _on_direction_changed(self, v: str) -> None:
        """方向切换。"""
        self._emit_params({"options_horizontal": v == "horizontal"})
