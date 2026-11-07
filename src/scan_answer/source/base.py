"""图像源接口：按顺序产出"一张卡的各面图像"或"设备事件"。

文件夹导入（FolderSource）与扫描仪（计划 4 的 TwainSource）都实现同一接口，流水线不关心来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

import numpy as np


@dataclass
class SheetImages:
    """一张卡的各面。来自文件时 paths 有值（images 可为空，由流水线按需读取）；来自扫描仪时 images 有值。"""
    source: str  # 来源标识：文件名前缀或扫描序号
    names: list[str]  # 每面的文件名（原图另存时沿用）
    dpis: list[float | None]  # 每面的元数据 DPI，未知为 None
    paths: list[Path] = field(default_factory=list)
    images: list[np.ndarray] = field(default_factory=list)


@dataclass
class DeviceEvent:
    """扫描仪上报的事件：double_feed（双张）、paper_jam（卡纸）、no_paper（缺纸）、error。"""
    kind: str
    message: str


SourceItem = SheetImages | DeviceEvent


class ImageSource(Protocol):
    def sheets(self) -> Iterator[SourceItem]:
        ...
