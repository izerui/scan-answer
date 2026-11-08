"""属性面板：显示和编辑选中区域的参数、位置、吸附结果。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSpinBox,
                               QStyle, QVBoxLayout, QWidget)

from ...template.doc import KIND_LABELS, RegionRef, TemplateDoc
from .. import theme
from ..widgets.common import Segmented, hline, section_header

# 默认选项标签
_DEFAULT_LABELS = {
    "single": list("ABCDEFGH"),
    "multiple": list("ABCDEFGH"),
    "judge": ["T", "F"],
}

# 主观题 qtype 映射
_SUBJ_QTYPE = [("fill", "填空"), ("short", "问答"), ("essay", "作文")]
_SUBJ_CONTENT = [("zh", "中文"), ("en", "英文"), ("math", "数学")]


def _mm_spin(value: float = 0.0, lo: float = 0.0, hi: float = 999.0,
             step: float = 0.1) -> QDoubleSpinBox:
    """创建 mm 单位的 QDoubleSpinBox。"""
    sb = QDoubleSpinBox()
    sb.setRange(lo, hi)
    sb.setDecimals(1)
    sb.setSingleStep(step)
    sb.setSuffix(" mm")
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

        self._content = QWidget()
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._scroll.setWidget(self._content)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._scroll)

        # 标题
        self._header = QLabel()
        self._header.setObjectName("h2")
        self._header.setStyleSheet("font-size:16px; font-weight:600; padding:12px 16px;")
        self._layout.addWidget(self._header)

        # 参数区容器
        self._params_container = QWidget()
        self._params_layout = QVBoxLayout(self._params_container)
        self._params_layout.setContentsMargins(16, 8, 16, 8)
        self._params_layout.setSpacing(6)
        self._layout.addWidget(self._params_container)

        # 位置区
        self._layout.addWidget(hline())
        self._pos_header = section_header("位置（毫米，相对定位点校正后）")
        self._layout.addWidget(self._pos_header)

        pos_w = QWidget()
        pos_form = QFormLayout(pos_w)
        pos_form.setContentsMargins(16, 8, 16, 8)
        pos_form.setSpacing(6)

        self._spin_x = _mm_spin()
        self._spin_y = _mm_spin()
        self._spin_w = _mm_spin()
        self._spin_h = _mm_spin()

        xy_row = QHBoxLayout()
        xy_row.addWidget(QLabel("X"))
        xy_row.addWidget(self._spin_x)
        xy_row.addWidget(QLabel("Y"))
        xy_row.addWidget(self._spin_y)
        pos_form.addRow(xy_row)

        wh_row = QHBoxLayout()
        wh_row.addWidget(QLabel("宽"))
        wh_row.addWidget(self._spin_w)
        wh_row.addWidget(QLabel("高"))
        wh_row.addWidget(self._spin_h)
        pos_form.addRow(wh_row)

        self._layout.addWidget(pos_w)

        # 吸附区（仅网格类可见）
        self._snap_container = QWidget()
        snap_lay = QVBoxLayout(self._snap_container)
        snap_lay.setContentsMargins(16, 8, 16, 8)
        snap_lay.setSpacing(6)

        self._btn_snap = QPushButton("自动吸附")
        snap_lay.addWidget(self._btn_snap)
        self._snap_label = QLabel()
        self._snap_label.setWordWrap(True)
        snap_lay.addWidget(self._snap_label)

        self._layout.addWidget(self._snap_container)

        # 弹簧
        self._layout.addStretch(1)

        # 连接位置 spin 信号
        for spin in (self._spin_x, self._spin_y, self._spin_w, self._spin_h):
            spin.valueChanged.connect(self._on_rect_changed)

        self._btn_snap.clicked.connect(self._on_snap)

        # 动态控件引用
        self._spin_n_questions: QSpinBox | None = None
        self._seg_qtype: Segmented | None = None
        self._sub_remove_buttons: list[QPushButton] = []

        # 初始隐藏
        self._header.hide()
        self._params_container.hide()
        self._pos_header.hide()
        self._snap_container.hide()

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
            self._header.hide()
            self._params_container.hide()
            self._pos_header.hide()
            self._snap_container.hide()
            return

        doc = self._doc
        ref = self._ref

        # 标题
        kind_label = KIND_LABELS.get(ref.kind, ref.kind)
        title = doc.title(ref)
        self._header.setText(f"{kind_label} {title}")
        self._header.show()

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
        else:
            self._params_container.hide()

    def _build_objective_params(self, params: dict) -> None:
        """客观题参数。"""
        lay = self._params_layout

        # 题目参数标题
        header = section_header("题目参数")
        lay.addWidget(header)

        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        # 起始题号
        spin_start = _int_spin(params.get("start", 1), 1, 9999)
        spin_start.valueChanged.connect(lambda v: self._emit_params({"start": v}))
        fl.addRow("起始题号", spin_start)

        # 题数
        self._spin_n_questions = _int_spin(params.get("n_questions", 5), 1, 200)
        self._spin_n_questions.valueChanged.connect(
            lambda v: self._emit_params({"n_questions": v}))
        fl.addRow("题数", self._spin_n_questions)

        # 选项数
        spin_opts = _int_spin(params.get("n_options", 4), 2, 8)
        spin_opts.valueChanged.connect(lambda v: self._on_n_options_changed(v))
        fl.addRow("选项数", spin_opts)

        # 每组题数
        spin_group = _int_spin(params.get("group", 0), 0, 50)
        spin_group.valueChanged.connect(lambda v: self._emit_params({"group": v}))
        fl.addRow("每组题数", spin_group)

        lay.addWidget(form)

        # 选项标签
        labels = params.get("labels", ["A", "B", "C", "D"])
        labels_row = QHBoxLayout()
        labels_label = QLabel("选项")
        theme.set_style(labels_label, "color:$INK2; font-size:13px;")
        labels_row.addWidget(labels_label)
        self._label_edits: list[QLineEdit] = []
        for i, lb in enumerate(labels):
            le = QLineEdit(lb)
            le.setMaximumWidth(36)
            le.setAlignment(Qt.AlignmentFlag.AlignCenter)
            le.editingFinished.connect(self._on_labels_changed)
            self._label_edits.append(le)
            labels_row.addWidget(le)
        labels_row.addStretch(1)
        lw = QWidget()
        lw.setLayout(labels_row)
        lay.addWidget(lw)

        # 类型（单选/多选/判断）
        qtype = params.get("qtype", "single")
        self._seg_qtype = Segmented(
            [("single", "单选"), ("multiple", "多选"), ("judge", "判断")], qtype)
        self._seg_qtype.changed.connect(self._on_qtype_changed)
        qtype_row = QHBoxLayout()
        qtype_row.addWidget(QLabel("类型"))
        qtype_row.addWidget(self._seg_qtype)
        qtype_row.addStretch(1)
        qw = QWidget()
        qw.setLayout(qtype_row)
        lay.addWidget(qw)

        # 方向（横排/竖排）
        horiz = params.get("options_horizontal", True)
        self._seg_direction = Segmented(
            [("horizontal", "横排"), ("vertical", "竖排")],
            "horizontal" if horiz else "vertical")
        self._seg_direction.changed.connect(self._on_direction_changed)
        dir_row = QHBoxLayout()
        dir_row.addWidget(QLabel("方向"))
        dir_row.addWidget(self._seg_direction)
        dir_row.addStretch(1)
        dw = QWidget()
        dw.setLayout(dir_row)
        lay.addWidget(dw)

    def _build_exam_no_params(self, params: dict) -> None:
        """考号参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        spin_digits = _int_spin(params.get("n_digits", 10), 1, 30)
        spin_digits.valueChanged.connect(lambda v: self._emit_params({"n_digits": v}))
        fl.addRow("位数", spin_digits)

        horiz = params.get("options_horizontal", True)
        seg = Segmented([("horizontal", "横排"), ("vertical", "竖排")],
                        "horizontal" if horiz else "vertical")
        seg.changed.connect(self._on_direction_changed)
        self._seg_direction = seg
        fl.addRow("方向", seg)

        lay.addWidget(form)

    def _build_omr_field_params(self, params: dict) -> None:
        """自定义填涂字段参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        le_key = QLineEdit(params.get("key", ""))
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        spin_digits = _int_spin(params.get("n_digits", 1), 1, 30)
        spin_digits.valueChanged.connect(lambda v: self._emit_params({"n_digits": v}))
        fl.addRow("位数", spin_digits)

        le_values = QLineEdit(",".join(params.get("labels", [])))
        le_values.editingFinished.connect(
            lambda: self._emit_params({"labels": list(le_values.text().replace(",", ""))}))
        fl.addRow("值", le_values)

        horiz = params.get("options_horizontal", True)
        seg = Segmented([("horizontal", "横排"), ("vertical", "竖排")],
                        "horizontal" if horiz else "vertical")
        seg.changed.connect(self._on_direction_changed)
        self._seg_direction = seg
        fl.addRow("方向", seg)

        lay.addWidget(form)

    def _build_subjective_params(self, params: dict) -> None:
        """主观题参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        le_no = QLineEdit(params.get("no", ""))
        le_no.editingFinished.connect(lambda: self._emit_params({"no": le_no.text()}))
        fl.addRow("题号", le_no)

        le_sub = QLineEdit(params.get("sub", ""))
        le_sub.setPlaceholderText("可空，如 1、(2)、a")
        le_sub.editingFinished.connect(lambda: self._emit_params({"sub": le_sub.text().strip()}))
        fl.addRow("小题号", le_sub)

        seg_qtype = Segmented(_SUBJ_QTYPE, params.get("qtype", "essay"))
        seg_qtype.changed.connect(lambda v: self._emit_params({"qtype": v}))
        fl.addRow("题型", seg_qtype)

        seg_content = Segmented(_SUBJ_CONTENT, params.get("content", "zh"))
        seg_content.changed.connect(lambda v: self._emit_params({"content": v}))
        fl.addRow("内容", seg_content)

        lay.addWidget(form)

        # 区域列表（如果有多个区域）
        if self._ref and self._doc:
            s = self._doc.template.subjective[self._ref.index]
            if len(s.regions) > 0:
                reg_header = QLabel("区域列表")
                theme.set_style(reg_header, "font-weight:600; font-size:13px; color:$INK2;")
                lay.addWidget(reg_header)
                for ri, reg in enumerate(s.regions):
                    row = QHBoxLayout()
                    face_label = "正面" if reg.face == "front" else "背面"
                    row.addWidget(QLabel(f"区域 {ri + 1}（{face_label}）"))
                    row.addStretch(1)
                    sub_ref = RegionRef("subjective", self._ref.index, ri)
                    for delta, icon, tooltip in (
                        (-1, QStyle.StandardPixmap.SP_ArrowUp, "区域上移"),
                        (1, QStyle.StandardPixmap.SP_ArrowDown, "区域下移"),
                    ):
                        move = QPushButton()
                        move.setIcon(self.style().standardIcon(icon))
                        move.setToolTip(tooltip)
                        move.setAccessibleName(tooltip)
                        move.setFixedSize(28, 28)
                        move.setEnabled(0 <= ri + delta < len(s.regions))
                        move.clicked.connect(
                            lambda _c=False, r=sub_ref, dest=ri + delta: self.regionMoveRequested.emit(r, dest))
                        row.addWidget(move)
                    btn_del = QPushButton("删除")
                    btn_del.setObjectName("danger")
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
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        le_key = QLineEdit(params.get("key", ""))
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        questions = params.get("questions", [])
        le_questions = QLineEdit(",".join(questions))
        le_questions.editingFinished.connect(
            lambda: self._emit_params({"questions": [q.strip() for q in le_questions.text().split(",") if q.strip()]}))
        fl.addRow("题号", le_questions)

        lay.addWidget(form)

    def _build_field_params(self, params: dict) -> None:
        """信息栏参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        le_key = QLineEdit(params.get("key", ""))
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        le_label = QLineEdit(params.get("label", ""))
        le_label.editingFinished.connect(lambda: self._emit_params({"label": le_label.text()}))
        fl.addRow("标签", le_label)

        lay.addWidget(form)

    def _build_barcode_params(self, params: dict) -> None:
        """条码参数。"""
        lay = self._params_layout
        form = QWidget()
        fl = QFormLayout(form)
        fl.setContentsMargins(0, 4, 0, 4)
        fl.setSpacing(6)

        le_key = QLineEdit(params.get("key", ""))
        le_key.editingFinished.connect(lambda: self._emit_params({"key": le_key.text()}))
        fl.addRow("键", le_key)

        lay.addWidget(form)

    # ── 信号转发 ──────────────────────────────────────────────

    def _emit_params(self, p: dict) -> None:
        """发出参数变化信号。"""
        if self._suppress or self._ref is None:
            return
        self.paramsChanged.emit(self._ref, p)

    def _on_rect_changed(self) -> None:
        """位置 spin 变化 → 发出 rectChanged。"""
        if self._suppress or self._ref is None:
            return
        new_rect = (
            round(self._spin_x.value(), 1),
            round(self._spin_y.value(), 1),
            round(self._spin_w.value(), 1),
            round(self._spin_h.value(), 1),
        )
        self.rectChanged.emit(self._ref, new_rect)

    def _on_snap(self) -> None:
        """吸附按钮点击。"""
        if self._ref is not None:
            self.snapRequested.emit(self._ref)

    def _on_qtype_changed(self, qtype: str) -> None:
        """客观题类型切换 → 更新标签。"""
        defaults = _DEFAULT_LABELS.get(qtype, list("ABCD"))
        # 更新标签编辑框
        if hasattr(self, "_label_edits"):
            # 重建标签会导致复杂性，直接发出新参数
            pass
        self._emit_params({"qtype": qtype, "labels": defaults[:], "n_options": len(defaults)})

    def _on_n_options_changed(self, n: int) -> None:
        """选项数变化 → 更新标签。"""
        # 当前 qtype
        qtype = "single"
        if self._seg_qtype:
            qtype = self._seg_qtype.value()
        defaults = _DEFAULT_LABELS.get(qtype, list("ABCDEFGH"))
        labels = defaults[:n]
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
