"""Shared lazy image and JSON inspection for batch results and trial evidence."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QLabel, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget

from ...output.paths import resolve_path
from .card_view import CardView


NO_IMAGES_TEXT = "这份结果没有可看的图片"
COMBO_TIP = "选择要看的图：整页扫描图、考号 / 信息栏、各题切图；切图下方显示识别值和状态"


class EvidencePreview(QWidget):
    def __init__(self, parent=None, empty_text: str = "还没有结果"):
        super().__init__(parent)
        self.empty_text = empty_text  # 没有结果时图片区和 JSON 区显示的说明
        self._images = None
        self._base: Path | None = None
        self._root: Path | None = None
        self._summaries: dict[str, str] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self.sources = QLabel()
        self.sources.setObjectName("muted")
        self.sources.setTextFormat(Qt.TextFormat.PlainText)
        self.sources.setWordWrap(True)
        self.sources.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.sources)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        images = QWidget()
        il = QVBoxLayout(images)
        il.setContentsMargins(4, 8, 4, 4)
        self.image_combo = QComboBox()
        self.image_combo.setMinimumWidth(0)
        self.image_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.image_combo.setMinimumContentsLength(16)
        self.image_combo.currentIndexChanged.connect(self._select)
        self.image_combo.setEnabled(False)
        self.image_combo.setToolTip(empty_text)
        il.addWidget(self.image_combo)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        il.addWidget(self.summary)
        self.view = CardView(placeholder=empty_text)
        il.addWidget(self.view, 1)
        self.tabs.addTab(images, "切图")
        self.json_view = QPlainTextEdit()
        self.json_view.setReadOnly(True)
        self.json_view.setPlaceholderText(empty_text)
        self.json_view.setToolTip("原始数据：这份答卷完整的识别结果（只读，JSON 格式），可选中复制；交付的 JSON 就是这个格式")
        self.tabs.addTab(self.json_view, "原始数据")
        self.tabs.setTabToolTip(0, "切图：看整页图和各题切图，核对识别是否正确")
        self.tabs.setTabToolTip(1, "原始数据：这份答卷完整的识别结果（JSON 格式，技术人员排查问题时用）")

    def set_result(self, result: dict | None, *, base: Path | None = None,
                   root: Path | None = None, images=None, sources: list[str] | None = None) -> None:
        """Index references without decoding them; only the selected image is read."""
        self._base = Path(base).absolute() if base is not None else None
        self._root = Path(root) if root is not None else self._base
        self._images = images
        self._summaries.clear()
        self.view.clear()
        self.summary.clear()
        self.sources.clear()
        self.json_view.clear()
        self.image_combo.blockSignals(True)
        self.image_combo.clear()
        try:
            labels: dict[str, str] = {}
            r = result or {}
            for face, path in r.get("pages", {}).items():
                labels[path] = {"front": "正面", "back": "背面"}.get(face, face.replace("raw_", "原图 "))
            student = r.get("student") or {}
            entries = [("考号", student.get("exam_no")), ("缺考", student.get("absent"))]
            for group in ("omr_fields", "barcodes", "fields"):
                entries.extend((student.get(group) or {}).items())
            for key, item in entries:
                if isinstance(item, dict) and item.get("image"):
                    path = item["image"]
                    labels[path] = item.get("label") or key
                    self._summaries[path] = self._summary(item)
                if isinstance(item, dict):
                    for index, candidate in enumerate(item.get("candidates") or [], 1):
                        if not isinstance(candidate, dict) or not candidate.get("image"):
                            continue
                        path = candidate["image"]
                        selected = candidate.get("id") == item.get("selected_id")
                        labels[path] = f"{key} · 候选 {index}" + (" · 已采用" if selected else "")
                        self._summaries[path] = self._summary(candidate)
            for kind in ("objective", "subjective"):
                for item in r.get(kind, []):
                    title = f"第 {item['no']} 题"
                    if item.get("sub"):
                        title += f" / {item['sub']}"
                    paths = list(item.get("images") or [])
                    for region in item.get("regions") or []:
                        if region.get("image") and region["image"] not in paths:
                            paths.append(region["image"])
                    if item.get("image") and item["image"] not in paths:
                        paths.insert(0, item["image"])
                    for i, path in enumerate(paths, 1):
                        labels[path] = title + (f" · 区域 {i}" if len(paths) > 1 else "")
                        self._summaries[path] = self._summary(item)
            # Trial images may include diagnostic crops absent from older JSON.
            for path in images if images is not None else ():
                labels.setdefault(path, path)
            for path, label in labels.items():
                self.image_combo.addItem(label, path)
                self.image_combo.setItemData(self.image_combo.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
            origins = []
            for origin in r.get("input_pages") or []:
                name = origin.get("file") or origin.get("source") or origin.get("raw") or origin.get("kind", "")
                page = origin.get("page")
                origins.append(f"{name} · 第 {page} 页" if page is not None else str(name))
            self.sources.setText("\n".join(origins or sources or []))
            if result is not None:
                self.json_view.setPlainText(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            self.image_combo.blockSignals(False)
        has_images = self.image_combo.count() > 0
        self.image_combo.setEnabled(has_images)
        self.image_combo.setToolTip(COMBO_TIP if has_images else
                                    self.empty_text if result is None and images is None else NO_IMAGES_TEXT)
        self.view.set_placeholder(self.empty_text if result is None and images is None else
                                  "图片缺失或无法读取" if has_images else NO_IMAGES_TEXT)
        self._select()

    def select_image(self, path: str) -> None:
        index = self.image_combo.findData(path)
        self.image_combo.setCurrentIndex(index)
        self.tabs.setCurrentIndex(0)

    @staticmethod
    def _summary(item: dict) -> str:
        parts = []
        for key, label in (("answer", "答案"), ("value", "识别值"), ("status", "状态"),
                           ("ocr_status", "转写状态"), ("text", "转写")):
            if key in item:
                value = item[key]
                if isinstance(value, list):
                    value = "、".join(str(v) for v in value)
                if key == "status":
                    value = {"ok": "正常", "blank": "未答", "multiple": "多涂",
                             "uncertain": "存疑", "partial": "部分", "failed": "失败",
                             "ambiguous": "候选有歧义"}.get(value, value)
                elif key == "ocr_status":
                    value = {"none": "已放弃转写" if item.get("ocr_skipped") else "未开启转写",
                             "pending": "等待", "done": "完成", "failed": "失败"}.get(value, value)
                parts.append(f"{label}：{value if value is not None else '未确定'}")
        return "　".join(parts)

    def _select(self, *_args) -> None:
        path = self.image_combo.currentData()
        self.view.clear()
        loaded = False
        error = "图片缺失或无法读取"
        if path:
            if self._images is not None:
                loaded = self.view.set_array(self._images.get(path))
            elif self._base is not None:
                try:
                    image = resolve_path(self._root, self._base.resolve(), path)
                except (ValueError, OSError, RuntimeError):
                    error = "图片路径无效"
                else:
                    # 文件图片在后台解码，不卡界面；读不到时再补上提示
                    summary = self._summaries.get(path, "")
                    self.summary.setText(summary)

                    def shown(ok: bool) -> None:
                        if not ok:
                            self.summary.setText(f"{summary}　{error}".strip())

                    self.view.load_image(image, shown)
                    return
        summary = self._summaries.get(path, "")
        if path and not loaded:
            summary = f"{summary}　{error}".strip()
        self.summary.setText(summary)
