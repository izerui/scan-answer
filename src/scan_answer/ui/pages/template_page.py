"""模板页（本版本）：列出可用模板、添加模板目录。可视化模板编辑器在下一个计划中替换本页。"""
from __future__ import annotations

from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from PySide6.QtCore import Signal

from ...app.library import list_templates
from ...app.settings import AppSettings, save_settings
from .. import theme


class TemplatePage(QWidget):
    changed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        body = QWidget()
        body.setObjectName("work")
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(body)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(28, 24, 28, 24)
        h = QLabel("模板")
        h.setObjectName("h1")
        lay.addWidget(h)
        note = QLabel("可视化模板编辑器将在下一版本提供。当前可使用随程序提供的模板，或添加包含 template.json 的模板目录。")
        note.setStyleSheet(f"color:{theme.INK2};")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["名称", "版本", "纸张（mm）", "面数", "题数", "目录"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.setShowGrid(False)
        lay.addWidget(self.table, 1)
        acts = QHBoxLayout()
        add = QPushButton("添加模板目录…")
        add.clicked.connect(self._add)
        acts.addWidget(add)
        acts.addStretch(1)
        lay.addLayout(acts)
        self.refresh()

    def refresh(self) -> None:
        ts = list_templates(self.settings.template_dirs)
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
