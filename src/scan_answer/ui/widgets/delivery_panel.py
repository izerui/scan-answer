"""Batch-scoped validation and publication actions."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QStyle, QVBoxLayout, QWidget

from ...output.audit import ISSUE_LABELS, AuditReport, audit_batch
from ...output.delivery import DeliveryError, export_delivery
from ...output.protection import is_sealed_batch


class _Signals(QObject):
    done = Signal(int, object)


class DeliveryPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.batch_dir: Path | None = None
        self.active_batch_fn = None
        self.last_report: AuditReport | None = None
        self.last_export: Path | None = None
        self._generation = 0
        self._busy = False
        self._sealed = False
        self._signals = _Signals(self)
        self._signals.done.connect(self._finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 8, 24, 8)
        row = QHBoxLayout()
        self.status = QLabel("交付尚未检查")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.check_btn = QPushButton("检查交付")
        self.check_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogApplyButton))
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        self.export_btn = QPushButton("生成交付目录…")
        self.export_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton))
        self.export_btn.clicked.connect(self._choose_target)
        row.addWidget(self.export_btn)
        layout.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(96)
        self.details.hide()
        layout.addWidget(self.details)
        self._controls()

    @property
    def busy(self) -> bool:
        return self._busy

    def set_batch(self, path: str | Path) -> None:
        self._generation += 1
        self.batch_dir = Path(path).resolve()
        self.last_report = self.last_export = None
        self.details.clear()
        self.details.hide()
        try:
            self._sealed = is_sealed_batch(self.batch_dir)
        except (OSError, ValueError):
            self._sealed = False
        self.status.setText("固定交付目录（只读）" if self._sealed else "交付尚未检查")
        self.status.setToolTip("")
        self._controls()

    def _controls(self) -> None:
        enabled = self.batch_dir is not None and not self.busy
        self.check_btn.setEnabled(enabled)
        self.export_btn.setEnabled(enabled and not self._sealed)

    def _allowed(self) -> bool:
        if self.batch_dir is None or self.busy:
            return False
        active = self.active_batch_fn() if self.active_batch_fn else None
        if active and Path(active).resolve() == self.batch_dir:
            self.status.setText("批次正在处理，请在本轮结束后检查交付")
            return False
        return True

    def check(self) -> None:
        if self._allowed():
            self._start("check")

    def export_to(self, target: str | Path) -> None:
        if self._allowed() and not self._sealed:
            self._start("export", Path(target))

    def _choose_target(self) -> None:
        if not self._allowed() or self._sealed:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择交付目录的上级文件夹")
        if folder:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self.export_to(Path(folder) / f"{self.batch_dir.name}_delivery_{stamp}")

    def _start(self, operation: str, target: Path | None = None) -> None:
        owner, generation = self.batch_dir, self._generation
        self._busy = True
        self.last_report = None
        self.details.clear()
        self.details.hide()
        self.status.setText("正在检查交付…" if operation == "check" else "正在生成交付目录…")
        self._controls()

        def run() -> None:
            try:
                value = audit_batch(owner) if operation == "check" else export_delivery(owner, target)
            except Exception as error:
                value = error
            self._signals.done.emit(generation, (operation, value))

        threading.Thread(target=run, daemon=True, name="delivery").start()

    def _show_report(self, report: AuditReport) -> None:
        self.last_report = report
        summary = report.summary
        self.status.setText(
            (f"检查完成：可交付 · {summary.get('ok', 0)} 份"
             + (f" · {summary['warnings']} 项说明" if summary.get("warnings") else ""))
            if report.ready else f"暂不可交付 · {summary.get('errors', 0)} 项待处理"
        )
        self.status.setToolTip(f"检查时间：{report.checked_at}；生成目录时会重新检查")
        lines = []
        for issue in report.issues:
            label = ISSUE_LABELS.get(issue["code"], issue["message"])
            seq = f"#{issue['seq']:04d} " if isinstance(issue.get("seq"), int) else ""
            path = f" · {issue['path']}" if issue.get("path") else ""
            lines.append(f"{seq}{label}{path}")
        self.details.setPlainText("\n".join(lines))
        self.details.setVisible(bool(lines))

    def _finished(self, generation: int, payload: tuple) -> None:
        self._busy = False
        self._controls()
        if generation != self._generation:
            return
        operation, value = payload
        if isinstance(value, DeliveryError):
            self._show_report(value.report)
        elif isinstance(value, Exception):
            self.status.setText(f"未完成：{value}")
        elif operation == "check":
            self._show_report(value)
        else:
            self.last_export = Path(value)
            self.status.setText(f"已生成：{value}")
            self.status.setToolTip(str(value))
