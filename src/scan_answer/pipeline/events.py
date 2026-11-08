"""流水线发给界面（或命令行）的事件。界面层（计划 3）把它们转成 Qt 信号。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SheetQueued:
    """一张卡已登记、开始识别（界面据此画"识别中"）。"""
    seq: int
    source: str


@dataclass
class SheetEvent:
    """一张卡处理完成（成功或物理异常）。"""
    seq: int
    source: str
    ok: bool
    physical: str | None
    message: str
    exam_no: str | None
    warnings: int
    rescan: bool = False
    result: str | None = None  # 成功时 result.json 的绝对路径


@dataclass
class ThresholdEvent:
    """扫描中改用已扫部分拟合出的阈值。"""
    mark: float
    uncertain: float
    sheets: int
    issues: list[dict] = field(default_factory=list)  # 涂黑度分布异常提示（threshold_issues）


@dataclass
class DeviceNotice:
    """扫描仪事件（双张、卡纸等），原样转发。"""
    kind: str
    message: str
    after_seq: int  # 事件发生时已分配的最大序号


@dataclass
class VoidEvent:
    seq: int


@dataclass
class FinalizedEvent:
    mark: float
    uncertain: float
    changed: int  # 重新判定后结果有变化的卡数
    changed_seqs: list[int] = field(default_factory=list)  # 结果有变化的序号
    issues: list[dict] = field(default_factory=list)  # 涂黑度分布异常提示


@dataclass
class OcrProgress:
    done: int
    failed: int
    total: int
