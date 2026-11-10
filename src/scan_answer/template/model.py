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
    group: int = 0  # 每组题数，仅编辑器重新生成网格时用


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
    sub: str = ""  # 小题号，可为空

    @property
    def key(self) -> str:
        """主观题的唯一键：题号，有小题号时为“题号.小题号”。"""
        return subjective_key(self.no, self.sub)


def subjective_key(no: str, sub: str = "") -> str:
    return f"{no}.{sub}" if sub else str(no)


def subjective_item_key(item: dict) -> str:
    """result.json 中主观题项的唯一键（兼容没有 sub 字段的旧结果）。"""
    return subjective_key(str(item.get("no", "")), str(item.get("sub") or ""))


def safe_name(key: str) -> str:
    """把键转成可用作文件名的形式（只保留字母、数字、点、短横线和下划线）。"""
    return "".join(ch if ch.isalnum() or ch in ".-_" else "_" for ch in key)


@dataclass
class SyncSpec:
    """同步头规格。marks 为同一方向排列的定位标记矩形列表。"""
    face: FaceName
    direction: Literal["vertical", "horizontal"]  # vertical = 同步头沿纸张上下排成一列
    marks: list[Rect]


@dataclass
class OmrFieldSpec:
    """自定义填涂字段（如考场号、AB 卷）。"""
    key: str            # 如 "room"、"paper_type"
    label: str          # 如 "考场号"、"AB 卷"
    grid: OmrGrid       # 每位一组；labels 为候选值


@dataclass
class ChooseOneSpec:
    """选做组规格：学生选择其中一个题号作答。"""
    key: str
    label: str
    face: FaceName
    questions: list[str]   # 题号，与 cells 一一对应
    cells: list[Rect]


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
    sync: list[SyncSpec] = field(default_factory=list)
    omr_fields: list[OmrFieldSpec] = field(default_factory=list)
    choose_one: list[ChooseOneSpec] = field(default_factory=list)
    # 标识区：框住印有科目 / 卷别名称、不会被书写的印刷文字。识别时与底图做局部相关比较，
    # 对不上即判为混卡（WRONG_CARD）。旧模板没有此字段，视为不检查。
    identity: list[Region] = field(default_factory=list)
