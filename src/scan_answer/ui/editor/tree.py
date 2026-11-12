"""区域树：按类别分组列出当前面的区域，支持选中与信号通知。"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem

from ...template.doc import RegionRef, TemplateDoc
from .. import theme

EMPTY_TEXT = ("当前面还没有区域。\n\n1. 先确认定位：在右侧“自动定位”里找定位点或外框，"
              "找不到就手动画定位块。\n2. 再在左侧的“添加区域”工具列选区域类型，在画布上拖出框。")

# 分组定义：(组名, 包含的 kind 列表, 色码键)
_GROUPS = [
    ("定位", ["anchor", "frame", "sync"], "loc"),
    ("考号与填涂字段", ["exam_no", "omr_field"], "id"),
    ("条码", ["barcode"], "bar"),
    ("信息栏 / 缺考", ["field", "absent"], "info"),
    ("客观题", ["objective"], "obj"),
    ("主观题", ["subjective"], "sub"),
    ("选做组", ["choose_one"], "sub"),
    ("忽略区", ["ignore"], "info"),
    ("标识区（混卡检测）", ["identity"], "loc"),
]


def _summary(doc: TemplateDoc, ref: RegionRef) -> str:
    """子行右侧摘要文本。"""
    t = doc.template
    if ref.kind == "objective":
        blk = t.objective[ref.index]
        total = sum(len(row) for row in blk.grid.cells)
        return f"{total} 框"
    if ref.kind == "subjective":
        s = t.subjective[ref.index]
        faces = {r.face for r in s.regions}
        if len(faces) > 1:
            return "正 + 背"
        face = next(iter(faces))
        return "正面" if face == "front" else "背面"
    if ref.kind == "barcode":
        return "条码"
    if ref.kind == "exam_no":
        n = len(t.exam_no.grid.cells)
        return f"{n} 位"
    if ref.kind == "field":
        return "切图"
    if ref.kind == "absent":
        return "单框"
    if ref.kind == "anchor":
        return ""
    if ref.kind == "frame":
        return ""
    if ref.kind == "sync":
        return ""
    if ref.kind == "omr_field":
        of = t.omr_fields[ref.index]
        n = len(of.grid.cells)
        return f"{n} 位"
    if ref.kind == "choose_one":
        ch = t.choose_one[ref.index]
        return f"{len(ch.questions)} 题"
    if ref.kind == "ignore":
        return ""
    if ref.kind == "identity":
        return "比对底图"
    return ""


class RegionTree(QTreeWidget):
    """区域树面板。"""

    # 信号：选中的 RegionRef 列表
    regionSelectionChanged = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._doc: TemplateDoc | None = None
        self._face: str = "front"
        self._ref_map: dict[int, RegionRef] = {}  # QTreeWidgetItem id → RegionRef
        self._suppress_signal = False

        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setRootIsDecorated(True)
        self.setIndentation(16)
        self.setSelectionMode(QTreeWidget.SelectionMode.ExtendedSelection)
        self.setObjectName("regionTree")

        # 样式（QSS 用对象名选择器）
        theme.set_style(self, """
            QTreeWidget#regionTree {
                background: $PANEL;
                border: none;
                outline: none;
            }
            QTreeWidget#regionTree::item {
                padding: 3px 4px;
                border: none;
            }
            QTreeWidget#regionTree::item:selected {
                background: $SELECTION;
                color: $INK;
            }
        """)

        # 空状态说明（当前面没有任何区域时显示）
        self.empty_label = QLabel(EMPTY_TEXT, self.viewport())
        self.empty_label.setObjectName("muted")
        self.empty_label.setWordWrap(True)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.empty_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.empty_label.hide()

        # 连接内部选中变化
        self.itemSelectionChanged.connect(self._on_selection_changed)

    def _place_empty(self) -> None:
        r = self.viewport().rect().adjusted(16, 12, -16, -12)
        self.empty_label.setGeometry(r)

    def resizeEvent(self, event) -> None:  # noqa: N802 — Qt 方法名
        super().resizeEvent(event)
        self._place_empty()

    def set_doc(self, doc: TemplateDoc, face: str) -> None:
        """设置文档和面，刷新树。"""
        self._doc = doc
        self._face = face
        self.refresh()

    def refresh(self) -> None:
        """重建树结构。"""
        self._suppress_signal = True
        self.clear()
        self._ref_map.clear()

        if self._doc is None:
            self._suppress_signal = False
            self._update_empty()
            return

        doc = self._doc
        face = self._face
        all_refs = doc.regions(face)

        # 按 kind 归类
        refs_by_kind: dict[str, list[RegionRef]] = {}
        for ref in all_refs:
            refs_by_kind.setdefault(ref.kind, []).append(ref)

        # 主观题特殊处理：同一 subjective index 的多个区域只显示一行
        # 但 brief 说两面都会出现，所以列出当前面的即可
        # 对主观题按 (index) 去重，显示一行
        subj_seen: dict[int, list[RegionRef]] = {}
        if "subjective" in refs_by_kind:
            for ref in refs_by_kind["subjective"]:
                subj_seen.setdefault(ref.index, []).append(ref)

        for group_name, kinds, color_key in _GROUPS:
            # 收集该组的 refs
            group_refs: list[RegionRef] = []
            for k in kinds:
                if k == "subjective":
                    # 主观题去重后的 refs（取每个 index 的第一个）
                    for idx_refs in subj_seen.values():
                        group_refs.append(idx_refs[0])
                else:
                    group_refs.extend(refs_by_kind.get(k, []))

            if not group_refs:
                continue

            # 创建分组行
            group_item = QTreeWidgetItem(self)
            group_item.setText(0, group_name)
            group_item.setText(1, str(len(group_refs)))
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)

            theme.set_item_colors(group_item, foreground=f"REGION_TEXT_{color_key}", column=0)

            # 展开
            group_item.setExpanded(True)

            # 子行
            for ref in group_refs:
                child = QTreeWidgetItem(group_item)
                title = doc.title(ref)
                child.setText(0, title)
                summary = _summary(doc, ref)
                child.setText(1, summary)

                # 存储 ref 映射
                self._ref_map[id(child)] = ref

        self.resizeColumnToContents(0)
        self.resizeColumnToContents(1)
        self._suppress_signal = False
        self._update_empty()

    def _update_empty(self) -> None:
        self.empty_label.setVisible(self._doc is not None and self.topLevelItemCount() == 0)
        self._place_empty()

    def select(self, refs: list[RegionRef]) -> None:
        """程序化选中。"""
        self._suppress_signal = True
        self.clearSelection()
        ref_set = set(refs)
        for item_id, ref in self._ref_map.items():
            if ref in ref_set:
                # 找到对应的 QTreeWidgetItem
                for i in range(self.topLevelItemCount()):
                    group = self.topLevelItem(i)
                    for j in range(group.childCount()):
                        child = group.child(j)
                        if id(child) == item_id:
                            child.setSelected(True)
        self._suppress_signal = False

    def _on_selection_changed(self) -> None:
        """内部选中变化 → 发出信号。"""
        if self._suppress_signal:
            return
        refs = []
        for item in self.selectedItems():
            ref = self._ref_map.get(id(item))
            if ref is not None:
                refs.append(ref)
        self.regionSelectionChanged.emit(refs)
