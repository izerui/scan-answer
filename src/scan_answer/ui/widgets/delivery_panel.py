"""Batch-scoped validation and publication actions."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QStandardPaths, Signal
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QWidget)

from ...output.audit import ISSUE_LABELS, AuditReport, audit_batch
from ...output.delivery import DeliveryError, export_delivery
from ...output.protection import is_sealed_batch
from .. import theme

# 阻断项去哪里处理（只列能给出明确去处的代码）
ISSUE_GUIDE: dict[str, str] = {
    "not_finalized": "到扫描页点“结束批次”完成结算",
    "rejudge_failed": "到扫描页点“结束批次”重新结算；仍失败时核对对应答卷",
    "sheet_unresolved": "到扫描页重扫或作废该答卷",
    "ocr_pending": "在结果页点“补做和重试转写”，或“放弃未完成转写…”",
    "ocr_status_invalid": "在结果页点“补做和重试转写”重新转写",
    "file_unreadable": "恢复缺失文件；缺题图可到扫描页结束批次重新结算补建",
    "image_invalid": "恢复损坏图片；原图损坏需重扫该答卷",
    "json_invalid": "从备份恢复该文件",
    "payload_changed": "批次仍在写入，稍后重新检查",
    "empty_batch": "先扫描答卷",
    "template_invalid": "从备份恢复批次的模板快照",
    "template_mismatch": "从备份恢复批次的模板快照",
}


class _Signals(QObject):
    done = Signal(int, object)


class DeliveryPanel(QWidget):
    # 不属于当前显示批次的结果（例如导出期间切换了批次）用这个信号报告，由上层显示在提示/状态栏里
    notice = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.batch_dir: Path | None = None
        self.active_batch_fn = None
        # 返回不允许放交付目录的位置 [(目录, 说明)]（输出根目录等），由结果页提供
        self.protected_dirs_fn: Callable[[], list[tuple[Path, str]]] | None = None
        self.last_report: AuditReport | None = None
        self.last_export: Path | None = None
        self.stale = False  # 检查之后数据有变化：结论已过期
        self._generation = 0
        self._epoch = 0  # 数据变化计数：检查进行中数据变化，结果到达时直接标为过期
        self._busy = False
        self._sealed = False
        self._last_parent: Path | None = None
        self._signals = _Signals(self)
        self._signals.done.connect(self._finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 10)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.status = QLabel("交付尚未检查")
        self.status.setWordWrap(True)
        row.addWidget(self.status, 1)
        self.check_btn = QPushButton("检查交付")
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        self.export_btn = QPushButton("生成交付目录…")
        self.export_btn.clicked.connect(self._choose_target)
        row.addWidget(self.export_btn)
        layout.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        theme.set_style(self.details, "QPlainTextEdit { font-size:12px; color:$INK2; background:$HEADER; }")
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
        self.stale = False
        self.details.clear()
        self.details.hide()
        try:
            self._sealed = is_sealed_batch(self.batch_dir)
        except (OSError, ValueError):
            self._sealed = False
        self.status.setText("固定交付目录（只读）" if self._sealed else "交付尚未检查")
        self.status.setToolTip("")
        self._tone(None)
        self._controls()

    def mark_stale(self) -> None:
        """批次数据变化（转写重试、放弃转写等）：已显示的检查结论不再可信，提示重新检查。"""
        self._epoch += 1
        if self.busy or self.last_report is None or self.stale:
            return
        self.stale = True
        self.status.setText("数据已变化，上次检查结论已过期，请重新检查")
        self.status.setToolTip(f"上次检查时间：{self.last_report.checked_at}")
        self._tone(None)

    def _tone(self, role: str | None) -> None:
        """检查结论着色：可交付绿色、不可交付红色，其余状态用正文色。"""
        theme.set_style(self.status, f"color:${role};" if role else "")

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

    def target_problem(self, target: str | Path) -> str:
        """交付目录不能放在输出根目录 / 考试目录里：考试页会把它当成一个批次列出（还是只读的）。"""
        target = Path(target).resolve()
        places: list[tuple[Path, str]] = []
        if self.batch_dir is not None:
            places.append((self.batch_dir, "源批次"))
            exam = self.batch_dir.parent
            if (exam / "exam.json").is_file():  # 按考试库目录约定存放的批次
                places += [(exam, "考试目录"), (exam.parent, "输出根目录")]
        if self.protected_dirs_fn is not None:
            places += self.protected_dirs_fn()
        for place, label in places:
            try:
                inside = target.is_relative_to(Path(place).resolve())
            except (OSError, ValueError, RuntimeError):
                continue
            if inside:
                return (f"交付目录不能放在{label}（{place}）里：考试页会把它当成批次列出，"
                        "也容易和工作批次混淆。请选择输出根目录以外的文件夹。")
        return ""

    def export_to(self, target: str | Path) -> None:
        if not self._allowed() or self._sealed:
            return
        problem = self.target_problem(target)
        if problem:
            self.status.setText(problem)
            self._tone("ERR")
            return
        self._start("export", Path(target))

    def _default_parent(self) -> str:
        if self._last_parent is not None and self._last_parent.is_dir():
            return str(self._last_parent)
        docs = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        start = Path(docs) if docs and Path(docs).is_dir() else Path.home()
        return str(start) if not self.target_problem(start / "x") else str(Path.home())

    def _choose_target(self) -> None:
        if not self._allowed() or self._sealed:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择交付目录的上级文件夹（不要选输出根目录或考试目录）",
                                                  self._default_parent())
        if not folder:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = Path(folder) / f"{self.batch_dir.name}_delivery_{stamp}"
        problem = self.target_problem(target)
        if problem:
            QMessageBox.warning(self, "不能放在这里", problem)
            return
        self._last_parent = Path(folder)
        self.export_to(target)

    def _start(self, operation: str, target: Path | None = None) -> None:
        owner, generation, epoch = self.batch_dir, self._generation, self._epoch
        self._busy = True
        self.last_report = None
        self.stale = False
        self.details.clear()
        self.details.hide()
        self.status.setText("正在检查交付…" if operation == "check" else "正在生成交付目录…")
        self._tone(None)
        self._controls()

        def run() -> None:
            try:
                value = audit_batch(owner) if operation == "check" else export_delivery(owner, target)
            except Exception as error:
                value = error
            self._signals.done.emit(generation, (operation, value, owner, epoch))

        threading.Thread(target=run, daemon=True, name="delivery").start()

    @staticmethod
    def _issue_line(issue: dict) -> str:
        label = ISSUE_LABELS.get(issue["code"], issue["code"])
        message = str(issue.get("message") or "")
        text = message if message.startswith(label) else f"{label}：{message}" if message else label
        seq = f"#{issue['seq']:04d} " if isinstance(issue.get("seq"), int) else ""
        path = f"（{issue['path']}）" if issue.get("path") and issue["path"] not in message else ""
        return f"  {seq}{text}{path}"

    def _show_report(self, report: AuditReport) -> None:
        self.last_report = report
        self.stale = False
        summary = report.summary
        errors = [i for i in report.issues if i.get("severity") == "error"]
        notes = [i for i in report.issues if i.get("severity") != "error"]
        self.status.setText(
            (f"检查完成：可交付 · {summary.get('ok', 0)} 份"
             + (f" · {len(notes)} 项说明" if notes else ""))
            if report.ready else
            f"暂不可交付 · {len(errors)} 项阻断" + (f" · {len(notes)} 项说明" if notes else "")
        )
        self._tone("SUCCESS" if report.ready else "ERR")
        self.status.setToolTip(f"检查时间：{report.checked_at}；生成目录时会重新检查")
        lines = []
        if errors:
            lines.append(f"阻断项（{len(errors)}，处理后才能交付）：")
            guides = []
            for issue in errors:
                lines.append(self._issue_line(issue))
                guide = ISSUE_GUIDE.get(issue["code"])
                if guide and guide not in guides:
                    guides.append(guide)
            if guides:
                lines.append("处理方法：" + "；".join(guides))
        if notes:
            lines.append(f"说明项（{len(notes)}，不阻断交付，会写入交付记录）：")
            lines.extend(self._issue_line(issue) for issue in notes)
        self.details.setPlainText("\n".join(lines))
        self.details.setVisible(bool(lines))

    def _finished(self, generation: int, payload: tuple) -> None:
        self._busy = False
        self._controls()
        operation, value, *rest = payload
        owner, epoch = (*rest, self.batch_dir, self._epoch)[:2]
        if generation != self._generation:
            # 已切换到别的批次：检查结论作废，但导出结果仍要告诉用户（目录已经生成或失败了）
            if operation == "export":
                name = owner.name if owner is not None else ""
                if isinstance(value, Exception):
                    self.notice.emit(f"批次“{name}”的交付目录未生成：{value}")
                else:
                    self.notice.emit(f"批次“{name}”已生成交付目录：{value}")
            return
        if isinstance(value, DeliveryError):
            self._show_report(value.report)
        elif isinstance(value, Exception):
            self.status.setText(f"未完成：{value}")
            self._tone("ERR")
        elif operation == "check":
            self._show_report(value)
        else:
            self.last_export = Path(value)
            self.status.setText(f"已生成：{value}")
            self._tone("SUCCESS")
            self.status.setToolTip(str(value))
            self.notice.emit(f"批次“{owner.name}”已生成交付目录：{value}")
            return
        if epoch != self._epoch and self.last_report is not None:
            self.mark_stale()
