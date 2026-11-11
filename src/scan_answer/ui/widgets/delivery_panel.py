"""Batch-scoped validation and publication actions."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QStandardPaths, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ...output.audit import ISSUE_LABELS, AuditReport, audit_batch
from ...output.delivery import DeliveryError, export_delivery
from ...output.protection import is_sealed_batch
from .. import theme
from .common import ElidedLabel
from .guide import HelpButton

NO_BATCH_TEXT = "打开批次后才能检查"
NOT_CHECKED_TEXT = "还没检查交付"
JUMP_TIP = "切到“核对答卷”，选中这一份看卡面和明细"
PREVIEW_ROWS = 3  # 每组先列出的项数；其余收起，免得“生成交付目录”被挤到看不见的地方
CHECK_TIP = "只检查不写文件：看数据和图片是否齐全；有阻断项会写明去哪里处理"
EXPORT_TIP = "选一个文件夹，生成最终的 JSON + 图片；每次新建目录，不覆盖旧的（生成时会再检查一遍）"
ACTIVE_TIP = "这一批还在扫描中：到扫描页点“结束本轮”或“结束批次”后再"

# 阻断项去哪里处理（只列能给出明确去处的代码）
ISSUE_GUIDE: dict[str, str] = {
    "not_finalized": "到扫描页点“结束批次”完成结算",
    "rejudge_failed": "到扫描页点“结束批次”重新结算；仍失败时核对对应答卷",
    "sheet_unresolved": "到扫描页重扫或作废该答卷",
    "ocr_pending": "在结果页“交付”里点“补做和重试转写”，或“放弃未完成转写…”",
    "ocr_status_invalid": "在结果页“交付”里点“补做和重试转写”重新转写",
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


class _StatusLabel(QLabel):
    """检查结论小标签：短结论不折行，长说明（出错原因、目录问题）折行，不把卡片撑宽。"""

    MAX_WIDTH = 360

    def __init__(self, text: str = ""):
        super().__init__()
        self.setWordWrap(True)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 — 覆盖 Qt 方法
        super().setText(text)
        width = self.fontMetrics().horizontalAdvance(text) + 20  # 加上左右内边距
        self.setMinimumWidth(min(width, self.MAX_WIDTH))

    def changeEvent(self, e) -> None:
        super().changeEvent(e)
        if e.type() == e.Type.FontChange:  # 样式表改了字号后按新字号重算
            self.setText(self.text())


def _stage(title: str) -> tuple[QFrame, QHBoxLayout]:
    """交付清单里的一个阶段卡片：标题行 + 内容。"""
    frame = QFrame()
    frame.setObjectName("stage")
    theme.set_style(frame, "#stage { background:$PANEL; border:1px solid $LINE; border-radius:8px; }"
                           "#stageHead { border-bottom:1px solid $LINE; }")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    head = QWidget()
    head.setObjectName("stageHead")
    head.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    row = QHBoxLayout(head)
    row.setContentsMargins(16, 12, 16, 12)
    row.setSpacing(10)
    t = QLabel(title)
    t.setObjectName("h2")
    row.addWidget(t)
    lay.addWidget(head)
    return frame, row


class DeliveryPanel(QWidget):
    # 不属于当前显示批次的结果（例如导出期间切换了批次）用这个信号报告，由上层显示在提示/状态栏里
    notice = Signal(str)
    # 检查 / 生成结束（结论有更新），上层据此刷新“下一步”提示
    finished = Signal()
    # 结论或按钮状态变了（检查开始 / 结束、过期、换批次），上层据此更新模式徽标
    changed = Signal()
    # 点了某一项的“到核对里看”：序号
    showStudent = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.batch_dir: Path | None = None
        self.active_batch_fn = None
        # 返回不允许放交付目录的位置 [(目录, 说明)]（输出根目录等），由结果页提供
        self.protected_dirs_fn: Callable[[], list[tuple[Path, str]]] | None = None
        # 阻断项的处理按钮：问题代码 -> 按钮列表（只有结果页上已有的动作），由结果页提供
        self.fix_actions_fn: Callable[[str], list[QPushButton]] | None = None
        self.last_report: AuditReport | None = None
        self.last_export: Path | None = None
        self.stale = False  # 检查之后数据有变化：结论已过期
        self._generation = 0
        self._epoch = 0  # 数据变化计数：检查进行中数据变化，结果到达时直接标为过期
        self._busy = False
        self._operation = ""
        self._failed = ""  # 上次检查 / 生成出错时为操作名（check / export）
        self._sealed = False
        self._last_parent: Path | None = None
        self._signals = _Signals(self)
        self._signals.done.connect(self._finished)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)
        # 第一步：检查交付（结论 + 阻断项 / 说明项清单）
        check, row = _stage("先检查交付")
        self.help_btn = HelpButton("delivery")
        row.addWidget(self.help_btn)
        desc = ElidedLabel("只检查不写文件，看数据和图片是否齐全", Qt.TextElideMode.ElideRight)
        theme.set_style(desc, "color:$INK3; font-size:13px;")
        row.addWidget(desc, 1)
        self.status = _StatusLabel(NO_BATCH_TEXT)
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row.addWidget(self.status, 0, Qt.AlignmentFlag.AlignVCenter)
        self.check_btn = QPushButton("检查交付")
        self.check_btn.clicked.connect(self.check)
        row.addWidget(self.check_btn)
        # 检查明细：阻断项必须处理（附处理方法和处理按钮），说明项可以带着交付、会写入交付记录
        self.issues = QWidget()
        self.issues.setObjectName("issues")
        self._issues_box = QVBoxLayout(self.issues)
        self._issues_box.setContentsMargins(16, 6, 16, 12)
        self._issues_box.setSpacing(6)
        self.issues.hide()
        check.layout().addWidget(self.issues)
        layout.addWidget(check)
        # 第二步：生成交付目录（不能点时写明原因）
        export, row = _stage("再生成交付目录")
        row.addStretch(1)
        self.export_why = _StatusLabel("")
        row.addWidget(self.export_why, 0, Qt.AlignmentFlag.AlignVCenter)
        self.export_btn = QPushButton("生成交付目录…")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self._choose_target)
        row.addWidget(self.export_btn)
        exp = QLabel(EXPORT_TIP + "。不要选输出根目录或考试目录。")
        exp.setWordWrap(True)
        theme.set_style(exp, "color:$INK3; font-size:13px; padding:10px 16px 4px 16px;")
        export.layout().addWidget(exp)
        self.export_result = ElidedLabel("")
        theme.set_style(self.export_result, "color:$SUCCESS; font-size:13px; padding:0 16px 12px 16px;")
        self.export_result.hide()
        export.layout().addWidget(self.export_result)
        layout.addWidget(export)
        layout.addStretch(1)
        self._tone(None)
        self._controls()

    @property
    def busy(self) -> bool:
        return self._busy

    def set_batch(self, path: str | Path) -> None:
        self._generation += 1
        self.batch_dir = Path(path).resolve()
        self.last_report = self.last_export = None
        self.stale = False
        self._failed = ""
        self._clear_issues()
        self.export_result.hide()
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
        """检查结论着色：可交付绿色、不可交付红色（带底色的小标签），其余状态用次要正文色。"""
        pill = {"ERR": "background:$ERR_BG; color:$ERR;", "SUCCESS": "background:$NOTICE; color:$SUCCESS;"}
        theme.set_style(self.status, "border-radius:4px; padding:2px 8px; font-size:12px;"
                        + pill.get(role or "", "color:$INK2;"))

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
                return ISSUE_LABELS.get(code, code), ISSUE_GUIDE.get(code, "在“交付”里看检查清单逐项处理")
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
        # 有阻断项时短写成“还有 N 项阻断，处理后重新检查”（明细就在上方清单里），其余原因原样显示
        self.export_why.setText(export_reason.split("：")[0] + "，处理后重新检查" if export_reason.startswith("还有")
                                else export_reason)
        blocked = self.last_report is not None and not self.last_report.ready and not self.stale
        theme.set_style(self.export_why, "font-size:13px;" + ("color:$ERR;" if blocked else "color:$INK3;"))
        self.changed.emit()

    def badge(self) -> tuple[str, str]:
        """模式切换里“交付”旁的徽标：(文字, 颜色角色)。没有批次时为空。"""
        if self.batch_dir is None:
            return "", ""
        if self.busy:
            return ("检查中…" if self._operation == "check" else "生成中…"), ""
        if self._failed:  # 检查 / 生成出错：与结论“未完成：…”一致
            return ("检查失败" if self._failed == "check" else "生成失败"), "ERR"
        if self.last_report is not None:
            if self.stale:
                return "需重新检查", "WARN"
            if self.last_report.ready:
                return "可交付", "SUCCESS"
            blockers = sum(1 for i in self.last_report.issues if i.get("severity") == "error")
            return f"{blockers} 项阻断", "ERR"
        if self.last_export is not None:
            return "已生成", "SUCCESS"
        if self._sealed:
            return "只读", ""
        return "还没检查", ""

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
        self._operation = operation
        self._failed = ""
        self.last_report = None
        self.stale = False
        self._clear_issues()
        self.export_result.hide()
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
        self._failed = ""
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
        self._clear_issues()
        box = self._issues_box
        if errors:
            box.addWidget(self._group(f"阻断项 {len(errors)} · 处理后才能交付", "ERR"))
            by_code: dict[str, list[dict]] = {}
            for issue in errors:
                by_code.setdefault(issue.get("code", ""), []).append(issue)
            for code, items in by_code.items():
                box.addWidget(self._blocker(code, items))
        if notes:
            box.addWidget(self._group(f"说明项 {len(notes)} · 不阻断交付，会写入交付记录", None))
            self._add_rows(box, notes)
        self.issues.setVisible(bool(errors or notes))
        self._controls()

    # ---------- 检查明细 ----------
    def _clear_issues(self) -> None:
        while self._issues_box.count():
            item = self._issues_box.takeAt(0)
            if item.widget():
                item.widget().hide()  # 立即隐藏，免得 deleteLater 生效前和新内容叠在一起
                item.widget().deleteLater()
        self.issues.hide()

    @staticmethod
    def _group(text: str, role: str | None) -> QLabel:
        label = QLabel(text)
        theme.set_style(label, "font-size:13px; font-weight:600; margin-top:8px;"
                        + (f"color:${role};" if role else ""))
        return label

    def _issue_row(self, issue: dict) -> QWidget:
        """一项：序号、内容、“到核对里看”（有序号时）。"""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(8)
        seq = issue.get("seq") if isinstance(issue.get("seq"), int) else None
        tag = QLabel(f"#{seq:04d}" if seq is not None else "整批")
        tag.setFixedWidth(52)
        theme.set_style(tag, "color:$INK3; font-size:13px;")
        lay.addWidget(tag)
        text = QLabel(self._issue_line(issue).strip().removeprefix(f"#{seq:04d} " if seq is not None else ""))
        text.setWordWrap(True)
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        theme.set_style(text, "font-size:13px;")
        lay.addWidget(text, 1)
        if seq is not None:
            jump = QPushButton(f"到核对里看 #{seq:04d}")
            jump.setFlat(True)
            jump.setToolTip(JUMP_TIP)
            theme.set_style(jump, "QPushButton { color:$ACCENT; font-size:13px; padding:2px 6px; }")
            jump.clicked.connect(lambda _c=False, s=seq: self.showStudent.emit(s))
            lay.addWidget(jump)
        return row

    def _add_rows(self, layout: QVBoxLayout, issues: list[dict]) -> None:
        """逐项列出；超过 PREVIEW_ROWS 项时其余先收起，用“展开”按钮显示全部。"""
        rows = [self._issue_row(issue) for issue in issues]
        for row in rows:
            layout.addWidget(row)
        hidden = rows[PREVIEW_ROWS:]
        if not hidden:
            return
        for row in hidden:
            row.hide()
        more = QPushButton()
        more.setFlat(True)
        theme.set_style(more, "QPushButton { color:$ACCENT; font-size:13px; padding:2px 6px; text-align:left; }")

        def toggle(show: bool) -> None:
            for row in hidden:
                row.setVisible(show)
            more.setText("收起" if show else f"展开其余 {len(hidden)} 项")
            more.setToolTip("只看前几项" if show else "这一组还有更多项，点开逐项查看")

        more.clicked.connect(lambda: toggle(hidden[0].isHidden()))
        toggle(False)
        layout.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)

    def _blocker(self, code: str, items: list[dict]) -> QWidget:
        """一类阻断项：逐项内容、处理方法、处理按钮（结果页上已有的动作）。"""
        box = QFrame()
        box.setObjectName("blocker")
        theme.set_style(box, "#blocker { background:$ERR_BG; border:1px solid $ERR; border-radius:6px; }")
        lay = QVBoxLayout(box)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(2)
        self._add_rows(lay, items)
        if code in ISSUE_GUIDE:  # 只写有明确去处的处理方法，没有的不另造
            guide = QLabel(f"处理方法：{ISSUE_GUIDE[code]}")
            guide.setWordWrap(True)
            theme.set_style(guide, "color:$INK2; font-size:13px; margin-left:60px;")
            lay.addWidget(guide)
        actions = self.fix_actions_fn(code) if self.fix_actions_fn else []
        if actions:
            row = QHBoxLayout()
            row.setContentsMargins(60, 4, 0, 0)
            row.setSpacing(6)
            for button in actions:
                row.addWidget(button)
            row.addStretch(1)
            lay.addLayout(row)
        return box

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
            self._failed = operation
            self._controls()
            self._tone("ERR")
        elif operation == "check":
            self._show_report(value)
        else:
            self.last_export = Path(value)
            self.status.setText("已生成交付目录")
            self._tone("SUCCESS")
            self.status.setToolTip(str(value))
            self.export_result.setText(f"已生成：{value}")
            self.export_result.show()
            self._controls()
            return
        if epoch != self._epoch and self.last_report is not None:
            self.mark_stale()
