"""模板数据模型。所有坐标单位为 mm，原点为纸张左上角（以纠偏后的空白底图为准）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Rect = tuple[float, float, float, float]  # x, y, w, h（mm）
FaceName = Literal["front", "back"]


@dataclass
class AlignSpec:
    method: Literal["anchors", "frames"]
    anchors: list[Rect] = field(default_factory=list)  # 定位块矩形
    frames: list[Rect] = field(default_factory=list)  # 定位外框矩形


@dataclass
class Face:
    image: str  # 空白底图文件名（相对模板目录），按 template.dpi 存储
    align: AlignSpec


@dataclass
class OmrGrid:
    """一组填涂格：cells[i][j] 为第 i 组（题/位）第 j 个选项的矩形。"""
    face: FaceName
    labels: list[str]  # 选项标签，如 A..E 或 0..9
    cells: list[list[Rect]]


@dataclass
class ExamNoSpec:
    grid: OmrGrid  # 每位一组，labels = 0..9


@dataclass
class BarcodeSpec:
    key: str
    face: FaceName
    rect: Rect


@dataclass
class FieldSpec:
    key: str
    label: str
    face: FaceName
    rect: Rect


@dataclass
class ObjectiveBlock:
    name: str
    qtype: Literal["single", "multiple", "judge"]
    start: int  # 起始题号
    grid: OmrGrid  # 每题一组


@dataclass
class Region:
    face: FaceName
    rect: Rect


@dataclass
class SubjectiveSpec:
    no: str
    qtype: Literal["fill", "short", "essay"]
    content: Literal["zh", "en", "math"]
    regions: list[Region]


@dataclass
class Template:
    schema_version: str
    name: str
    version: int
    paper: tuple[float, float]  # 宽, 高（mm）
    dpi: int
    faces: dict[str, Face]
    exam_no: ExamNoSpec | None
    absent: Region | None
    barcodes: list[BarcodeSpec]
    fields: list[FieldSpec]
    objective: list[ObjectiveBlock]
    subjective: list[SubjectiveSpec]
    ignore: list[Region] = field(default_factory=list)
