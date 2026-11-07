"""流水线发给界面（或命令行）的事件。界面层（计划 3）把它们转成 Qt 信号。"""
from __future__ import annotations

from dataclasses import dataclass


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


@dataclass
class ThresholdEvent:
    """扫描中改用已扫部分拟合出的阈值。"""
    mark: float
    uncertain: float
    sheets: int


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


@dataclass
class OcrProgress:
    done: int
    failed: int
    total: int
