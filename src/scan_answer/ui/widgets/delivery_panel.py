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
from .guide import HelpButton

NOT_CHECKED_TEXT = "还没检查交付：核对完后点“检查交付”，看这一批能不能交付"
CHECK_TIP = "只检查不写文件：看数据和图片是否齐全；有阻断项会写明去哪里处理"
EXPORT_TIP = "选一个文件夹，生成最终的 JSON + 图片；每次新建目录，不覆盖旧的（生成时会再检查一遍）"
ACTIVE_TIP = "这一批还在扫描中：到扫描页点“结束本轮”或“结束批次”后再"

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
    # 检查 / 生成结束（结论有更新），上层据此刷新“下一步”提示
    finished = Signal()

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
        title = QLabel("交付")
        title.setStyleSheet("font-weight:600;")
        row.addWidget(title)
        self.help_btn = HelpButton("delivery")
        row.addWidget(self.help_btn)
        row.addSpacing(4)
        self.status = QLabel("打开批次后，在这里检查能不能交付、生成交付目录")
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
        self.details.setToolTip("检查明细：阻断项必须处理（附处理方法），说明项可以带着交付、会写入交付记录")
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
        self.status.setText("这是已生成的交付目录（只读）：可以检查，不能再生成" if self._sealed
                            else NOT_CHECKED_TEXT)
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
        self._controls()

    def _tone(self, role: str | None) -> None:
        """检查结论着色：可交付绿色、不可交付红色，其余状态用正文色。"""
        theme.set_style(self.status, f"color:${role};" if role else "")

    def _active(self) -> bool:
        """当前批次正在扫描页处理（扫描 / 导入 / 结算中）。"""
        try:
            active = self.active_batch_fn() if self.active_batch_fn else None
            return bool(active) and self.batch_dir is not None and Path(active).resolve() == self.batch_dir
        except (OSError, ValueError, TypeError):
            return False

    def check_block_reason(self) -> str:
        """“检查交付”不能点的原因；空串表示可以点。"""
        if self.batch_dir is None:
            return "先打开一个批次"
        if self.busy:
            return "正在检查或生成，请稍候"
        if self._active():
            return ACTIVE_TIP + "检查"
        return ""

    def export_block_reason(self) -> str:
        """“生成交付目录”不能点的原因；空串表示可以点。必须先检查通过且结论没过期。"""
        reason = self.check_block_reason()
        if reason:
            return reason.replace("再检查", "再生成") if reason.startswith(ACTIVE_TIP) else reason
        if self._sealed:
            return "这是已生成的交付目录（只读），不能再生成；要修改请回原工作批次"
        if self.last_report is None:
            if self.last_export is not None:
                return "已生成过一份；要再生成，先重新点“检查交付”"
            return "先点“检查交付”，检查通过后才能生成"
        if self.stale:
            return "数据变了，上次检查结论已过期：先重新点“检查交付”"
        if not self.last_report.ready:
            blockers = sum(1 for i in self.last_report.issues if i.get("severity") == "error")
            return f"还有 {blockers} 项阻断：按下方明细处理后，重新点“检查交付”"
        return ""

    def first_blocker(self) -> tuple[str, str] | None:
        """上次检查的第一类阻断项：(类别名称, 处理方法)。用于页面上的“下一步”提示。"""
        if self.last_report is None or self.stale:
            return None
        for issue in self.last_report.issues:
            if issue.get("severity") == "error":
                code = issue.get("code", "")
                return ISSUE_LABELS.get(code, code), ISSUE_GUIDE.get(code, "按下方明细处理")
        return None

    def update_controls(self) -> None:
        """外部状态变化（扫描开始 / 结束）后重新计算按钮可用性与说明。"""
        self._controls()

    def _controls(self) -> None:
        check_reason = self.check_block_reason()
        self.check_btn.setEnabled(not check_reason)
        self.check_btn.setToolTip(f"不能检查：{check_reason}" if check_reason else CHECK_TIP)
        export_reason = self.export_block_reason()
        self.export_btn.setEnabled(not export_reason)
        self.export_btn.setToolTip(f"不能生成：{export_reason}" if export_reason else EXPORT_TIP)

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
        self._controls()

    def _finished(self, generation: int, payload: tuple) -> None:
        self._busy = False
        self._controls()
        operation, value, *rest = payload
        owner, epoch = (*rest, self.batch_dir, self._epoch)[:2]
        if generation == self._generation:
            # 先更新结论再通知上层：上层的“下一步”提示读取的是最新结论
            self._apply_result(operation, value, owner, epoch)
            self.finished.emit()
            if operation == "export" and not isinstance(value, Exception):
                self.notice.emit(f"批次“{owner.name}”已生成交付目录：{value}")
            return
        # 已切换到别的批次：检查结论作废，但导出结果仍要告诉用户（目录已经生成或失败了）
        if operation == "export":
            name = owner.name if owner is not None else ""
            if isinstance(value, Exception):
                self.notice.emit(f"批次“{name}”的交付目录未生成：{value}")
            else:
                self.notice.emit(f"批次“{name}”已生成交付目录：{value}")

    def _apply_result(self, operation: str, value, owner, epoch: int) -> None:
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
            self._controls()
            return
        if epoch != self._epoch and self.last_report is not None:
            self.mark_stale()
