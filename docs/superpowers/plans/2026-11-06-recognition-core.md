# 识别核心（第 1 期 · 计划 1/4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 做出不依赖界面和扫描仪的识别核心：模板模型、199 卡模板、模拟测试集生成器、识别引擎（定位/方向/OMR/条码/切图）、本地 JSON 输出、命令行 `scan-answer synth | recognize | eval`，并用 200 张模拟卡的回归测试守住验收指标。

**Architecture:** `src/scan_answer/` 下按职责分包：`template/`（数据模型与读写）、`engine/`（纯函数识别：align → recognize → result）、`synth/`（以真实空白卡为底图生成模拟扫描图与标准答案）、`output/`（本地目录导出器）；`batch.py` 用“两遍”流程（先算全部涂黑度拟合批次阈值，再判定与写出）；`evaluate.py` 把结果与 truth.json 对比。所有坐标以 mm 为单位，单应矩阵 H 统一表示“图像像素 → 模板 mm”。

**Tech Stack:** Python 3.14、NumPy、OpenCV（opencv-python-headless 5.x）、zxing-cpp、Pillow；开发依赖 pytest、PyMuPDF（只用于把样卡 PDF 渲染成底图）。包管理用 uv。

**设计文档:** `docs/superpowers/specs/2026-11-06-scan-answer-design.md`（本计划覆盖其第 4、5、6、9 节的非界面部分）

**后续计划（不在本计划内）:** 计划 2 流水线与 OCR（进程池、`FolderSource`、重扫接续、OCR 队列）；计划 3 PySide6 界面；计划 4 TWAIN 扫描仪接入与 Windows 打包（含核实 Windows 下 cp314 wheel）。

## Global Constraints

- Python `>=3.14`；只在工程 `.venv` 里安装依赖：`uv pip install --python .venv -e ".[dev]"`；运行测试 `.venv/bin/python -m pytest`
- 运行依赖仅限：`numpy>=2.3`、`opencv-python-headless>=5.0`、`zxing-cpp>=3.0`、`pillow>=12.0`；开发依赖：`pytest>=9.0`、`pymupdf>=1.26`
- 坐标单位 mm，原点为纸张左上角（纠偏后的空白底图）；模板底图 300 DPI；识别工作图 200 DPI；切图按扫描图实际分辨率输出
- 识别引擎不得依赖界面或扫描仪；`engine/` 中的函数只接收 numpy 图像和模板对象
- JSON 写入一律“临时文件 + `os.replace`”原子替换；中文不转义（`ensure_ascii=False`）
- `result.json` 的 `schema_version` 为 `"1.0"`；`status` 取值 `ok/blank/multiple/uncertain/partial/failed`；`ocr_status` 本期固定 `"none"`；`manual` 固定 `null`
- 物理异常代码：`ALIGN_FAILED`、`FACE_MISMATCH`、`BLANK_PAGE`、`MISSING_FACE`、`SKEW_TOO_LARGE`
- 数据警告代码：`EXAM_NO_PARTIAL`、`EXAM_NO_BLANK`、`ABSENT_MARKED`、`OBJ_BLANK`、`OBJ_MULTIPLE`、`OBJ_UNCERTAIN`、`ID_MISMATCH`、`BARCODE_MISSING`、`ALIGN_FALLBACK`
- 验收指标（回归测试）：正常填涂准确率 ≥ 99.9%、客观题整体 ≥ 99.9%、静默错误 0、考号逐位 ≥ 99.9%、物理异常检出 100% 且误报 0
- 代码注释、提示信息用中文；标识符用英文

## 文件结构

```
pyproject.toml                         # 依赖、脚本入口、pytest 配置
src/scan_answer/
  __init__.py
  template/
    __init__.py                        # 对外导出模型与读写函数
    model.py                           # 模板数据类（Rect 为 mm 元组）
    layout.py                          # grid_cells：按规则批量生成填涂格
    io.py                              # template.json 读写 + 加载底图
  engine/
    __init__.py
    align.py                           # 定位块 / 外框 / 特征匹配对齐，warp 与裁图
    omr.py                             # 涂黑度、局部微调、判定、批次阈值
    barcode.py                         # zxing-cpp 读码
    recognize.py                       # 方向与正反面、analyze_sheet、decide_sheet、物理异常
    result.py                          # 组装 result.json、输出切图
  synth/
    __init__.py
    draw.py                            # 铅笔涂卡、手写、贴条码
    scan.py                            # 模拟扫描扰动、透印、折角
    generate.py                        # make_sheet / make_scans / generate_dataset
  output/
    __init__.py
    local.py                           # LocalExporter：students/ errors/ manifest.json
  batch.py                             # 文件夹批量识别（两遍流程）
  evaluate.py                          # 与 truth.json 对比的指标
  cli.py                               # scan-answer synth|recognize|eval
tools/
  build_template_199.py                # 从样卡 PDF 生成 templates/guanzong199
  overlay_template.py                  # 把模板区域画到底图上，人工核对
templates/guanzong199/                 # template.json + front.png + back.png（由工具生成并提交）
tests/                                 # 见各任务
docs/output-format.md                  # 输出格式与代码表
```

---

### Task 1: 工程骨架与依赖

**Files:**
- Modify: `pyproject.toml`（整文件替换）
- Modify: `.gitignore`（追加）
- Create: `src/scan_answer/__init__.py`、`src/scan_answer/engine/__init__.py`、`src/scan_answer/synth/__init__.py`、`src/scan_answer/output/__init__.py`（均为空文件）
- Create: `tests/conftest.py`、`tests/test_smoke.py`

**Interfaces:**
- Produces: 包 `scan_answer` 可导入；pytest fixture `tpl_bases`（`(Template, dict[str, ndarray])`）与 `ctx`（`TemplateContext`），常量 `tests/conftest.py::TEMPLATE_DIR`。fixture 内部延迟导入，后续任务完成前不会报错。

- [ ] **Step 1: 写冒烟测试**

`tests/test_smoke.py`:
```python
def test_imports():
    import cv2
    import numpy
    import zxingcpp
    from PIL import Image

    import scan_answer

    assert int(cv2.__version__.split(".")[0]) >= 5
    assert scan_answer.__name__ == "scan_answer"
```

`tests/conftest.py`:
```python
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates" / "guanzong199"


@pytest.fixture(scope="session")
def tpl_bases():
    from scan_answer.template import load_template
    return load_template(TEMPLATE_DIR)


@pytest.fixture(scope="session")
def ctx(tpl_bases):
    from scan_answer.engine.recognize import TemplateContext
    t, bases = tpl_bases
    return TemplateContext(t, bases)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `.venv/bin/python -m pytest tests/test_smoke.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'pytest'` 或 `scan_answer`）

- [ ] **Step 3: 写 pyproject.toml 与空包**

`pyproject.toml`（整文件替换）:
```toml
[project]
name = "scan-answer"
version = "0.1.0"
description = "答题卡扫描识别"
requires-python = ">=3.14"
dependencies = [
    "numpy>=2.3",
    "opencv-python-headless>=5.0",
    "zxing-cpp>=3.0",
    "pillow>=12.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=9.0",
    "pymupdf>=1.26",
]

[project.scripts]
scan-answer = "scan_answer.cli:main"

[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
testpaths = ["tests"]
pythonpath = ["tests"]
markers = ["slow: 完整回归测试（200 张模拟卡，约 4 分钟）"]
addopts = "-m 'not slow'"
```

`.gitignore` 追加：
```
*.egg-info/
.pytest_cache/
```

创建空文件：
```bash
mkdir -p src/scan_answer/engine src/scan_answer/synth src/scan_answer/output src/scan_answer/template tests tools
touch src/scan_answer/__init__.py src/scan_answer/engine/__init__.py src/scan_answer/synth/__init__.py src/scan_answer/output/__init__.py
```

安装：
```bash
uv pip install --python .venv -e ".[dev]"
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python -m pytest tests/test_smoke.py -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "chore: 识别核心工程骨架与依赖"
```

---

### Task 2: 模板数据模型、布局生成与读写

**Files:**
- Create: `src/scan_answer/template/model.py`、`src/scan_answer/template/layout.py`、`src/scan_answer/template/io.py`、`src/scan_answer/template/__init__.py`
- Test: `tests/test_layout.py`、`tests/test_template_io.py`

**Interfaces:**
- Produces:
  - `Rect = tuple[float, float, float, float]`（x, y, w, h，mm）
  - 数据类 `AlignSpec(method: "anchors"|"frames", anchors: list[Rect], frames: list[Rect])`、`Face(image: str, align: AlignSpec)`、`OmrGrid(face, labels: list[str], cells: list[list[Rect]])`、`ExamNoSpec(grid)`、`BarcodeSpec(key, face, rect)`、`FieldSpec(key, label, face, rect)`、`ObjectiveBlock(name, qtype, start: int, grid)`、`Region(face, rect)`、`SubjectiveSpec(no: str, qtype, content, regions: list[Region])`、`Template(schema_version, name, version, paper: tuple[float,float], dpi, faces: dict[str, Face], exam_no, absent, barcodes, fields, objective, subjective, ignore)`
  - `grid_cells(first_center, n_groups, n_options, option_pitch, group_pitch, cell_size, options_horizontal=True, block_every=0, block_gap=0.0) -> list[list[Rect]]`
  - `load_template(dir) -> (Template, dict[str, np.ndarray])`（底图为灰度）、`save_template(t, dir)`、`template_from_dict(d)`（`schema_version != "1.0"` 抛 `ValueError`）、`template_to_dict(t)`

- [ ] **Step 1: 写失败测试**

`tests/test_layout.py`:
```python
from scan_answer.template.layout import grid_cells


def test_grid_cells_horizontal_options_with_block_gap():
    cells = grid_cells((10.0, 20.0), n_groups=6, n_options=3, option_pitch=5.0, group_pitch=4.0,
                       cell_size=(2.0, 1.0), options_horizontal=True, block_every=5, block_gap=3.0)
    assert len(cells) == 6 and len(cells[0]) == 3
    assert cells[0][0] == (9.0, 19.5, 2.0, 1.0)
    assert cells[0][2] == (19.0, 19.5, 2.0, 1.0)  # 第 3 个选项右移 2*5
    assert cells[4][0][1] == 19.5 + 4 * 4.0
    assert cells[5][0][1] == 19.5 + 5 * 4.0 + 3.0  # 第 6 组多出 block_gap


def test_grid_cells_vertical_options():
    cells = grid_cells((0.0, 0.0), n_groups=2, n_options=10, option_pitch=3.0, group_pitch=5.0,
                       cell_size=(2.0, 2.0), options_horizontal=False)
    assert cells[1][9] == (4.0, 26.0, 2.0, 2.0)  # 第 2 位、数字 9：x=5，y=27
```

`tests/test_template_io.py`:
```python
import json

import cv2
import numpy as np
import pytest

from scan_answer.template import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid,
                                  Region, SubjectiveSpec, Template, load_template, save_template,
                                  template_from_dict, template_to_dict)
from scan_answer.template.layout import grid_cells


def tiny() -> Template:
    cells = grid_cells((20.0, 30.0), 2, 4, 6.0, 4.0, (3.8, 2.4))
    return Template(
        schema_version="1.0", name="小卡", version=2, paper=(100.0, 80.0), dpi=300,
        faces={"front": Face(image="front.png", align=AlignSpec(method="anchors", anchors=[(5, 5, 4, 3), (90, 5, 4, 3), (90, 70, 4, 3), (5, 70, 4, 3)]))},
        exam_no=ExamNoSpec(grid=OmrGrid(face="front", labels=list("0123456789"),
                                        cells=grid_cells((60.0, 20.0), 3, 10, 3.6, 5.1, (3.6, 2.2), options_horizontal=False))),
        absent=Region(face="front", rect=(10.0, 60.0, 4.6, 2.5)),
        barcodes=[BarcodeSpec(key="student_barcode", face="front", rect=(10.0, 10.0, 40.0, 15.0))],
        fields=[FieldSpec(key="name", label="姓名", face="front", rect=(10.0, 40.0, 30.0, 10.0))],
        objective=[ObjectiveBlock(name="1-2", qtype="single", start=1, grid=OmrGrid(face="front", labels=list("ABCD"), cells=cells))],
        subjective=[SubjectiveSpec(no="3", qtype="short", content="zh", regions=[Region(face="front", rect=(50.0, 50.0, 40.0, 20.0))])],
    )


def test_round_trip_and_load(tmp_path):
    t = tiny()
    save_template(t, tmp_path)
    cv2.imwrite(str(tmp_path / "front.png"), np.full((945, 1181), 250, np.uint8))
    again, bases = load_template(tmp_path)
    assert again == t
    assert bases["front"].shape == (945, 1181)


def test_rejects_unknown_schema():
    d = template_to_dict(tiny())
    d["schema_version"] = "9.9"
    with pytest.raises(ValueError):
        template_from_dict(d)


def test_missing_base_image(tmp_path):
    save_template(tiny(), tmp_path)
    with pytest.raises(FileNotFoundError):
        load_template(tmp_path)
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_layout.py tests/test_template_io.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.template'`）

- [ ] **Step 3: 实现**

`src/scan_answer/template/model.py`:
```python
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
```

`src/scan_answer/template/layout.py`:
```python
"""按规则批量生成填涂格坐标（模板编辑器“框整块、填参数”也用这个函数）。"""
from __future__ import annotations

from .model import Rect


def grid_cells(
    first_center: tuple[float, float],
    n_groups: int,
    n_options: int,
    option_pitch: float,
    group_pitch: float,
    cell_size: tuple[float, float],
    options_horizontal: bool = True,
    block_every: int = 0,
    block_gap: float = 0.0,
) -> list[list[Rect]]:
    """生成 n_groups 组、每组 n_options 个格子。

    options_horizontal=True：同一组的选项横向排列，组（题）纵向排列；False 则相反。
    block_every>0 时，每 block_every 组额外增加 block_gap 的间距（例如每 5 题空一行）。
    返回 cells[group][option] = (x, y, w, h)，以格子中心对称展开。
    """
    cx0, cy0 = first_center
    w, h = cell_size
    cells: list[list[Rect]] = []
    for g in range(n_groups):
        extra = (g // block_every) * block_gap if block_every else 0.0
        row: list[Rect] = []
        for o in range(n_options):
            if options_horizontal:
                cx = cx0 + o * option_pitch
                cy = cy0 + g * group_pitch + extra
            else:
                cx = cx0 + g * group_pitch + extra
                cy = cy0 + o * option_pitch
            row.append((round(cx - w / 2, 3), round(cy - h / 2, 3), w, h))
        cells.append(row)
    return cells
```

`src/scan_answer/template/io.py`:
```python
"""模板 JSON 读写。模板目录结构：template.json + 各面空白底图。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np

from .model import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid, Region,
                    SubjectiveSpec, Template)

SCHEMA_VERSION = "1.0"


def _rect(v) -> tuple[float, float, float, float]:
    x, y, w, h = v
    return (float(x), float(y), float(w), float(h))


def _grid(d: dict) -> OmrGrid:
    return OmrGrid(face=d["face"], labels=list(d["labels"]), cells=[[_rect(c) for c in row] for row in d["cells"]])


def template_from_dict(d: dict) -> Template:
    if d.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"不支持的模板版本: {d.get('schema_version')}")
    faces = {
        k: Face(image=v["image"], align=AlignSpec(method=v["align"]["method"],
                                                  anchors=[_rect(r) for r in v["align"].get("anchors", [])],
                                                  frames=[_rect(r) for r in v["align"].get("frames", [])]))
        for k, v in d["faces"].items()
    }
    return Template(
        schema_version=d["schema_version"], name=d["name"], version=int(d["version"]),
        paper=(float(d["paper"][0]), float(d["paper"][1])), dpi=int(d["dpi"]), faces=faces,
        exam_no=ExamNoSpec(grid=_grid(d["exam_no"]["grid"])) if d.get("exam_no") else None,
        absent=Region(face=d["absent"]["face"], rect=_rect(d["absent"]["rect"])) if d.get("absent") else None,
        barcodes=[BarcodeSpec(key=b["key"], face=b["face"], rect=_rect(b["rect"])) for b in d.get("barcodes", [])],
        fields=[FieldSpec(key=f["key"], label=f["label"], face=f["face"], rect=_rect(f["rect"])) for f in d.get("fields", [])],
        objective=[ObjectiveBlock(name=o["name"], qtype=o["qtype"], start=int(o["start"]), grid=_grid(o["grid"]))
                   for o in d.get("objective", [])],
        subjective=[SubjectiveSpec(no=s["no"], qtype=s["qtype"], content=s["content"],
                                   regions=[Region(face=r["face"], rect=_rect(r["rect"])) for r in s["regions"]])
                    for s in d.get("subjective", [])],
        ignore=[Region(face=r["face"], rect=_rect(r["rect"])) for r in d.get("ignore", [])],
    )


def template_to_dict(t: Template) -> dict:
    return asdict(t)


def load_template(directory: str | Path) -> tuple[Template, dict[str, np.ndarray]]:
    """读取模板及各面空白底图（灰度，dpi = template.dpi）。"""
    directory = Path(directory)
    t = template_from_dict(json.loads((directory / "template.json").read_text(encoding="utf-8")))
    bases = {}
    for name, face in t.faces.items():
        img = cv2.imread(str(directory / face.image), cv2.IMREAD_GRAYSCALE)
        if img is None:
            raise FileNotFoundError(directory / face.image)
        bases[name] = img
    return t, bases


def save_template(t: Template, directory: str | Path) -> None:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "template.json").write_text(
        json.dumps(template_to_dict(t), ensure_ascii=False, indent=1), encoding="utf-8")
```

`src/scan_answer/template/__init__.py`:
```python
from .model import *  # noqa: F401,F403
from .io import load_template, save_template, template_from_dict, template_to_dict  # noqa: F401
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_layout.py tests/test_template_io.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/template tests/test_layout.py tests/test_template_io.py
git commit -m "feat(template): 模板数据模型、填涂格生成与 JSON 读写"
```

---

### Task 3: 全局对齐（定位块 / 外框 / 特征匹配）

**Files:**
- Create: `src/scan_answer/engine/align.py`
- Test: `tests/test_align.py`（直接用 `samples/raw/8k-199.pdf` 渲染的真实样卡，不依赖模板底图）

**Interfaces:**
- Consumes: `AlignSpec`、`Rect`（Task 2）
- Produces:
  - `MM_PER_INCH = 25.4`；`px_per_mm(dpi) -> float`；`scale_matrix(dpi) -> 3x3`
  - `AlignResult(ok: bool, method: str, H: ndarray|None, skew_deg: float, residual_mm: float, reason: str)`，`H` 把图像像素映射到模板 mm
  - `detect_anchors(gray, expected: list[Rect], dpi, tol_mm=3.0) -> list[(x, y)] | None`（全图候选 + 相似变换几何验证，只接受 ±45° 旋转，比例容差 ±15%）
  - `detect_frames(gray, expected: list[Rect], dpi) -> list[ndarray(4,2)] | None`
  - `align_primary(gray, spec: AlignSpec, dpi) -> AlignResult`
  - `align_features(gray, base, base_dpi, dpi, work_dpi=100) -> AlignResult`（ORB + RANSAC 兜底，method=`"features"`）
  - `effective_dpi(H) -> float`；`warp_to_template(gray, H, paper, dpi) -> ndarray`；`crop_region(gray, H, rect, dpi) -> ndarray`

- [ ] **Step 1: 写失败测试**

`tests/test_align.py`:
```python
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest

from scan_answer.engine.align import align_features, align_primary, effective_dpi
from scan_answer.template.model import AlignSpec

PDF = Path(__file__).resolve().parents[1] / "samples" / "raw" / "8k-199.pdf"
FRONT = AlignSpec(method="anchors", anchors=[(17.45, 11.4, 4.7, 3.5), (372.6, 11.4, 4.7, 3.5),
                                             (372.6, 255.65, 4.7, 3.5), (17.45, 255.65, 4.7, 3.5)])
BACK = AlignSpec(method="frames", frames=[(19.5, 25.7, 166.0, 225.9), (199.0, 25.7, 166.4, 225.9)])


@pytest.fixture(scope="module")
def raw():
    doc = pymupdf.open(PDF)
    pages = []
    for page in doc:
        pix = page.get_pixmap(dpi=300, colorspace=pymupdf.csGRAY)
        pages.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
    return pages


def _rotate(img, deg, shift_px=(0, 0), pad=200):
    """旋转 + 平移，四周加 pad 像素白边（模拟扫描仪背板），返回新图和 3x3 变换矩阵。"""
    h, w = img.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), deg, 1.0)
    M[:, 2] += (shift_px[0] + pad, shift_px[1] + pad)
    return cv2.warpAffine(img, M, (w + 2 * pad, h + 2 * pad), borderValue=250), np.vstack([M, [0, 0, 1]])


def test_raw_front_anchors_and_back_frames(raw):
    f = align_primary(raw[0], FRONT, 300)
    assert f.ok and f.method == "anchors" and abs(f.skew_deg - (-0.36)) < 0.2
    b = align_primary(raw[1], BACK, 300)
    assert b.ok and b.method == "frames" and abs(b.skew_deg - 1.32) < 0.2 and b.residual_mm < 0.5
    assert abs(effective_dpi(f.H) - 300) < 3


@pytest.mark.parametrize("deg,shift", [(2.5, (60, -40)), (-3.0, (-80, 50))])
def test_consistent_after_rotation(raw, deg, shift):
    base = align_primary(raw[0], FRONT, 300)
    img, M = _rotate(raw[0], deg, shift)
    res = align_primary(img, FRONT, 300)
    assert res.ok
    # 同一个原图点：经旋转后再用新 H 映射，应与原 H 映射到同一 mm 位置
    pts = np.array([[[1000.0, 800.0]], [[3500.0, 2600.0]], [[2300.0, 1500.0]]])
    a = cv2.perspectiveTransform(pts, base.H)
    b = cv2.perspectiveTransform(cv2.perspectiveTransform(pts, M), res.H)
    assert np.abs(a - b).max() < 0.5


def test_features_fallback_when_anchor_covered(raw):
    img, _ = _rotate(raw[0], 1.5, (30, 20))
    img[-450:, -650:] = 250
    assert not align_primary(img, FRONT, 300).ok
    res = align_features(img, raw[0], 300, 300)
    assert res.ok and res.method == "features"


def test_blank_has_no_frames():
    blank = np.full((3189, 4606), 250, np.uint8)
    assert not align_primary(blank, BACK, 300).ok
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_align.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.engine.align'`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/align.py`:
```python
"""全局对齐：把扫描图映射到模板坐标（mm）。

定位方式：定位块（anchors）/ 外框（frames）；都失败时用特征匹配（features）兜底。
所有函数返回的单应矩阵 H 都把“图像像素坐标”映射到“模板 mm 坐标”。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..template.model import AlignSpec, Rect

MM_PER_INCH = 25.4


def px_per_mm(dpi: float) -> float:
    return dpi / MM_PER_INCH


def scale_matrix(dpi: float) -> np.ndarray:
    s = px_per_mm(dpi)
    return np.array([[s, 0, 0], [0, s, 0], [0, 0, 1]], dtype=np.float64)


@dataclass
class AlignResult:
    ok: bool
    method: str
    H: np.ndarray | None = None  # 图像像素 -> 模板 mm
    skew_deg: float = 0.0
    residual_mm: float = 0.0
    reason: str = ""


def _center(r: Rect) -> tuple[float, float]:
    return (r[0] + r[2] / 2, r[1] + r[3] / 2)


def _order_quad(pts: np.ndarray) -> np.ndarray:
    """按 左上、右上、右下、左下 排序。"""
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def _anchor_candidates(gray: np.ndarray, size_mm: tuple[float, float], dpi: float) -> np.ndarray:
    """全图找与定位块尺寸相近的实心深色块，返回中心坐标数组 (N, 2)（像素）。"""
    s = px_per_mm(dpi)
    rw, rh = size_mm[0] * s, size_mm[1] * s
    n, _, st, cen = cv2.connectedComponentsWithStats((gray < 140).astype(np.uint8))
    keep = []
    for k in range(1, n):
        x, y, w, h, a = st[k]
        # 允许旋转后外接框变大：宽高在 0.6~1.6 倍之间，实心度 > 0.5
        if 0.6 * min(rw, rh) < min(w, h) < 1.6 * max(rw, rh) and 0.6 * max(rw, rh) < max(w, h) < 1.8 * max(rw, rh) \
                and a > 0.5 * w * h:
            keep.append(cen[k])
    return np.array(keep, dtype=np.float64).reshape(-1, 2)


def detect_anchors(gray: np.ndarray, expected: list[Rect], dpi: float, tol_mm: float = 3.0) -> list[tuple[float, float]] | None:
    """找出与模板定位块几何关系一致的一组黑块，按 expected 的顺序返回中心（像素）；找不到返回 None。

    做法：任取两个候选块，假设它们是第 0、1 个定位块，求相似变换，预测其余定位块的位置，
    在 tol_mm 内都能找到候选块才算成立；取总误差最小的假设。与纸张在图中的位置、旋转角度无关。
    """
    s = px_per_mm(dpi)
    cand = _anchor_candidates(gray, (expected[0][2], expected[0][3]), dpi)
    if len(cand) < len(expected):
        return None
    exp = np.array([_center(r) for r in expected], dtype=np.float64) * s
    e0, e1 = exp[0], exp[1]
    ve = complex(*(e1 - e0))
    best, best_err = None, None
    for i in range(len(cand)):
        for j in range(len(cand)):
            if i == j:
                continue
            vc = complex(*(cand[j] - cand[i]))
            z = vc / ve  # 旋转 + 缩放
            # 容许 DPI 估计误差；只接受 ±45° 内的旋转——定位块近似中心对称，180° 由调用方旋转图像后再试
            if not 0.85 < abs(z) < 1.15 or abs(np.degrees(np.angle(z))) > 45:
                continue
            pred = [cand[i] + np.array([(z * complex(*(p - e0))).real, (z * complex(*(p - e0))).imag]) for p in exp]
            picked, err = [], 0.0
            for p in pred:
                d = np.linalg.norm(cand - p, axis=1)
                k = int(d.argmin())
                if d[k] > tol_mm * s:
                    break
                picked.append((float(cand[k][0]), float(cand[k][1])))
                err += float(d[k])
            else:
                if best_err is None or err < best_err:
                    best, best_err = picked, err
    return best


def detect_frames(gray: np.ndarray, expected: list[Rect], dpi: float) -> list[np.ndarray] | None:
    """找与期望外框面积、位置相近的四边形，返回每个外框的 4 个角点（像素，已排序）。"""
    s = px_per_mm(dpi)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    bw = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 51, 15)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    contours, _ = cv2.findContours(bw, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    min_area = min(r[2] * r[3] for r in expected) * s * s * 0.6
    quads = []
    for c in contours:
        area = cv2.contourArea(c)
        if area < min_area:
            continue
        approx = cv2.approxPolyDP(c, 0.01 * cv2.arcLength(c, True), True)
        if len(approx) == 4:
            quads.append((area, _order_quad(approx.reshape(4, 2).astype(np.float32))))
    result = []
    for r in expected:
        exp_area = r[2] * r[3] * s * s
        ec = np.array(_center(r)) * s
        best, best_d = None, None
        for area, q in quads:
            if not (0.7 * exp_area < area < 1.3 * exp_area):
                continue
            d = float(np.linalg.norm(q.mean(0) - ec))
            if d > 60 * s:
                continue
            # 同一框有内外两条轮廓：距离相近时取面积更大的（外侧）
            if best_d is None or d < best_d - 2 * s or (abs(d - best_d) <= 2 * s and area > best[0]):
                best, best_d = (area, q), d
        if best is None:
            return None
        result.append(best[1])
    return result


def _rect_corners(r: Rect) -> np.ndarray:
    x, y, w, h = r
    return np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], np.float32)


def _finish(method: str, src_px: np.ndarray, dst_mm: np.ndarray) -> AlignResult:
    H, _ = cv2.findHomography(src_px.astype(np.float32), dst_mm.astype(np.float32), 0)
    if H is None:
        return AlignResult(False, method, reason="单应矩阵求解失败")
    proj = cv2.perspectiveTransform(src_px.reshape(-1, 1, 2).astype(np.float64), H).reshape(-1, 2)
    residual = float(np.linalg.norm(proj - dst_mm, axis=1).max())
    (x0, y0), (x1, y1) = src_px[0], src_px[1]
    skew = float(np.degrees(np.arctan2(y1 - y0, x1 - x0)))
    return AlignResult(True, method, H=H, skew_deg=skew, residual_mm=residual)


def align_primary(gray: np.ndarray, spec: AlignSpec, dpi: float) -> AlignResult:
    if spec.method == "anchors":
        centers = detect_anchors(gray, spec.anchors, dpi)
        if centers is None:
            return AlignResult(False, "anchors", reason="定位块缺失")
        return _finish("anchors", np.array(centers), np.array([_center(r) for r in spec.anchors]))
    if spec.method == "frames":
        quads = detect_frames(gray, spec.frames, dpi)
        if quads is None:
            return AlignResult(False, "frames", reason="定位外框缺失")
        return _finish("frames", np.vstack(quads), np.vstack([_rect_corners(r) for r in spec.frames]))
    raise ValueError(spec.method)


def align_features(gray: np.ndarray, base: np.ndarray, base_dpi: float, dpi: float, work_dpi: float = 100) -> AlignResult:
    """兜底：ORB 特征点 + RANSAC，与空白底图匹配。"""
    a = cv2.resize(gray, None, fx=work_dpi / dpi, fy=work_dpi / dpi, interpolation=cv2.INTER_AREA)
    b = cv2.resize(base, None, fx=work_dpi / base_dpi, fy=work_dpi / base_dpi, interpolation=cv2.INTER_AREA)
    orb = cv2.ORB_create(4000)
    ka, da = orb.detectAndCompute(a, None)
    kb, db = orb.detectAndCompute(b, None)
    if da is None or db is None:
        return AlignResult(False, "features", reason="特征点不足")
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(da, db)
    if len(matches) < 40:
        return AlignResult(False, "features", reason="特征匹配不足")
    pa = np.float32([ka[m.queryIdx].pt for m in matches])
    pb = np.float32([kb[m.trainIdx].pt for m in matches])
    Hw, mask = cv2.findHomography(pa, pb, cv2.RANSAC, 3.0)
    if Hw is None or int(mask.sum()) < 40:
        return AlignResult(False, "features", reason="特征匹配内点不足")
    # a 像素 = 原图像素 * work/dpi；b 像素 -> mm = /px_per_mm(work)
    down = np.diag([work_dpi / dpi, work_dpi / dpi, 1.0])
    to_mm = np.diag([1 / px_per_mm(work_dpi), 1 / px_per_mm(work_dpi), 1.0])
    H = to_mm @ Hw @ down
    skew = float(np.degrees(np.arctan2(Hw[1, 0], Hw[0, 0])))
    inl = mask.ravel().astype(bool)
    proj = cv2.perspectiveTransform(pa[inl].reshape(-1, 1, 2), Hw).reshape(-1, 2)
    residual = float(np.median(np.linalg.norm(proj - pb[inl], axis=1))) / px_per_mm(work_dpi)
    return AlignResult(True, "features", H=H, skew_deg=-skew, residual_mm=residual)


def effective_dpi(H: np.ndarray) -> float:
    """由 H（像素 -> mm）反推图像实际分辨率。"""
    return MM_PER_INCH / float(np.sqrt(abs(np.linalg.det(H[:2, :2]))))


def warp_to_template(gray: np.ndarray, H: np.ndarray, paper: tuple[float, float], dpi: float) -> np.ndarray:
    """把原图按 H 重采样成模板坐标系下、指定 dpi 的整页图。"""
    s = px_per_mm(dpi)
    size = (int(round(paper[0] * s)), int(round(paper[1] * s)))
    return cv2.warpPerspective(gray, scale_matrix(dpi) @ H, size, flags=cv2.INTER_LINEAR, borderValue=255)


def crop_region(gray: np.ndarray, H: np.ndarray, rect: Rect, dpi: float) -> np.ndarray:
    """从原图上直接裁切模板 rect（mm）区域，输出分辨率为 dpi。"""
    x, y, w, h = rect
    T = np.array([[1, 0, -x], [0, 1, -y], [0, 0, 1]], dtype=np.float64)
    s = px_per_mm(dpi)
    size = (max(1, int(round(w * s))), max(1, int(round(h * s))))
    return cv2.warpPerspective(gray, scale_matrix(dpi) @ T @ H, size, flags=cv2.INTER_LINEAR, borderValue=255)
```

实现要点（读代码前先理解）：
- 定位块近似中心对称，几何验证无法区分 0° 与 180°，所以 `detect_anchors` 只接受 ±45° 的假设，180° 由调用方（Task 8）把图像旋转后再试，并用与底图的相关系数判断哪个对。
- 同一个外框会检测出内外两条轮廓，`detect_frames` 在距离相近时取面积较大的那条。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_align.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/align.py tests/test_align.py
git commit -m "feat(engine): 定位块/外框/特征匹配三种全局对齐"
```

---

### Task 4: 生成 199 卡模板（底图 + template.json）

**Files:**
- Create: `tools/build_template_199.py`、`tools/overlay_template.py`
- Create（工具生成后提交）: `templates/guanzong199/template.json`、`templates/guanzong199/front.png`、`templates/guanzong199/back.png`
- Test: `tests/test_template_199.py`

**Interfaces:**
- Consumes: `align_primary`、`warp_to_template`（Task 3）；模板模型与 `grid_cells`、`save_template`（Task 2）
- Produces: `templates/guanzong199`（后续所有任务的测试都依赖它）。版面坐标来自对真实空白卡的测量：
  - 纸张 390×270 mm；正面四角定位块中心 (19.8, 13.15)、(374.95, 13.15)、(374.95, 257.4)、(19.8, 257.4)，尺寸 4.7×3.5
  - 背面无定位块，用两个作文外框 (19.5, 25.7, 166.0, 225.9)、(199.0, 25.7, 166.4, 225.9)
  - 考号 15 位：首格中心 (115.55, 51.2)，位距 5.10，行距 3.62，格 3.6×2.2
  - 选择题 4 列：A 中心 x = 36.7 / 76.45 / 115.85 / 154.95，首题 y = 155.9，题距 3.625，每 5 题多空 2.125，选项距 6.22，格 3.8×2.4
  - 缺考 (51.0, 130.9, 4.6, 2.5)；条码搜索区 (225, 8, 62, 26)、(298, 8, 48, 26)
  - 写作 56 = 正面 (215.5, 43.5, 151, 200) + 背面 (26.5, 33.5, 152.5, 80.5)；作文 57 = 背面 (26.5, 131.5, 152.5, 113.5) + (206, 33, 153, 212)

- [ ] **Step 1: 写失败测试**

`tests/test_template_199.py`:
```python
def test_199_template_content(tpl_bases):
    t, bases = tpl_bases
    assert t.name == "管综199" and t.paper == (390.0, 270.0) and t.dpi == 300
    assert t.faces["front"].align.method == "anchors" and len(t.faces["front"].align.anchors) == 4
    assert t.faces["back"].align.method == "frames" and len(t.faces["back"].align.frames) == 2
    assert len(t.exam_no.grid.cells) == 15 and len(t.exam_no.grid.cells[0]) == 10
    assert [b.name for b in t.objective] == ["1-15", "16-30", "31-45", "46-55"]
    assert sum(len(b.grid.cells) for b in t.objective) == 55
    assert [s.no for s in t.subjective] == ["56", "57"]
    assert {r.face for r in t.subjective[0].regions} == {"front", "back"}
    assert bases["front"].shape == (3189, 4606) and bases["back"].shape == (3189, 4606)
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_template_199.py -q`
Expected: FAIL（`FileNotFoundError: .../templates/guanzong199/template.json`）

- [ ] **Step 3: 写工具并生成模板**

`tools/build_template_199.py`:
```python
"""从 samples/raw/8k-199.pdf 生成 templates/guanzong199（空白底图 + template.json）。

坐标来自对真实空白卡的测量（300 DPI，正面按四角定位块纠偏，背面按两个作文外框纠偏）。
用法: python tools/build_template_199.py samples/raw/8k-199.pdf templates/guanzong199
"""
import sys
from pathlib import Path

import cv2
import numpy as np
import pymupdf

from scan_answer.engine.align import align_primary, warp_to_template
from scan_answer.template import (AlignSpec, BarcodeSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock, OmrGrid,
                                  Region, SubjectiveSpec, Template, save_template)
from scan_answer.template.layout import grid_cells

DPI = 300
PAPER = (390.0, 270.0)


def anchor(cx, cy, w=4.7, h=3.5):
    return (round(cx - w / 2, 2), round(cy - h / 2, 2), w, h)


def build() -> Template:
    front_align = AlignSpec(method="anchors", anchors=[
        anchor(19.8, 13.15), anchor(374.95, 13.15), anchor(374.95, 257.4), anchor(19.8, 257.4)])
    back_align = AlignSpec(method="frames", frames=[(19.5, 25.7, 166.0, 225.9), (199.0, 25.7, 166.4, 225.9)])

    # 考生编号：15 位，每位 0-9 纵向排列；第 1 位“0”格中心 (115.55, 51.2)，位距 5.10，行距 3.62
    exam_cells = grid_cells((115.55, 51.2), n_groups=15, n_options=10, option_pitch=3.62, group_pitch=5.10,
                            cell_size=(3.6, 2.2), options_horizontal=False)

    # 选择题：4 列，A 选项中心 x 依次为 36.7 / 76.45 / 115.85 / 154.95；第 1 题 y=155.9；
    # 题距 3.625，每 5 题额外空 (20.25 - 5*3.625) = 2.125；选项间距 6.22
    blocks = []
    for c, ax in enumerate([36.7, 76.45, 115.85, 154.95]):
        start = 1 + 15 * c
        count = 15 if c < 3 else 10
        cells = grid_cells((ax, 155.9), n_groups=count, n_options=5, option_pitch=6.22, group_pitch=3.625,
                           cell_size=(3.8, 2.4), options_horizontal=True, block_every=5, block_gap=2.125)
        blocks.append(ObjectiveBlock(name=f"{start}-{start + count - 1}", qtype="single", start=start,
                                     grid=OmrGrid(face="front", labels=list("ABCDE"), cells=cells)))

    return Template(
        schema_version="1.0", name="管综199", version=1, paper=PAPER, dpi=DPI,
        faces={"front": Face(image="front.png", align=front_align), "back": Face(image="back.png", align=back_align)},
        exam_no=ExamNoSpec(grid=OmrGrid(face="front", labels=[str(d) for d in range(10)], cells=exam_cells)),
        absent=Region(face="front", rect=(51.0, 130.9, 4.6, 2.5)),
        barcodes=[BarcodeSpec(key="student_barcode", face="front", rect=(225.0, 8.0, 62.0, 26.0)),
                  BarcodeSpec(key="paper_barcode", face="front", rect=(298.0, 8.0, 48.0, 26.0))],
        fields=[FieldSpec(key="unit", label="报考单位", face="front", rect=(23.8, 44.1, 87.2, 16.0)),
                FieldSpec(key="name", label="考生姓名", face="front", rect=(23.8, 70.3, 87.3, 16.0)),
                FieldSpec(key="name_back", label="考生姓名（背面）", face="back", rect=(148.0, 13.0, 38.0, 12.0))],
        objective=blocks,
        subjective=[
            SubjectiveSpec(no="56", qtype="essay", content="zh", regions=[
                Region(face="front", rect=(215.5, 43.5, 151.0, 200.0)),
                Region(face="back", rect=(26.5, 33.5, 152.5, 80.5))]),
            SubjectiveSpec(no="57", qtype="essay", content="zh", regions=[
                Region(face="back", rect=(26.5, 131.5, 152.5, 113.5)),
                Region(face="back", rect=(206.0, 33.0, 153.0, 212.0))]),
        ],
        ignore=[Region(face="front", rect=(23.5, 223.8, 166.6, 22.4))],
    )


def render(pdf: Path) -> list[np.ndarray]:
    doc = pymupdf.open(pdf)
    out = []
    for page in doc:
        pix = page.get_pixmap(dpi=DPI, colorspace=pymupdf.csGRAY)
        out.append(np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.width).copy())
    return out


def main(pdf: str, out: str) -> None:
    t = build()
    front_raw, back_raw = render(Path(pdf))
    out_dir = Path(out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, raw in (("front", front_raw), ("back", back_raw)):
        res = align_primary(raw, t.faces[name].align, DPI)
        if not res.ok:
            raise SystemExit(f"{name} 对齐失败: {res.reason}")
        print(f"{name}: {res.method} 倾斜 {res.skew_deg:+.2f}° 残差 {res.residual_mm:.2f} mm")
        cv2.imwrite(str(out_dir / t.faces[name].image), warp_to_template(raw, res.H, t.paper, DPI))
    save_template(t, out_dir)
    print("模板已写入", out_dir)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

`tools/overlay_template.py`:
```python
"""把模板区域画到空白底图上，用于人工核对坐标。
用法: python tools/overlay_template.py templates/guanzong199 out_dir
"""
import sys
from pathlib import Path

import cv2

from scan_answer.template import load_template

COLORS = {"anchor": (0, 0, 255), "exam": (200, 80, 0), "obj": (0, 160, 0), "absent": (0, 0, 255),
          "barcode": (160, 0, 160), "field": (90, 90, 90), "sub": (0, 120, 255)}


def main(tpl_dir: str, out: str) -> None:
    t, bases = load_template(tpl_dir)
    s = t.dpi / 25.4
    canv = {k: cv2.cvtColor(v, cv2.COLOR_GRAY2BGR) for k, v in bases.items()}

    def box(face, r, kind, th=2):
        x, y, w, h = r
        cv2.rectangle(canv[face], (int(x * s), int(y * s)), (int((x + w) * s), int((y + h) * s)), COLORS[kind], th)

    for name, f in t.faces.items():
        for r in f.align.anchors + f.align.frames:
            box(name, r, "anchor", 4)
    if t.exam_no:
        for row in t.exam_no.grid.cells:
            for c in row:
                box(t.exam_no.grid.face, c, "exam")
    if t.absent:
        box(t.absent.face, t.absent.rect, "absent", 3)
    for b in t.barcodes:
        box(b.face, b.rect, "barcode", 4)
    for fd in t.fields:
        box(fd.face, fd.rect, "field", 4)
    for blk in t.objective:
        for row in blk.grid.cells:
            for c in row:
                box(blk.grid.face, c, "obj")
    for sq in t.subjective:
        for r in sq.regions:
            box(r.face, r.rect, "sub", 5)
    Path(out).mkdir(parents=True, exist_ok=True)
    for name, img in canv.items():
        cv2.imwrite(str(Path(out) / f"overlay_{name}.png"), img)
    print("已输出", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
```

生成：
```bash
.venv/bin/python tools/build_template_199.py samples/raw/8k-199.pdf templates/guanzong199
```
Expected 输出：
```
front: anchors 倾斜 -0.36° 残差 0.00 mm
back: frames 倾斜 +1.32° 残差 0.28 mm
模板已写入 templates/guanzong199
```

- [ ] **Step 4: 人工核对叠图**

```bash
.venv/bin/python tools/overlay_template.py templates/guanzong199 /tmp/overlay
```
打开 `/tmp/overlay/overlay_front.png` 和 `overlay_back.png`，逐项确认：每个绿色框（选择题）、蓝色框（考号）都套在印刷的 `[A]` / `[0]` 上；红框套住缺考框和四角定位块；背面红框与两个作文外框重合；橙色框覆盖作文格。若有整体偏移，修改 `build_template_199.py` 中对应参数后重新生成。

- [ ] **Step 5: 运行测试，确认通过**

Run: `.venv/bin/python -m pytest tests/test_template_199.py -q`
Expected: `1 passed`

- [ ] **Step 6: Commit**

```bash
git add tools/build_template_199.py tools/overlay_template.py templates/guanzong199 tests/test_template_199.py
git commit -m "feat(template): 199 管综卡模板（真实空白卡底图 + 实测坐标）"
```

---

### Task 5: OMR（涂黑度、局部微调、判定、批次阈值）

**Files:**
- Create: `src/scan_answer/engine/omr.py`
- Test: `tests/test_omr.py`

**Interfaces:**
- Consumes: `px_per_mm`（Task 3）
- Produces:
  - `Thresholds(mark=0.45, uncertain=0.2025)`（frozen dataclass）；常量 `DEFAULT_MARK=0.45`、`UNCERTAIN_RATIO=0.45`、`SECOND_RATIO=0.35`
  - `cell_fill(work, base, rect, dpi, shift=(0,0)) -> float`：格子内缩 15% 后 `clip((底图-扫描图)/200, 0, 1)` 的均值
  - `grid_bbox(cells) -> Rect`；`local_shift(work, base, bbox, dpi, max_shift_mm=1.5) -> (dx, dy)`；`grid_fills(work, base, cells, dpi) -> ndarray(groups, options)`
  - `decide(fills, labels, qtype, th) -> (answer, status)`；`fit_thresholds(all_fills) -> Thresholds`
- 判定规则（单选/判断）：无选项 ≥ mark 时，最高者 ≥ uncertain 判 `uncertain`，否则 `blank`；多于一个 ≥ mark 判 `multiple`；恰好一个时，第二名 ≥ max(uncertain, 0.35×第一名) 判 `uncertain`，否则 `ok`。多选：所有 ≥ mark 的选项为答案，存在 [uncertain, mark) 区间的选项则判 `uncertain`。

- [ ] **Step 1: 写失败测试**

`tests/test_omr.py`:
```python
import numpy as np

from scan_answer.engine.omr import Thresholds, cell_fill, decide, fit_thresholds

TH = Thresholds(mark=0.45, uncertain=0.2)
L = list("ABCDE")


def f(*v):
    return np.array(v, dtype=np.float32)


def test_decide_single():
    assert decide(f(0.02, 0.9, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "ok")
    assert decide(f(0.02, 0.01, 0.03, 0.01, 0.0), L, "single", TH) == ("", "blank")
    assert decide(f(0.8, 0.02, 0.85, 0.01, 0.0), L, "single", TH) == ("AC", "multiple")
    assert decide(f(0.02, 0.3, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "uncertain")  # 浅涂
    assert decide(f(0.12, 0.85, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "ok")  # 擦除残留
    assert decide(f(0.35, 0.85, 0.03, 0.01, 0.0), L, "single", TH) == ("B", "uncertain")  # 另一个也不浅


def test_decide_multiple_choice():
    assert decide(f(0.8, 0.02, 0.85, 0.01, 0.0), L, "multiple", TH) == ("AC", "ok")
    assert decide(f(0.8, 0.3, 0.85, 0.01, 0.0), L, "multiple", TH) == ("AC", "uncertain")


def test_fit_thresholds_bimodal_and_default():
    rng = np.random.default_rng(0)
    v = np.concatenate([rng.uniform(0, 0.08, 1000), rng.uniform(0.75, 0.95, 200)])
    th = fit_thresholds(v)
    assert 0.3 <= th.mark <= 0.6 and abs(th.uncertain - th.mark * 0.45) < 1e-6
    assert fit_thresholds(np.zeros(10)) == Thresholds()


def test_cell_fill_subtracts_base():
    base = np.full((100, 100), 250, np.uint8)
    base[40:60, 40:60] = 200  # 印刷的浅色字
    work = base.copy()
    assert cell_fill(work, base, (0, 0, 25.4, 25.4), dpi=100) == 0.0  # 与底图相同 -> 0
    work[:, :] = 50
    assert cell_fill(work, base, (0, 0, 25.4, 25.4), dpi=100) > 0.9
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_omr.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.engine.omr'`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/omr.py`:
```python
"""OMR：涂黑度计算、局部微调、判定、批次阈值。

涂黑度 fill：格子内（四周内缩 15%）每个像素 clip((底图 - 扫描图) / 200, 0, 1) 的均值。
扣除了空白底图上的印刷内容（[A]、底纹），2B 铅笔满涂约 0.8~1.0，未涂约 0。
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..template.model import Rect
from .align import px_per_mm

DEFAULT_MARK = 0.45  # 已涂阈值
UNCERTAIN_RATIO = 0.45  # 存疑阈值 = 已涂阈值 * 该比例
SECOND_RATIO = 0.35  # 单选：第二名 >= 第一名 * 该比例 时判为存疑


@dataclass(frozen=True)
class Thresholds:
    mark: float = DEFAULT_MARK
    uncertain: float = DEFAULT_MARK * UNCERTAIN_RATIO


def _px(rect: Rect, dpi: float, inset: float = 0.0) -> tuple[int, int, int, int]:
    s = px_per_mm(dpi)
    x, y, w, h = rect
    dx, dy = w * inset, h * inset
    return (int(round((x + dx) * s)), int(round((y + dy) * s)),
            int(round((x + w - dx) * s)), int(round((y + h - dy) * s)))


def cell_fill(work: np.ndarray, base: np.ndarray, rect: Rect, dpi: float, shift: tuple[int, int] = (0, 0)) -> float:
    x0, y0, x1, y1 = _px(rect, dpi, inset=0.15)
    dx, dy = shift
    a = work[y0 + dy:y1 + dy, x0 + dx:x1 + dx].astype(np.float32)
    b = base[y0:y1, x0:x1].astype(np.float32)
    if a.size == 0 or a.shape != b.shape:
        return 0.0
    return float(np.clip((b - a) / 200.0, 0, 1).mean())


def grid_bbox(cells: list[list[Rect]]) -> Rect:
    xs = [c[0] for row in cells for c in row] + [c[0] + c[2] for row in cells for c in row]
    ys = [c[1] for row in cells for c in row] + [c[1] + c[3] for row in cells for c in row]
    return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def local_shift(work: np.ndarray, base: np.ndarray, bbox: Rect, dpi: float, max_shift_mm: float = 1.5) -> tuple[int, int]:
    """在 ±max_shift_mm 内，把扫描图的 bbox 区域与空白底图对应区域做匹配，返回像素偏移 (dx, dy)。"""
    x0, y0, x1, y1 = _px(bbox, dpi)
    m = int(round(max_shift_mm * px_per_mm(dpi)))
    tpl = base[y0:y1, x0:x1]
    sy0, sx0 = max(0, y0 - m), max(0, x0 - m)
    search = work[sy0:y1 + m, sx0:x1 + m]
    if tpl.size == 0 or search.shape[0] < tpl.shape[0] or search.shape[1] < tpl.shape[1]:
        return (0, 0)
    res = cv2.matchTemplate(cv2.GaussianBlur(search, (3, 3), 0), cv2.GaussianBlur(tpl, (3, 3), 0), cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < 0.3:
        return (0, 0)
    return (loc[0] + sx0 - x0, loc[1] + sy0 - y0)


def grid_fills(work: np.ndarray, base: np.ndarray, cells: list[list[Rect]], dpi: float) -> np.ndarray:
    shift = local_shift(work, base, grid_bbox(cells), dpi)
    return np.array([[cell_fill(work, base, c, dpi, shift) for c in row] for row in cells], dtype=np.float32)


def decide(fills: np.ndarray, labels: list[str], qtype: str, th: Thresholds) -> tuple[str, str]:
    """返回 (answer, status)。answer 为选中标签按顺序拼接；status: ok/blank/multiple/uncertain。"""
    order = np.argsort(-fills)
    top = float(fills[order[0]])
    second = float(fills[order[1]]) if len(fills) > 1 else 0.0
    marked = [i for i in range(len(fills)) if fills[i] >= th.mark]
    ambiguous = [i for i in range(len(fills)) if th.uncertain <= fills[i] < th.mark]
    if not marked:
        if top >= th.uncertain:
            return labels[int(order[0])], "uncertain"
        return "", "blank"
    answer = "".join(labels[i] for i in sorted(marked))
    if qtype in ("single", "judge"):
        if len(marked) > 1:
            return answer, "multiple"
        # 第二名明显偏弱（如擦除残留）不算存疑；与第一名接近才存疑
        return answer, ("uncertain" if second >= max(th.uncertain, SECOND_RATIO * top) else "ok")
    # 多选
    return answer, ("uncertain" if ambiguous else "ok")


def fit_thresholds(all_fills: np.ndarray) -> Thresholds:
    """批次自适应：对全部涂黑度做 Otsu 分割，已涂阈值限制在 [0.30, 0.60]。"""
    v = np.asarray(all_fills, dtype=np.float32).ravel()
    if v.size < 50 or (v >= 0.5).sum() < 5:
        return Thresholds()
    hist, edges = np.histogram(v, bins=100, range=(0, 1))
    hist = hist.astype(np.float64)
    centers = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    m0 = np.cumsum(hist * centers)
    mu0 = np.divide(m0, w0, out=np.zeros_like(m0), where=w0 > 0)
    mu1 = np.divide(m0[-1] - m0, w1, out=np.zeros_like(m0), where=w1 > 0)
    between = w0 * w1 * (mu0 - mu1) ** 2
    t = float(np.clip(centers[int(np.argmax(between))], 0.30, 0.60))
    return Thresholds(mark=t, uncertain=t * UNCERTAIN_RATIO)
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_omr.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/omr.py tests/test_omr.py
git commit -m "feat(engine): OMR 涂黑度、局部微调、判定规则与批次自适应阈值"
```

---

### Task 6: 条码识别

**Files:**
- Create: `src/scan_answer/engine/barcode.py`
- Test: `tests/test_barcode.py`

**Interfaces:**
- Produces: `read_barcode(gray) -> tuple[str, str] | None`，返回 `(文本, 码制名)`，如 `("100015000123456", "Code128")`；依次尝试原图、Otsu 二值化、放大 2 倍。

- [ ] **Step 1: 写失败测试**

`tests/test_barcode.py`:
```python
import numpy as np
import zxingcpp

from scan_answer.engine.barcode import read_barcode


def test_read_code128_and_none():
    bc = zxingcpp.create_barcode("100015000123456", zxingcpp.BarcodeFormat.Code128)
    img = np.asarray(zxingcpp.write_barcode_to_image(bc, scale=3, add_quiet_zones=True))
    assert read_barcode(img) == ("100015000123456", "Code128")
    assert read_barcode(np.full((80, 200), 255, np.uint8)) is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_barcode.py -q`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/barcode.py`:
```python
"""条码 / 二维码识别（zxing-cpp）。"""
from __future__ import annotations

import cv2
import numpy as np
import zxingcpp


def _try(img: np.ndarray) -> tuple[str, str] | None:
    for r in zxingcpp.read_barcodes(img):
        if r.valid and r.text:
            return r.text, r.format.name
    return None


def read_barcode(gray: np.ndarray) -> tuple[str, str] | None:
    """依次尝试：原图、Otsu 二值化、放大 2 倍。返回 (文本, 码制) 或 None。"""
    hit = _try(gray)
    if hit:
        return hit
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    hit = _try(bw)
    if hit:
        return hit
    return _try(cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_barcode.py -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/barcode.py tests/test_barcode.py
git commit -m "feat(engine): zxing-cpp 条码识别"
```

---

### Task 7: 模拟测试集生成器

**Files:**
- Create: `src/scan_answer/synth/draw.py`、`src/scan_answer/synth/scan.py`、`src/scan_answer/synth/generate.py`
- Test: `tests/test_synth.py`

**Interfaces:**
- Consumes: 模板（Task 2/4）、`px_per_mm`（Task 3）、`read_barcode`（Task 6，仅测试用）
- Produces:
  - `draw.pencil_mark(img, rect, dpi, kind: "normal"|"light"|"erased", rng)`、`draw.handwrite(img, rect, dpi, text, rng, char_mm=6.0, pitch_mm=None, line_mm=None)`、`draw.paste_barcode(img, rect, dpi, text)`、`draw.find_cjk_font() -> str|None`
  - `scan.ScanParams`、`scan.random_params(rng)`、`scan.scanify(paper, paper_dpi, out_dpi, params, rng) -> ndarray`（四周加 `margin_mm` 背板）、`scan.show_through(back, front, strength)`、`scan.fold_corner(paper, dpi, size_mm=35.0)`
  - `generate.make_sheet(t, bases, rng) -> (dict[face, ndarray], truth: dict)`、`generate.make_scans(t, paper, truth, rng, out_dpi=300, special=None) -> list[ndarray]`（special ∈ `rot180/swap/fold/blank/None`）、`generate.generate_dataset(t, bases, out_dir, count, seed=0, out_dpi=300)`
  - 测试集文件：`{seq:04d}_1.jpg`、`{seq:04d}_2.jpg`（JPEG 内写入 DPI）、`{seq:04d}.truth.json`
  - truth 结构：`{"objective": [{"no","case","answer","status"}], "exam_no": {"value","status","full"}|None, "absent": bool, "barcode": str|None, "physical": str|None, "special": str|None}`

- [ ] **Step 1: 写失败测试**

`tests/test_synth.py`:
```python
import json

import numpy as np
from PIL import Image

from scan_answer.synth.draw import paste_barcode, pencil_mark
from scan_answer.synth.generate import generate_dataset, make_scans, make_sheet
from scan_answer.synth.scan import ScanParams, scanify


def test_pencil_mark_darkens_only_inside_cell():
    img = np.full((300, 300), 250, np.uint8)
    rng = np.random.default_rng(0)
    pencil_mark(img, (5.0, 5.0, 10.0, 6.0), 300, "normal", rng)  # 300dpi: 5mm≈59px
    inside = img[85:130, 75:160]
    assert inside.mean() < 120
    assert img[:40, :].min() == 250 and img[200:, :].min() == 250


def test_paste_barcode_readable():
    from scan_answer.engine.barcode import read_barcode
    img = np.full((400, 900), 250, np.uint8)
    paste_barcode(img, (2.0, 2.0, 70.0, 26.0), 300, "100015000123456")
    assert read_barcode(img) == ("100015000123456", "Code128")


def test_make_sheet_truth(tpl_bases):
    t, bases = tpl_bases
    paper, truth = make_sheet(t, bases, np.random.default_rng(1))
    assert set(paper) == {"front", "back"}
    assert len(truth["objective"]) == 55
    assert len(truth["exam_no"]["value"]) == 15
    for o in truth["objective"]:
        assert o["status"] in {"ok", "blank", "multiple", "uncertain"}
    assert (paper["front"] < bases["front"]).sum() > 1000  # 确实画上了东西


def test_scanify_adds_margin_and_keeps_dpi():
    paper = np.full((1181, 1181), 250, np.uint8)  # 100mm @300dpi
    out = scanify(paper, 300, 200, ScanParams(margin_mm=10), np.random.default_rng(0))
    assert out.shape == (944, 944)  # (100+20)mm @200dpi


def test_special_cases(tpl_bases):
    t, bases = tpl_bases
    rng = np.random.default_rng(2)
    paper, truth = make_sheet(t, bases, rng)
    scans = make_scans(t, paper, truth, rng, 200, "blank")
    assert truth["physical"] == "BLANK_PAGE" and scans[-1].std() < 10


def test_generate_dataset_files(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path, 2, seed=3, out_dpi=200)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["0001.truth.json", "0001_1.jpg", "0001_2.jpg", "0002.truth.json", "0002_1.jpg", "0002_2.jpg"]
    with Image.open(tmp_path / "0001_1.jpg") as im:
        assert round(im.info["dpi"][0]) == 200
    assert "objective" in json.loads((tmp_path / "0001.truth.json").read_text(encoding="utf-8"))
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_synth.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.synth.draw'`）

- [ ] **Step 3: 实现**

`src/scan_answer/synth/draw.py`:
```python
"""在模板坐标系（template.dpi）的底图上绘制：铅笔涂卡、手写文字、条码。"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import zxingcpp
from PIL import Image, ImageDraw, ImageFont

from ..engine.align import px_per_mm
from ..template.model import Rect

# 涂卡种类 -> 铅笔灰度范围（越小越黑）
LEVELS = {"normal": (35, 90), "light": (160, 182), "erased": (215, 232)}

FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "C:/Windows/Fonts/simkai.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def find_cjk_font() -> str | None:
    for p in FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def pencil_mark(img: np.ndarray, rect: Rect, dpi: float, kind: str, rng: np.random.Generator) -> None:
    """在 rect（mm）内画一个铅笔涂块，原地修改 img（灰度，取较暗值合成）。"""
    s = px_per_mm(dpi)
    x, y, w, h = rect
    cov = rng.uniform(0.82, 1.0)
    cx, cy = (x + w / 2) * s, (y + h / 2) * s
    hw, hh = w * cov / 2 * s, h * cov / 2 * s
    cx += rng.uniform(-0.08, 0.08) * w * s
    cy += rng.uniform(-0.08, 0.08) * h * s
    x0, y0 = int(cx - hw), int(cy - hh)
    x1, y1 = int(cx + hw), int(cy + hh)
    lo, hi = LEVELS[kind]
    level = rng.uniform(lo, hi)
    patch = np.full((y1 - y0, x1 - x0), level, np.float32)
    patch += rng.normal(0, 12, patch.shape)  # 石墨颗粒
    for _ in range(int(rng.integers(2, 6))):  # 涂抹笔触
        yy = int(rng.integers(0, max(1, patch.shape[0])))
        patch[yy:yy + 2, :] += rng.uniform(-25, 25)
    mask = np.zeros(patch.shape, np.uint8)
    cv2.rectangle(mask, (0, 0), (patch.shape[1] - 1, patch.shape[0] - 1), 255, -1)
    r = max(1, int(min(patch.shape) * 0.25))  # 圆角
    for (px, py) in ((0, 0), (patch.shape[1] - 1, 0), (0, patch.shape[0] - 1), (patch.shape[1] - 1, patch.shape[0] - 1)):
        cv2.circle(mask, (px, py), r, 0, -1)
    patch = np.clip(patch, 0, 255).astype(np.uint8)
    region = img[y0:y1, x0:x1]
    blended = np.where(mask > 0, np.minimum(region, patch), region)
    img[y0:y1, x0:x1] = blended


def handwrite(img: np.ndarray, rect: Rect, dpi: float, text: str, rng: np.random.Generator,
              char_mm: float = 6.0, pitch_mm: float | None = None, line_mm: float | None = None) -> None:
    """在 rect 内逐字写入文字（带随机抖动），模拟手写；无中文字体时画随机笔画。"""
    s = px_per_mm(dpi)
    x, y, w, h = rect
    pitch = (pitch_mm or char_mm * 1.15) * s
    line = (line_mm or char_mm * 1.4) * s
    font_path = find_cjk_font()
    pil = Image.fromarray(img)
    draw = ImageDraw.Draw(pil)
    font = ImageFont.truetype(font_path, int(char_mm * s)) if font_path else None
    cx, cy = x * s + pitch * 0.15, y * s + line * 0.15
    for ch in text:
        if cx + pitch > (x + w) * s:
            cx, cy = x * s + pitch * 0.15, cy + line
        if cy + line > (y + h) * s:
            break
        jx, jy = rng.uniform(-0.12, 0.12) * pitch, rng.uniform(-0.12, 0.12) * line
        ink = int(rng.uniform(20, 60))
        if font:
            draw.text((cx + jx, cy + jy), ch, font=font, fill=ink)
        else:
            pts = [(cx + jx + rng.uniform(0, pitch * 0.8), cy + jy + rng.uniform(0, line * 0.8)) for _ in range(4)]
            draw.line(pts, fill=ink, width=max(1, int(0.3 * s)))
        cx += pitch
    img[:] = np.asarray(pil)


def paste_barcode(img: np.ndarray, rect: Rect, dpi: float, text: str) -> None:
    """在 rect 中央贴一张白底 Code128 条码标签（约 rect 宽的 80%）。"""
    s = px_per_mm(dpi)
    bc = zxingcpp.create_barcode(text, zxingcpp.BarcodeFormat.Code128)
    code = np.asarray(zxingcpp.write_barcode_to_image(bc, scale=4, add_quiet_zones=True))
    x, y, w, h = rect
    tw, th = int(w * 0.8 * s), int(min(h * 0.6, 14) * s)
    code = cv2.resize(code, (tw, th), interpolation=cv2.INTER_NEAREST)
    ox, oy = int((x + w * 0.1) * s), int((y + (h - min(h * 0.6, 14)) / 2) * s)
    img[oy - 8:oy + th + 8, ox - 8:ox + tw + 8] = 255
    img[oy:oy + th, ox:ox + tw] = code
```

`src/scan_answer/synth/scan.py`:
```python
"""模拟扫描：把模板坐标系下的“纸面图”变成一张扫描图（几何扰动 + 成像噪声）。"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from ..engine.align import px_per_mm


@dataclass
class ScanParams:
    rotate_deg: float = 0.0
    shift_mm: tuple[float, float] = (0.0, 0.0)
    scale: float = 1.0
    corner_jitter_mm: float = 0.0  # 轻微透视
    wave_mm: float = 0.0  # 非刚性起伏幅度
    blur_sigma: float = 0.0
    noise_sigma: float = 0.0
    brightness: float = 0.0
    contrast: float = 1.0
    jpeg_quality: int = 95
    margin_mm: float = 15.0  # 扫描图在纸张四周留白（扫描仪背板）


def random_params(rng: np.random.Generator) -> ScanParams:
    return ScanParams(
        rotate_deg=float(rng.uniform(-3, 3)),
        shift_mm=(float(rng.uniform(-8, 8)), float(rng.uniform(-8, 8))),
        scale=float(rng.uniform(0.98, 1.02)),
        corner_jitter_mm=float(rng.uniform(0, 0.8)),
        wave_mm=float(rng.uniform(0, 0.4)),
        blur_sigma=float(rng.uniform(0, 0.8)),
        noise_sigma=float(rng.uniform(0, 5)),
        brightness=float(rng.uniform(-12, 12)),
        contrast=float(rng.uniform(0.92, 1.08)),
        jpeg_quality=int(rng.integers(80, 96)),
    )


def scanify(paper: np.ndarray, paper_dpi: float, out_dpi: float, p: ScanParams, rng: np.random.Generator) -> np.ndarray:
    s_in, s_out = px_per_mm(paper_dpi), px_per_mm(out_dpi)
    h_mm, w_mm = paper.shape[0] / s_in, paper.shape[1] / s_in
    W, H = int((w_mm + 2 * p.margin_mm) * s_out), int((h_mm + 2 * p.margin_mm) * s_out)
    # 纸面四角（输入像素）-> 扫描图位置（输出像素）
    src = np.float32([[0, 0], [paper.shape[1], 0], [paper.shape[1], paper.shape[0]], [0, paper.shape[0]]])
    corners_mm = np.float32([[0, 0], [w_mm, 0], [w_mm, h_mm], [0, h_mm]])
    c = corners_mm.mean(0)
    th = np.radians(p.rotate_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]], np.float32)
    moved = (corners_mm - c) @ R.T * p.scale + c + np.float32(p.shift_mm) + p.margin_mm
    moved += rng.uniform(-p.corner_jitter_mm, p.corner_jitter_mm, moved.shape).astype(np.float32)
    M = cv2.getPerspectiveTransform(src, moved * s_out)
    out = cv2.warpPerspective(paper, M, (W, H), flags=cv2.INTER_LINEAR, borderValue=245)
    if p.wave_mm > 0:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        amp = p.wave_mm * s_out
        mapx = xx + amp * np.sin(yy / H * np.pi * 2)
        mapy = yy + amp * np.sin(xx / W * np.pi * 1.5)
        out = cv2.remap(out, mapx, mapy, cv2.INTER_LINEAR, borderValue=245)
    if p.blur_sigma > 0.05:
        out = cv2.GaussianBlur(out, (0, 0), p.blur_sigma)
    f = out.astype(np.float32) * p.contrast + p.brightness + (p.contrast - 1) * -128
    if p.noise_sigma > 0:
        f += rng.normal(0, p.noise_sigma, f.shape).astype(np.float32)
    out = np.clip(f, 0, 255).astype(np.uint8)
    ok, enc = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, p.jpeg_quality])
    return cv2.imdecode(enc, cv2.IMREAD_GRAYSCALE)


def show_through(back: np.ndarray, front: np.ndarray, strength: float = 0.10) -> np.ndarray:
    """背面叠加正面的镜像淡影（透印）。"""
    ghost = 255 - (255 - cv2.flip(front, 1).astype(np.float32)) * strength
    return np.minimum(back, ghost.astype(np.uint8))


def fold_corner(paper: np.ndarray, dpi: float, size_mm: float = 35.0) -> np.ndarray:
    """右下角折角：用背板颜色的三角形盖住右下角（会遮住定位块）。"""
    s = px_per_mm(dpi)
    h, w = paper.shape
    k = int(size_mm * s)
    out = paper.copy()
    tri = np.array([[w, h - k], [w, h], [w - k, h]], np.int32)
    cv2.fillPoly(out, [tri], 245)
    return out
```

`src/scan_answer/synth/generate.py`:
```python
"""生成模拟测试集：每张卡两张扫描图 + 一份标准答案 truth.json。

文件命名：{seq:04d}_1.jpg、{seq:04d}_2.jpg（扫描仪输出顺序），{seq:04d}.truth.json
"""
from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from ..template.model import Template
from .draw import handwrite, paste_barcode, pencil_mark
from .scan import fold_corner, random_params, scanify, show_through

ESSAY = ("管理的本质在于激发人的善意与潜能。企业在快速扩张的过程中往往把注意力放在规模与速度上，"
         "却忽略了组织内部的信任与沟通。由此可见，管理者应当把权力下放到最接近问题的地方。")
NAMES = ["张明远", "李思琪", "王浩然", "陈雨桐", "刘子墨", "赵一诺"]
UNITS = ["北京理工大学", "复旦大学", "浙江大学", "南京大学"]

# 客观题情形 -> 概率
CASES = [("normal", 0.84), ("blank", 0.04), ("multiple", 0.03), ("light", 0.04), ("erased", 0.05)]


def _pick(rng, items):
    names, probs = zip(*items)
    return names[int(rng.choice(len(names), p=np.array(probs) / sum(probs)))]


def make_sheet(t: Template, bases: dict[str, np.ndarray], rng: np.random.Generator) -> tuple[dict[str, np.ndarray], dict]:
    """在模板坐标系下生成一张填好的卡（纸面图），返回 ({face: img}, truth)。"""
    dpi = t.dpi
    paper = {k: v.copy() for k, v in bases.items()}
    truth: dict = {"objective": [], "exam_no": None, "absent": False, "barcode": None, "physical": None}

    if t.exam_no:
        digits = [str(int(d)) for d in rng.integers(0, 10, len(t.exam_no.grid.cells))]
        blank_pos = int(rng.integers(0, len(digits))) if rng.random() < 0.10 else -1
        value = ""
        for pos, (d, cells) in enumerate(zip(digits, t.exam_no.grid.cells)):
            if pos == blank_pos:
                value += "?"
                continue
            pencil_mark(paper[t.exam_no.grid.face], cells[int(d)], dpi, "normal", rng)
            value += d
        truth["exam_no"] = {"value": value, "status": "ok" if blank_pos < 0 else "partial", "full": "".join(digits)}
    if t.absent and rng.random() < 0.03:
        pencil_mark(paper[t.absent.face], t.absent.rect, dpi, "normal", rng)
        truth["absent"] = True

    for blk in t.objective:
        labels = blk.grid.labels
        for k, cells in enumerate(blk.grid.cells):
            case = _pick(rng, CASES)
            face = paper[blk.grid.face]
            opts = rng.permutation(len(labels))
            if case == "normal":
                chosen = [int(opts[0])]
                exp = ("".join(labels[i] for i in chosen), "ok")
            elif case == "blank":
                chosen = []
                exp = ("", "blank")
            elif case == "multiple":
                chosen = sorted(int(i) for i in opts[:2])
                exp = ("".join(labels[i] for i in chosen), "multiple")
            elif case == "light":
                chosen = []
                pencil_mark(face, cells[int(opts[0])], dpi, "light", rng)
                exp = (labels[int(opts[0])], "uncertain")
            else:  # erased：一个选项擦掉，另一个正常涂
                pencil_mark(face, cells[int(opts[1])], dpi, "erased", rng)
                chosen = [int(opts[0])]
                exp = (labels[int(opts[0])], "ok")
            for i in chosen:
                pencil_mark(face, cells[i], dpi, "normal", rng)
            truth["objective"].append({"no": str(blk.start + k), "case": case, "answer": exp[0], "status": exp[1]})

    for f in t.fields:
        text = str(rng.choice(NAMES)) if f.key.startswith("name") else str(rng.choice(UNITS))
        handwrite(paper[f.face], f.rect, dpi, text, rng, char_mm=min(8.0, f.rect[3] * 0.55))
    for sq in t.subjective:
        for r in sq.regions:
            handwrite(paper[r.face], r.rect, dpi, ESSAY, rng, char_mm=5.6, pitch_mm=7.6, line_mm=7.95)
    for bc in t.barcodes:
        if bc.key == "student_barcode" and truth["exam_no"] and rng.random() < 0.9:
            paste_barcode(paper[bc.face], bc.rect, dpi, truth["exam_no"]["full"])
            truth["barcode"] = truth["exam_no"]["full"]
    return paper, truth


def make_scans(t: Template, paper: dict[str, np.ndarray], truth: dict, rng: np.random.Generator,
               out_dpi: float = 300, special: str | None = None) -> list[np.ndarray]:
    """纸面图 -> 两张扫描图（按扫描仪输出顺序）。special: rot180/swap/fold/blank 之一或 None。"""
    front, back = paper["front"], paper.get("back")
    truth["special"] = special
    if special == "fold":
        front = fold_corner(front, t.dpi)
    if back is not None:
        back = show_through(back, paper["front"], strength=float(rng.uniform(0.05, 0.12)))
    scans = [scanify(front, t.dpi, out_dpi, random_params(rng), rng)]
    if back is not None:
        scans.append(scanify(back, t.dpi, out_dpi, random_params(rng), rng))
    if special == "rot180":
        scans[0] = cv2.rotate(scans[0], cv2.ROTATE_180)
    if special == "swap" and len(scans) == 2:
        scans.reverse()
    if special == "blank":
        scans[-1] = np.clip(np.full_like(scans[-1], 247) + rng.normal(0, 3, scans[-1].shape), 0, 255).astype(np.uint8)
        truth["physical"] = "BLANK_PAGE"
    return scans


SPECIALS = [(None, 0.72), ("rot180", 0.10), ("swap", 0.10), ("fold", 0.05), ("blank", 0.03)]


def generate_dataset(t: Template, bases: dict[str, np.ndarray], out_dir: str | Path, count: int, seed: int = 0,
                     out_dpi: float = 300) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for seq in range(1, count + 1):
        paper, truth = make_sheet(t, bases, rng)
        scans = make_scans(t, paper, truth, rng, out_dpi, _pick(rng, SPECIALS))
        for i, img in enumerate(scans, start=1):
            Image.fromarray(img).save(out / f"{seq:04d}_{i}.jpg", quality=95, dpi=(out_dpi, out_dpi))
        (out / f"{seq:04d}.truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_synth.py -q`
Expected: `6 passed`

- [ ] **Step 5: 目视检查一张模拟卡**

```bash
.venv/bin/python -c "
from scan_answer.template import load_template
from scan_answer.synth.generate import generate_dataset
t, b = load_template('templates/guanzong199')
generate_dataset(t, b, '/tmp/synth_check', 2, seed=1)
"
```
打开 `/tmp/synth_check/0001_1.jpg`：应看到考号和选择题有铅笔涂块、姓名和作文有文字、整页有轻微倾斜和噪点。

- [ ] **Step 6: Commit**

```bash
git add src/scan_answer/synth tests/test_synth.py
git commit -m "feat(synth): 以真实空白卡为底图的模拟测试集生成器"
```

---

### Task 8: 单张识别（方向与正反面、物理异常、判定）

**Files:**
- Create: `src/scan_answer/engine/recognize.py`
- Test: `tests/test_recognize.py`

**Interfaces:**
- Consumes: Task 3 的对齐函数、Task 5 的 OMR、Task 6 的 `read_barcode`、Task 7 的 `make_sheet/make_scans`（测试用）
- Produces:
  - 常量 `WORK_DPI=200`、`SCORE_DPI=30`、`MIN_SCORE=0.35`、`MAX_SKEW=8.0`、`PHYSICAL_CODES`
  - `PhysicalError(code, msg)`（异常，带 `.code`、`.msg`）
  - `TemplateContext(tpl, bases, work_dpi=200)`：属性 `tpl`、`bases`、`work_dpi`、`work_bases`、`score_bases`
  - `FacePlacement(source: int, rotated: 0|180, align: AlignResult, dpi: float)`
  - `SheetAnalysis(placements: dict[face, FacePlacement], swapped: bool, exam_fills, absent_fill, objective_fills: list[ndarray], barcodes: dict[key, (text, fmt)|None])`
  - `estimate_dpi(gray, paper)`、`is_blank(gray)`、`oriented(gray, rotated)`
  - `place_faces(ctx, images, dpis) -> (placements, swapped)`、`analyze_sheet(ctx, images, dpis=None) -> SheetAnalysis`
  - `SheetDecision(exam_no: dict|None, absent: dict|None, objective: list[dict], barcodes: dict[key, dict], consistent: bool|None, warnings: list[dict])`
  - `decide_sheet(ctx, analysis, th) -> SheetDecision`

- [ ] **Step 1: 写失败测试**

`tests/test_recognize.py`:
```python
import cv2
import numpy as np
import pytest

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import PhysicalError, analyze_sheet, decide_sheet
from scan_answer.synth.generate import make_scans, make_sheet


def _sheet(ctx, seed, special=None):
    t = ctx.tpl
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(t, ctx.bases, rng)
    return make_scans(t, paper, truth, rng, 300, special), truth


@pytest.mark.parametrize("special", [None, "rot180", "swap"])
def test_orientation_and_answers(ctx, special):
    scans, truth = _sheet(ctx, 3, special)
    a = analyze_sheet(ctx, scans, [300, 300])
    assert a.swapped == (special == "swap")
    assert a.placements["front"].rotated == (180 if special == "rot180" else 0)
    d = decide_sheet(ctx, a, Thresholds())
    got = {o["no"]: (o["answer"], o["status"]) for o in d.objective}
    for exp in truth["objective"]:
        if exp["case"] != "light":
            assert got[exp["no"]] == (exp["answer"], exp["status"]), exp
    assert d.exam_no["value"] == truth["exam_no"]["value"]


def test_blank_page_is_physical(ctx):
    scans, _ = _sheet(ctx, 4, "blank")
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, scans, [300, 300])
    assert e.value.code == "BLANK_PAGE"


def test_missing_face_and_garbage(ctx):
    scans, _ = _sheet(ctx, 5)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, scans[:1], [300])
    assert e.value.code == "MISSING_FACE"
    noise = np.random.default_rng(0).integers(0, 255, (3300, 4700)).astype(np.uint8)
    noise = cv2.GaussianBlur(noise, (0, 0), 6)
    with pytest.raises(PhysicalError) as e:
        analyze_sheet(ctx, [noise, noise], [300, 300])
    assert e.value.code in ("ALIGN_FAILED", "FACE_MISMATCH")


def test_blank_base_has_no_marks(ctx):
    """空白底图自身的涂黑度应接近 0（底图减底图）。"""
    from scan_answer.engine.omr import grid_fills
    t = ctx.tpl
    for blk in t.objective:
        f = grid_fills(ctx.work_bases["front"], ctx.work_bases["front"], blk.grid.cells, ctx.work_dpi)
        assert float(f.max()) < 0.01
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_recognize.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.engine.recognize'`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/recognize.py`:
```python
"""单张答题卡识别：方向与正反面判定 → 全局对齐 → OMR / 条码。

分两步，便于批次自适应阈值：
  analyze_sheet()  只计算涂黑度、条码等“原始量”
  decide_sheet()   用（批次）阈值做判定，生成考号、答案、状态和 warnings
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import permutations

import cv2
import numpy as np

from ..template.model import Template
from .align import AlignResult, align_features, align_primary, crop_region, effective_dpi, warp_to_template
from .barcode import read_barcode
from .omr import Thresholds, cell_fill, decide, grid_fills

WORK_DPI = 200
SCORE_DPI = 30
MIN_SCORE = 0.35  # 对齐后与空白底图的相关系数下限
MAX_SKEW = 8.0


class PhysicalError(Exception):
    """物理异常：需要现场重扫或作废。code 见 PHYSICAL_CODES。"""

    def __init__(self, code: str, msg: str):
        super().__init__(f"{code}: {msg}")
        self.code = code
        self.msg = msg


PHYSICAL_CODES = {"ALIGN_FAILED", "FACE_MISMATCH", "BLANK_PAGE", "MISSING_FACE", "SKEW_TOO_LARGE"}


class TemplateContext:
    """模板 + 预先缩放好的底图，供多张卡复用。"""

    def __init__(self, tpl: Template, bases: dict[str, np.ndarray], work_dpi: int = WORK_DPI):
        self.tpl = tpl
        self.bases = bases
        self.work_dpi = work_dpi
        self.work_bases = {k: cv2.resize(v, None, fx=work_dpi / tpl.dpi, fy=work_dpi / tpl.dpi,
                                         interpolation=cv2.INTER_AREA) for k, v in bases.items()}
        self.score_bases = {k: cv2.GaussianBlur(cv2.resize(v, None, fx=SCORE_DPI / tpl.dpi, fy=SCORE_DPI / tpl.dpi,
                                                           interpolation=cv2.INTER_AREA), (3, 3), 0)
                            for k, v in bases.items()}


@dataclass
class FacePlacement:
    source: int  # 来自第几张输入图
    rotated: int  # 0 或 180
    align: AlignResult
    dpi: float


@dataclass
class SheetAnalysis:
    placements: dict[str, FacePlacement]
    swapped: bool
    exam_fills: np.ndarray | None = None
    absent_fill: float | None = None
    objective_fills: list[np.ndarray] = field(default_factory=list)
    barcodes: dict[str, tuple[str, str] | None] = field(default_factory=dict)


def estimate_dpi(gray: np.ndarray, paper: tuple[float, float]) -> float:
    return max(gray.shape) / max(paper) * 25.4


def is_blank(gray: np.ndarray) -> bool:
    return float((gray < 128).mean()) < 0.003


def oriented(gray: np.ndarray, rotated: int) -> np.ndarray:
    return gray if rotated == 0 else cv2.rotate(gray, cv2.ROTATE_180)


def _score(ctx: TemplateContext, face: str, gray: np.ndarray, H: np.ndarray) -> float:
    small = cv2.GaussianBlur(warp_to_template(gray, H, ctx.tpl.paper, SCORE_DPI), (3, 3), 0)
    base = ctx.score_bases[face]
    h, w = min(small.shape[0], base.shape[0]), min(small.shape[1], base.shape[1])
    return float(cv2.matchTemplate(small[:h, :w], base[:h, :w], cv2.TM_CCOEFF_NORMED)[0, 0])


def _normalize(res: AlignResult, rot: int, shape: tuple[int, int]) -> tuple[AlignResult, int]:
    """定位块近似中心对称时，H 本身可能已包含 180° 旋转。

    此时改为“图像先旋转 180° 再对齐”的等价表示：rot += 180，H = H @ R180，
    使 rotated 字段如实反映图像方向，倾斜角归一到 (-90°, 90°]。
    """
    if abs(res.skew_deg) <= 90:
        return res, rot
    h, w = shape
    r180 = np.array([[-1, 0, w - 1], [0, -1, h - 1], [0, 0, 1]], dtype=np.float64)
    res.H = res.H @ r180
    res.skew_deg -= 180 if res.skew_deg > 0 else -180
    return res, (rot + 180) % 360


def place_faces(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float]) -> tuple[dict[str, FacePlacement], bool]:
    """为每个模板面找到对应的输入图和旋转角度。先用主定位方式，全部失败再用特征匹配兜底。"""
    faces = list(ctx.tpl.faces)
    if len(images) != len(faces):
        raise PhysicalError("MISSING_FACE", f"需要 {len(faces)} 面图像，实际 {len(images)} 面")
    best: dict[tuple[int, str], tuple[float, FacePlacement]] = {}
    any_aligned = False
    for use_fallback in (False, True):
        for i, img in enumerate(images):
            for rot in (0, 180):
                g = oriented(img, rot)
                for face in faces:
                    if best.get((i, face), (0.0,))[0] >= 0.6:
                        continue
                    spec = ctx.tpl.faces[face].align
                    res = (align_features(g, ctx.bases[face], ctx.tpl.dpi, dpis[i]) if use_fallback
                           else align_primary(g, spec, dpis[i]))
                    if not res.ok:
                        continue
                    any_aligned = True
                    sc = _score(ctx, face, g, res.H)
                    if sc > best.get((i, face), (-1.0,))[0]:
                        res, eff_rot = _normalize(res, rot, g.shape)
                        best[(i, face)] = (sc, FacePlacement(i, eff_rot, res, effective_dpi(res.H)))
        choice = None
        for perm in permutations(range(len(images)), len(faces)):
            pairs = [best.get((i, f)) for i, f in zip(perm, faces)]
            if any(p is None or p[0] < MIN_SCORE for p in pairs):
                continue
            total = sum(p[0] for p in pairs)
            if choice is None or total > choice[0]:
                choice = (total, perm, pairs)
        if choice:
            _, perm, pairs = choice
            placements = {f: p[1] for f, p in zip(faces, pairs)}
            for f, p in placements.items():
                if abs(p.align.skew_deg) > MAX_SKEW:
                    raise PhysicalError("SKEW_TOO_LARGE", f"{f} 面倾斜 {p.align.skew_deg:.1f}°")
            return placements, list(perm) != sorted(perm)
    if not any_aligned:
        raise PhysicalError("ALIGN_FAILED", "所有面都无法定位")
    raise PhysicalError("FACE_MISMATCH", "图像与模板的正反面无法匹配")


def _expand(rect, mm):
    x, y, w, h = rect
    return (x - mm, y - mm, w + 2 * mm, h + 2 * mm)


def analyze_sheet(ctx: TemplateContext, images: list[np.ndarray], dpis: list[float] | None = None) -> SheetAnalysis:
    t = ctx.tpl
    dpis = dpis or [estimate_dpi(g, t.paper) for g in images]
    for g in images:
        if is_blank(g):
            raise PhysicalError("BLANK_PAGE", "检测到空白页")
    placements, swapped = place_faces(ctx, images, dpis)
    work = {f: warp_to_template(oriented(images[p.source], p.rotated), p.align.H, t.paper, ctx.work_dpi)
            for f, p in placements.items()}
    a = SheetAnalysis(placements=placements, swapped=swapped)
    if t.exam_no:
        g = t.exam_no.grid
        a.exam_fills = grid_fills(work[g.face], ctx.work_bases[g.face], g.cells, ctx.work_dpi)
    if t.absent:
        a.absent_fill = cell_fill(work[t.absent.face], ctx.work_bases[t.absent.face], t.absent.rect, ctx.work_dpi)
    for blk in t.objective:
        a.objective_fills.append(grid_fills(work[blk.grid.face], ctx.work_bases[blk.grid.face], blk.grid.cells, ctx.work_dpi))
    for bc in t.barcodes:
        p = placements[bc.face]
        src = oriented(images[p.source], p.rotated)
        hit = read_barcode(crop_region(src, p.align.H, bc.rect, p.dpi))
        if hit is None:
            hit = read_barcode(crop_region(src, p.align.H, _expand(bc.rect, 10), p.dpi))
        a.barcodes[bc.key] = hit
    return a


@dataclass
class SheetDecision:
    exam_no: dict | None
    absent: dict | None
    objective: list[dict]
    barcodes: dict[str, dict]
    consistent: bool | None
    warnings: list[dict]


def decide_sheet(ctx: TemplateContext, a: SheetAnalysis, th: Thresholds) -> SheetDecision:
    t = ctx.tpl
    warnings: list[dict] = []
    exam = None
    if t.exam_no and a.exam_fills is not None:
        labels = t.exam_no.grid.labels
        digits, value = [], ""
        for pos, fills in enumerate(a.exam_fills, start=1):
            ans, st = decide(fills, labels, "single", th)
            ok = st == "ok"
            value += ans if ok else "?"
            digits.append({"pos": pos, "value": ans if ok else None, "status": st})
        statuses = {d["status"] for d in digits}
        status = "ok" if statuses == {"ok"} else ("blank" if statuses == {"blank"} else "partial")
        exam = {"value": value, "status": status, "digits": [d for d in digits if d["status"] != "ok"]}
        if status == "partial":
            bad = "、".join(str(d["pos"]) for d in digits if d["status"] != "ok")
            warnings.append({"code": "EXAM_NO_PARTIAL", "field": "student.exam_no", "msg": f"考号第 {bad} 位未识别"})
        elif status == "blank":
            warnings.append({"code": "EXAM_NO_BLANK", "field": "student.exam_no", "msg": "考号未填涂"})
    absent = None
    if a.absent_fill is not None:
        absent = {"value": bool(a.absent_fill >= th.mark), "fill": round(a.absent_fill, 3)}
        if absent["value"]:
            warnings.append({"code": "ABSENT_MARKED", "field": "student.absent", "msg": "缺考标记已填涂"})
    objective = []
    for blk, fills in zip(t.objective, a.objective_fills):
        for k, row in enumerate(fills):
            no = str(blk.start + k)
            ans, st = decide(row, blk.grid.labels, blk.qtype, th)
            objective.append({"no": no, "type": blk.qtype, "answer": ans, "status": st,
                              "fill": {lab: round(float(v), 3) for lab, v in zip(blk.grid.labels, row)}})
            if st != "ok":
                warnings.append({"code": "OBJ_" + st.upper(), "field": f"objective.{no}"})
    barcodes = {}
    for key, hit in a.barcodes.items():
        barcodes[key] = ({"value": hit[0], "symbology": hit[1], "status": "ok"} if hit
                         else {"value": None, "symbology": None, "status": "failed"})
    consistent = None
    sb = barcodes.get("student_barcode")
    if exam and exam["status"] == "ok" and sb and sb["status"] == "ok":
        consistent = sb["value"] == exam["value"]
        if not consistent:
            warnings.append({"code": "ID_MISMATCH", "msg": "填涂考号与条码不一致"})
    if sb and sb["status"] != "ok":
        warnings.append({"code": "BARCODE_MISSING", "field": "student.barcodes.student_barcode", "msg": "考生条码未识别"})
    for f, p in a.placements.items():
        if p.align.method == "features":
            warnings.append({"code": "ALIGN_FALLBACK", "field": f"scan.faces.{f}", "msg": f"{f} 面使用特征匹配兜底定位"})
    return SheetDecision(exam, absent, objective, barcodes, consistent, warnings)
```

实现要点：
- `place_faces` 对每张图 × {0°, 180°} × 每个模板面尝试对齐，用对齐后整页与空白底图（30 DPI）的相关系数打分，再在所有“图→面”的排列中取总分最高且每项 ≥ `MIN_SCORE` 的方案。第一轮只用主定位方式，全部不成立才启用特征匹配兜底。
- `_normalize`：若 H 本身含 180° 旋转（倾斜角绝对值 > 90°），改写为“图像先转 180° + 新 H”，保证 `rotated` 字段如实反映方向。
- 对齐后用 `effective_dpi(H)` 作为该面的真实分辨率，切图尺寸据此计算。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_recognize.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/recognize.py tests/test_recognize.py
git commit -m "feat(engine): 单张识别——方向/正反面判定、物理异常、OMR 与条码判定"
```

---

### Task 9: 结果组装与本地导出

**Files:**
- Create: `src/scan_answer/engine/result.py`、`src/scan_answer/output/local.py`
- Test: `tests/test_result.py`、`tests/test_output.py`

**Interfaces:**
- Consumes: Task 8 的 `TemplateContext`、`SheetAnalysis`、`SheetDecision`、`oriented`；Task 3 的 `crop_region`、`warp_to_template`；Task 5 的 `grid_bbox`
- Produces:
  - `result.SCHEMA_VERSION = "1.0"`；`clean_show_through(img, level=185)`
  - `render_images(ctx, analysis, decision, images) -> dict[相对路径, ndarray]`，键包括 `images/page_{face}.jpg`、`images/exam_no.jpg`、`images/{barcode_key}.jpg`、`images/{field_key}.jpg`、`images/q{no}_{i}.jpg`、非 ok 客观题的 `images/obj_{no}.jpg`
  - `build_result(ctx, analysis, decision, *, seq, batch_id, exam, scanner, raw_paths, image_paths, scan_time=None) -> dict`（设计文档 6.2 的结构）
  - `local.write_json_atomic(path, data)`、`local.write_jpg(path, img, quality=90)`
  - `LocalExporter(batch_dir)`：`student_dir(seq, exam_no)`（考号含 `?` 或为空时只用序号）、`write_student(seq, exam_no, result, images) -> Path`、`write_error(seq, code, msg, images) -> Path`（写到 `errors/{seq:04d}_{code}/`）、`write_manifest(manifest)`

- [ ] **Step 1: 写失败测试**

`tests/test_output.py`:
```python
import json

import numpy as np

from scan_answer.output.local import LocalExporter


def test_student_dir_naming_and_atomic_json(tmp_path):
    ex = LocalExporter(tmp_path / "b")
    p = ex.write_student(7, "100015000123456", {"a": 1}, {"images/x.jpg": np.zeros((10, 10), np.uint8)})
    assert p.parent.name == "0007_100015000123456"
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1}
    assert (p.parent / "images" / "x.jpg").exists()
    assert not list(p.parent.glob("*.tmp"))
    assert ex.student_dir(8, "10001500012345?").name == "0008"
    assert ex.student_dir(9, None).name == "0009"


def test_error_and_manifest(tmp_path):
    ex = LocalExporter(tmp_path / "b")
    e = ex.write_error(3, "BLANK_PAGE", "空白页", [np.zeros((5, 5), np.uint8)])
    assert json.loads(e.read_text(encoding="utf-8"))["code"] == "BLANK_PAGE"
    ex.write_manifest({"sheets": []})
    assert (tmp_path / "b" / "manifest.json").exists()
```

`tests/test_result.py`:
```python
import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import analyze_sheet, decide_sheet
from scan_answer.engine.result import build_result, clean_show_through, render_images
from scan_answer.synth.generate import make_scans, make_sheet


def test_clean_show_through():
    img = np.array([[250, 200, 186, 100]], np.uint8)
    assert clean_show_through(img).tolist() == [[255, 255, 255, 100]]


def test_render_and_build(ctx):
    t = ctx.tpl
    rng = np.random.default_rng(8)
    paper, truth = make_sheet(t, ctx.bases, rng)
    scans = make_scans(t, paper, truth, rng, 300, None)
    a = analyze_sheet(ctx, scans, [300, 300])
    d = decide_sheet(ctx, a, Thresholds())
    images = render_images(ctx, a, d, scans)
    for key in ("images/page_front.jpg", "images/page_back.jpg", "images/exam_no.jpg", "images/student_barcode.jpg",
                "images/name.jpg", "images/name_back.jpg", "images/q56_1.jpg", "images/q56_2.jpg",
                "images/q57_1.jpg", "images/q57_2.jpg"):
        assert key in images, key
    bad = [o["no"] for o in d.objective if o["status"] != "ok"]
    assert all(f"images/obj_{no}.jpg" in images for no in bad)
    h, w = images["images/page_front.jpg"].shape  # 按扫描图实际分辨率输出整页
    assert abs(w / h - 390 / 270) < 0.01 and 4400 < w < 4800
    r = build_result(ctx, a, d, seq=3, batch_id="b", exam={"name": "测试"}, scanner="folder",
                     raw_paths=["../../raw/a.jpg", "../../raw/b.jpg"], image_paths=set(images))
    assert r["seq"] == 3 and r["exam"]["name"] == "测试" and r["manual"] is None
    assert r["scan"]["faces"]["front"]["align_method"] == "anchors"
    assert r["scan"]["faces"]["back"]["align_method"] == "frames"
    assert r["pages"]["raw_1"] == "../../raw/a.jpg"
    assert r["subjective"][0]["images"] == ["images/q56_1.jpg", "images/q56_2.jpg"]
    assert r["student"]["fields"]["name"]["ocr_status"] == "none"
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_output.py tests/test_result.py -q`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现**

`src/scan_answer/output/local.py`:
```python
"""本地目录导出器：students/{seq}_{考号}/result.json + images/，errors/，manifest.json。

所有 JSON 先写临时文件再 os.replace，保证读者不会看到写了一半的文件。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def write_jpg(path: Path, img: np.ndarray, quality: int = 90) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise IOError(f"JPEG 编码失败: {path}")
    path.write_bytes(enc.tobytes())


class LocalExporter:
    def __init__(self, batch_dir: str | Path):
        self.batch_dir = Path(batch_dir)
        self.batch_dir.mkdir(parents=True, exist_ok=True)

    def student_dir(self, seq: int, exam_no: str | None) -> Path:
        safe = exam_no if exam_no and "?" not in exam_no else ""
        return self.batch_dir / "students" / (f"{seq:04d}_{safe}" if safe else f"{seq:04d}")

    def write_student(self, seq: int, exam_no: str | None, result: dict, images: dict[str, np.ndarray]) -> Path:
        """images 的键为相对 student 目录的路径（如 images/q56_1.jpg）。返回 result.json 路径。"""
        d = self.student_dir(seq, exam_no)
        for rel, img in images.items():
            write_jpg(d / rel, img)
        write_json_atomic(d / "result.json", result)
        return d / "result.json"

    def write_error(self, seq: int, code: str, msg: str, images: list[np.ndarray]) -> Path:
        d = self.batch_dir / "errors" / f"{seq:04d}_{code}"
        for i, img in enumerate(images, start=1):
            write_jpg(d / f"face_{i}.jpg", img)
        write_json_atomic(d / "error.json", {"seq": seq, "code": code, "msg": msg})
        return d / "error.json"

    def write_manifest(self, manifest: dict) -> None:
        write_json_atomic(self.batch_dir / "manifest.json", manifest)
```

`src/scan_answer/engine/result.py`:
```python
"""把识别结果组装成 result.json，并裁出要输出的图片。"""
from __future__ import annotations

from datetime import datetime

import numpy as np

from .align import crop_region, warp_to_template
from .omr import grid_bbox
from .recognize import SheetAnalysis, SheetDecision, TemplateContext, oriented

SCHEMA_VERSION = "1.0"


def _pad(rect, mm=2.0):
    x, y, w, h = rect
    return (x - mm, y - mm, w + 2 * mm, h + 2 * mm)


def clean_show_through(img: np.ndarray, level: int = 185) -> np.ndarray:
    """背面去透印：比 level 更浅的像素置白。"""
    out = img.copy()
    out[out > level] = 255
    return out


def render_images(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, images: list[np.ndarray]) -> dict[str, np.ndarray]:
    """返回 {相对路径: 图像}。切图从原分辨率图像上直接裁取。"""
    t = ctx.tpl
    src = {f: oriented(images[p.source], p.rotated) for f, p in a.placements.items()}

    def crop(face, rect):
        p = a.placements[face]
        img = crop_region(src[face], p.align.H, rect, p.dpi)
        return img if face == "front" else clean_show_through(img)

    out: dict[str, np.ndarray] = {}
    for f, p in a.placements.items():
        out[f"images/page_{f}.jpg"] = warp_to_template(src[f], p.align.H, t.paper, p.dpi)
    if t.exam_no:
        out["images/exam_no.jpg"] = crop(t.exam_no.grid.face, _pad(grid_bbox(t.exam_no.grid.cells)))
    for bc in t.barcodes:
        out[f"images/{bc.key}.jpg"] = crop(bc.face, bc.rect)
    for fd in t.fields:
        out[f"images/{fd.key}.jpg"] = crop(fd.face, fd.rect)
    for sq in t.subjective:
        for i, r in enumerate(sq.regions, start=1):
            out[f"images/q{sq.no}_{i}.jpg"] = crop(r.face, r.rect)
    bad = {o["no"] for o in d.objective if o["status"] != "ok"}
    for blk in t.objective:
        for k, cells in enumerate(blk.grid.cells):
            no = str(blk.start + k)
            if no in bad:
                x0 = min(c[0] for c in cells) - 9.0  # 带上题号
                bx = grid_bbox([cells])
                out[f"images/obj_{no}.jpg"] = crop(blk.grid.face, _pad((x0, bx[1], bx[0] + bx[2] - x0, bx[3]), 1.5))
    return out


def build_result(ctx: TemplateContext, a: SheetAnalysis, d: SheetDecision, *, seq: int, batch_id: str,
                 exam: dict, scanner: str, raw_paths: list[str], image_paths: set[str],
                 scan_time: datetime | None = None) -> dict:
    t = ctx.tpl
    faces_info = {f: {"source": p.source + 1, "rotated": p.rotated, "align_method": p.align.method,
                      "skew_deg": round(p.align.skew_deg, 2), "residual_mm": round(p.align.residual_mm, 3),
                      "dpi": round(p.dpi)} for f, p in a.placements.items()}
    student: dict = {"exam_no": None, "omr_fields": {}, "barcodes": {}, "absent": d.absent, "fields": {},
                     "consistent": d.consistent}
    if d.exam_no:
        student["exam_no"] = {**d.exam_no, "image": "images/exam_no.jpg"}
    for key, bc in d.barcodes.items():
        student["barcodes"][key] = {**bc, "image": f"images/{key}.jpg"}
    for fd in t.fields:
        student["fields"][fd.key] = {"label": fd.label, "image": f"images/{fd.key}.jpg", "text": None, "ocr_status": "none"}
    objective = []
    for o in d.objective:
        item = dict(o)
        if f"images/obj_{o['no']}.jpg" in image_paths:
            item["image"] = f"images/obj_{o['no']}.jpg"
        objective.append(item)
    subjective = [{"no": sq.no, "type": sq.qtype, "content": sq.content,
                   "images": [f"images/q{sq.no}_{i}.jpg" for i in range(1, len(sq.regions) + 1)],
                   "text": None, "ocr_status": "none", "ocr_model": None, "ocr_confidence": None}
                  for sq in t.subjective]
    return {
        "schema_version": SCHEMA_VERSION,
        "seq": seq,
        "batch_id": batch_id,
        "template": {"name": t.name, "version": t.version},
        "exam": exam,
        "scan": {"time": (scan_time or datetime.now().astimezone()).isoformat(timespec="seconds"),
                 "scanner": scanner, "faces": faces_info, "swapped": a.swapped},
        "pages": {**{f: f"images/page_{f}.jpg" for f in a.placements},
                  **{f"raw_{i + 1}": p for i, p in enumerate(raw_paths)}},
        "student": student,
        "objective": objective,
        "subjective": subjective,
        "choose_one": [],
        "manual": None,
        "warnings": d.warnings,
    }
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_output.py tests/test_result.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/result.py src/scan_answer/output/local.py tests/test_output.py tests/test_result.py
git commit -m "feat(output): result.json 组装、切图输出与本地目录导出器"
```

---

### Task 10: 批量识别与评估

**Files:**
- Create: `src/scan_answer/batch.py`、`src/scan_answer/evaluate.py`
- Test: `tests/test_batch.py`

**Interfaces:**
- Consumes: Task 2 `load_template`；Task 5 `fit_thresholds`；Task 8 `analyze_sheet/decide_sheet/estimate_dpi/PhysicalError/TemplateContext`；Task 9 `render_images/build_result/LocalExporter`
- Produces:
  - `batch.IMAGE_EXT`、`list_images(dir)`、`read_dpi(path) -> float|None`（读图片元数据）、`pair_images(paths, faces)`、`source_name(path)`（`0001_2.jpg` → `0001`）
  - `run_batch(template_dir, input_dir, out_root, *, exam=None, batch_id=None, scanner="folder") -> Path`（返回批次目录；写 `template.json` 快照、`raw/`、`students/`、`errors/`、`manifest.json`）
  - manifest：`{"schema_version","batch_id","exam","template","thresholds":{"mark","uncertain"},"counts":{"total","ok","physical","warnings"},"sheets":[{"seq","source","files","result"?,"exam_no"?,"warnings"?,"physical"?}]}`
  - `evaluate.Metrics`（`.report() -> dict`、`.details: list[str]`）、`compare(truth, result, physical, m, tag)`、`evaluate_dirs(truth_dir, batch_dir) -> Metrics`

- [ ] **Step 1: 写失败测试**

`tests/test_batch.py`:
```python
import json
import shutil

from scan_answer.batch import run_batch
from scan_answer.evaluate import evaluate_dirs
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR


def test_batch_end_to_end(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 8, seed=11)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    rep = evaluate_dirs(tmp_path / "ds", bd).report()
    assert rep["silent_errors"] == 0 and rep["physical_false"] == 0
    assert rep["normal_accuracy"] == 1.0
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    first = next(s for s in manifest["sheets"] if s.get("result"))
    result = json.loads((bd / first["result"]).read_text(encoding="utf-8"))
    for key in ("schema_version", "seq", "batch_id", "template", "exam", "scan", "pages", "student",
                "objective", "subjective", "choose_one", "manual", "warnings"):
        assert key in result
    assert len(result["objective"]) == 55
    student_dir = (bd / first["result"]).parent
    for q in result["subjective"]:
        for img in q["images"]:
            assert (student_dir / img).exists()


def test_odd_image_count_reports_missing_face(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 2, seed=12)
    (tmp_path / "ds" / "0002_2.jpg").unlink()
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    manifest = json.loads((bd / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["sheets"][-1]["physical"] == "MISSING_FACE"
    assert (bd / "errors" / "0002_MISSING_FACE" / "error.json").exists()
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_batch.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.batch'`）

- [ ] **Step 3: 实现**

`src/scan_answer/evaluate.py`:
```python
"""把识别结果与模拟测试集的 truth.json 对比，输出指标。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Metrics:
    sheets: int = 0
    physical_expected: int = 0
    physical_caught: int = 0
    physical_false: int = 0  # 不该报物理异常却报了（不含 fold，fold 两种结果都允许）
    objective_total: int = 0
    objective_correct: int = 0  # 答案与状态都符合预期
    silent_errors: int = 0  # 状态 ok 但答案错
    normal_total: int = 0
    normal_correct: int = 0
    exam_digits_total: int = 0
    exam_digits_correct: int = 0
    barcode_total: int = 0
    barcode_correct: int = 0
    details: list[str] = field(default_factory=list)

    def report(self) -> dict:
        def r(a, b):
            return round(a / b, 5) if b else None
        return {
            "sheets": self.sheets,
            "physical_detect_rate": r(self.physical_caught, self.physical_expected),
            "physical_false": self.physical_false,
            "objective_accuracy": r(self.objective_correct, self.objective_total),
            "normal_accuracy": r(self.normal_correct, self.normal_total),
            "silent_errors": self.silent_errors,
            "exam_digit_accuracy": r(self.exam_digits_correct, self.exam_digits_total),
            "barcode_accuracy": r(self.barcode_correct, self.barcode_total),
        }


def compare(truth: dict, result: dict | None, physical: str | None, m: Metrics, tag: str) -> None:
    """result 为 result.json 内容；physical 为物理异常代码（此时 result 为 None）。"""
    m.sheets += 1
    if truth.get("physical"):
        m.physical_expected += 1
        if physical == truth["physical"]:
            m.physical_caught += 1
        else:
            m.details.append(f"{tag}: 期望物理异常 {truth['physical']}，实际 {physical}")
        return
    if physical:
        if truth.get("special") != "fold":
            m.physical_false += 1
            m.details.append(f"{tag}: 误报物理异常 {physical}")
        return
    got = {o["no"]: o for o in result["objective"]}
    for exp in truth["objective"]:
        o = got[exp["no"]]
        m.objective_total += 1
        ok_answer = o["answer"] == exp["answer"]
        # 浅涂：判为 uncertain 或（阈值偏低时）判为 ok 都可以，但答案必须对
        status_ok = o["status"] == exp["status"] or (exp["case"] == "light" and o["status"] == "ok")
        if ok_answer and status_ok:
            m.objective_correct += 1
        else:
            m.details.append(f"{tag} 第{exp['no']}题 {exp['case']}: 期望 {exp['answer']}/{exp['status']}，"
                             f"实际 {o['answer']}/{o['status']} fill={o['fill']}")
        if o["status"] == "ok" and not ok_answer:
            m.silent_errors += 1
        if exp["case"] == "normal":
            m.normal_total += 1
            m.normal_correct += int(ok_answer and o["status"] == "ok")
    te, re_ = truth.get("exam_no"), result["student"].get("exam_no")
    if te and re_:
        for a, b in zip(te["value"], re_["value"]):
            m.exam_digits_total += 1
            m.exam_digits_correct += int(a == b)
    if truth.get("barcode"):
        m.barcode_total += 1
        got_bc = result["student"]["barcodes"].get("student_barcode", {}).get("value")
        m.barcode_correct += int(got_bc == truth["barcode"])


def evaluate_dirs(truth_dir: str | Path, batch_dir: str | Path) -> Metrics:
    """truth_dir 为模拟测试集目录；batch_dir 为 recognize 输出的批次目录（含 manifest.json）。"""
    truth_dir, batch_dir = Path(truth_dir), Path(batch_dir)
    manifest = json.loads((batch_dir / "manifest.json").read_text(encoding="utf-8"))
    m = Metrics()
    for item in manifest["sheets"]:
        truth = json.loads((truth_dir / f"{item['source']}.truth.json").read_text(encoding="utf-8"))
        result = None
        if item.get("result"):
            result = json.loads((batch_dir / item["result"]).read_text(encoding="utf-8"))
        compare(truth, result, item.get("physical"), m, item["source"])
    return m
```

`src/scan_answer/batch.py`:
```python
"""文件夹批量识别（第一期的命令行入口；第二期流水线会复用这里的两遍流程）。

第一遍：逐张 analyze_sheet，收集所有涂黑度；拟合批次阈值。
第二遍：decide_sheet + 裁图 + 写出。
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .engine.omr import fit_thresholds
from .engine.recognize import (PhysicalError, SheetAnalysis, TemplateContext, analyze_sheet, decide_sheet,
                               estimate_dpi)
from .engine.result import build_result, render_images
from .output.local import LocalExporter
from .template import load_template

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}


def list_images(input_dir: str | Path) -> list[Path]:
    return sorted(p for p in Path(input_dir).iterdir() if p.suffix.lower() in IMAGE_EXT)


def read_dpi(path: Path) -> float | None:
    """读取图片文件记录的 DPI（扫描仪输出的 JPEG/TIFF 通常都有）；没有则返回 None。"""
    with Image.open(path) as im:
        dpi = im.info.get("dpi")
    if dpi and dpi[0] and float(dpi[0]) > 50:
        return float(dpi[0])
    return None


def pair_images(paths: list[Path], faces: int) -> list[list[Path]]:
    """按文件名顺序每 faces 张一组；最后不足一组的单独成组（后续报 MISSING_FACE）。"""
    return [paths[i:i + faces] for i in range(0, len(paths), faces)]


def source_name(path: Path) -> str:
    stem = path.stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def run_batch(template_dir: str | Path, input_dir: str | Path, out_root: str | Path, *,
              exam: dict | None = None, batch_id: str | None = None, scanner: str = "folder") -> Path:
    tpl, bases = load_template(template_dir)
    ctx = TemplateContext(tpl, bases)
    batch_id = batch_id or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
    batch_dir = Path(out_root) / batch_id
    exporter = LocalExporter(batch_dir)
    shutil.copy(Path(template_dir) / "template.json", batch_dir / "template.json")
    groups = pair_images(list_images(input_dir), len(tpl.faces))

    # 第一遍
    analyses: list[SheetAnalysis | PhysicalError] = []
    all_fills = []
    for group in groups:
        imgs = [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in group]
        dpis = [read_dpi(p) or estimate_dpi(img, tpl.paper) for p, img in zip(group, imgs)]
        try:
            a = analyze_sheet(ctx, imgs, dpis)
            analyses.append(a)
            all_fills.extend(f.ravel() for f in a.objective_fills)
            if a.exam_fills is not None:
                all_fills.append(a.exam_fills.ravel())
        except PhysicalError as e:
            analyses.append(e)
    th = fit_thresholds(np.concatenate(all_fills) if all_fills else np.zeros(0))

    # 第二遍
    sheets, counts = [], {"total": len(groups), "ok": 0, "physical": 0, "warnings": 0}
    for seq, (group, a) in enumerate(zip(groups, analyses), start=1):
        imgs = [cv2.imread(str(p), cv2.IMREAD_GRAYSCALE) for p in group]
        raw_rel = []
        for p in group:
            dst = batch_dir / "raw" / f"{seq:04d}_{p.name}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(p, dst)
            raw_rel.append(f"../../raw/{dst.name}")
        item = {"seq": seq, "source": source_name(group[0]), "files": [p.name for p in group]}
        if isinstance(a, PhysicalError):
            exporter.write_error(seq, a.code, a.msg, imgs)
            item["physical"] = a.code
            counts["physical"] += 1
        else:
            d = decide_sheet(ctx, a, th)
            images = render_images(ctx, a, d, imgs)
            result = build_result(ctx, a, d, seq=seq, batch_id=batch_id, exam=exam or {}, scanner=scanner,
                                  raw_paths=raw_rel, image_paths=set(images))
            exam_value = d.exam_no["value"] if d.exam_no else None
            path = exporter.write_student(seq, exam_value, result, images)
            item.update({"result": str(path.relative_to(batch_dir)), "exam_no": exam_value,
                         "warnings": len(d.warnings)})
            counts["ok"] += 1
            counts["warnings"] += len(d.warnings)
        sheets.append(item)
    exporter.write_manifest({
        "schema_version": "1.0", "batch_id": batch_id, "exam": exam or {},
        "template": {"name": tpl.name, "version": tpl.version, "snapshot": "template.json"},
        "thresholds": {"mark": round(th.mark, 3), "uncertain": round(th.uncertain, 3)},
        "counts": counts, "sheets": sheets,
    })
    return batch_dir
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_batch.py -q`
Expected: `2 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/batch.py src/scan_answer/evaluate.py tests/test_batch.py
git commit -m "feat: 文件夹批量识别（两遍流程）与模拟集评估"
```

---

### Task 11: 命令行、回归测试与输出文档

**Files:**
- Create: `src/scan_answer/cli.py`、`docs/output-format.md`
- Test: `tests/test_cli.py`、`tests/test_regression.py`

**Interfaces:**
- Consumes: `run_batch`、`evaluate_dirs`、`generate_dataset`、`load_template`
- Produces: 命令 `scan-answer synth --template T --out D [--count N --seed S --dpi 300]`、`scan-answer recognize --template T --input D --out O [--batch-id B --exam JSON]`、`scan-answer eval --truth D --batch O/B`（指标未达标返回 1）；`cli.GATES`

- [ ] **Step 1: 写失败测试**

`tests/test_cli.py`:
```python
from scan_answer.cli import main

from conftest import TEMPLATE_DIR


def test_cli_synth_recognize_eval(tmp_path):
    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "4", "--seed", "5"]) == 0
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                 "--out", str(tmp_path / "out"), "--batch-id", "b"]) == 0
    assert main(["eval", "--truth", str(tmp_path / "ds"), "--batch", str(tmp_path / "out" / "b")]) == 0
```

`tests/test_regression.py`:
```python
"""完整回归：200 张模拟卡，验收指标见设计文档 9.2。运行：pytest -m slow"""
import pytest

from scan_answer.batch import run_batch
from scan_answer.evaluate import evaluate_dirs
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR


@pytest.mark.slow
def test_regression_200(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 200, seed=2026)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="r")
    m = evaluate_dirs(tmp_path / "ds", bd)
    rep = m.report()
    assert rep["silent_errors"] == 0, m.details
    assert rep["physical_false"] == 0, m.details
    assert rep["physical_detect_rate"] in (None, 1.0), m.details
    assert rep["normal_accuracy"] >= 0.999, m.details
    assert rep["objective_accuracy"] >= 0.999, m.details
    assert rep["exam_digit_accuracy"] >= 0.999, m.details
    assert rep["barcode_accuracy"] >= 0.99, m.details
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.cli'`）

- [ ] **Step 3: 实现**

`src/scan_answer/cli.py`:
```python
"""命令行入口：scan-answer synth | recognize | eval"""
from __future__ import annotations

import argparse
import json
import sys

from .batch import run_batch
from .evaluate import evaluate_dirs
from .synth.generate import generate_dataset
from .template import load_template

GATES = {"normal_accuracy": 0.999, "objective_accuracy": 0.999, "exam_digit_accuracy": 0.999,
         "physical_detect_rate": 1.0}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="scan-answer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("synth", help="以模板空白底图生成模拟测试集")
    s.add_argument("--template", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--count", type=int, default=50)
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--dpi", type=int, default=300)

    r = sub.add_parser("recognize", help="识别一个文件夹里的扫描图")
    r.add_argument("--template", required=True)
    r.add_argument("--input", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--batch-id")
    r.add_argument("--exam", help="考试元信息 JSON 字符串")

    e = sub.add_parser("eval", help="对比识别结果与 truth.json，输出指标；不达标返回 1")
    e.add_argument("--truth", required=True)
    e.add_argument("--batch", required=True)

    a = ap.parse_args(argv)
    if a.cmd == "synth":
        t, bases = load_template(a.template)
        generate_dataset(t, bases, a.out, a.count, seed=a.seed, out_dpi=a.dpi)
        print(f"已生成 {a.count} 张模拟卡 -> {a.out}")
        return 0
    if a.cmd == "recognize":
        exam = json.loads(a.exam) if a.exam else {}
        bd = run_batch(a.template, a.input, a.out, exam=exam, batch_id=a.batch_id)
        print(bd)
        return 0
    m = evaluate_dirs(a.truth, a.batch)
    rep = m.report()
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    for line in m.details[:50]:
        print(" -", line)
    failed = [k for k, v in GATES.items() if rep.get(k) is not None and rep[k] < v]
    if rep["silent_errors"] or rep["physical_false"]:
        failed.append("silent_errors/physical_false")
    if failed:
        print("未达标:", ", ".join(failed))
        return 1
    print("全部达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`docs/output-format.md`:
````markdown
# 输出格式（schema_version 1.0）

## 目录结构

```
<输出根目录>/<batch_id>/
  template.json                       # 本批次使用的模板快照
  manifest.json                       # 批次汇总
  raw/{seq:04d}_{原文件名}             # 原始扫描图副本
  students/{seq:04d}_{考号}/           # 考号识别不完整时只用 {seq:04d}
    result.json
    images/*.jpg
  errors/{seq:04d}_{物理异常代码}/
    error.json                        # {"seq", "code", "msg"}
    face_1.jpg, face_2.jpg
```

## 状态取值

| 字段 | 取值 |
|---|---|
| `status`（客观题、考号、考号逐位、条码） | `ok` / `blank` / `multiple` / `uncertain` / `partial` / `failed` |
| `ocr_status` | `none` / `pending` / `done` / `failed`（本期固定 `none`） |
| `scan.faces.*.align_method` | `anchors` / `frames` / `features` |

## 物理异常代码（不生成 result.json，写入 errors/）

| 代码 | 含义 | 现场处理 |
|---|---|---|
| `ALIGN_FAILED` | 所有面都无法定位 | 检查折角、放反，重扫 |
| `FACE_MISMATCH` | 能定位但与模板正反面对不上（扫错卡、混入其他纸） | 核对后重扫或作废 |
| `BLANK_PAGE` | 有一面几乎全白 | 检查是否漏进纸、双张 |
| `MISSING_FACE` | 图像数量不足一张卡的面数 | 补扫 |
| `SKEW_TOO_LARGE` | 倾斜超过 8° | 摆正后重扫 |

## 数据警告代码（result.json 的 `warnings[].code`）

| 代码 | 含义 | `field` |
|---|---|---|
| `EXAM_NO_PARTIAL` | 考号部分位未识别（未涂/多涂/涂浅），对应位输出 `?` | `student.exam_no` |
| `EXAM_NO_BLANK` | 考号整体未填涂 | `student.exam_no` |
| `ABSENT_MARKED` | 缺考标记已填涂 | `student.absent` |
| `OBJ_BLANK` | 客观题未作答 | `objective.{题号}` |
| `OBJ_MULTIPLE` | 单选题多涂 | `objective.{题号}` |
| `OBJ_UNCERTAIN` | 涂得浅、擦除不净或与其他选项区分度不足 | `objective.{题号}` |
| `ID_MISMATCH` | 填涂考号与考生条码不一致 | — |
| `BARCODE_MISSING` | 考生条码未识别（未贴或损坏） | `student.barcodes.student_barcode` |
| `ALIGN_FALLBACK` | 该面主定位失败，使用特征匹配兜底 | `scan.faces.{面}` |

## result.json 字段

| 字段 | 说明 |
|---|---|
| `seq`、`batch_id` | 扫描序号、批次号 |
| `template` | `{"name", "version"}` |
| `exam` | 扫描前填写的考试元信息（原样写入） |
| `scan` | `time`、`scanner`、`swapped`（正反面是否互换）、`faces.{面}`：`source`（第几张输入图）、`rotated`、`align_method`、`skew_deg`、`residual_mm`、`dpi` |
| `pages` | `front`/`back` 为对齐后的整页图；`raw_1`/`raw_2` 为原图相对路径 |
| `student.exam_no` | `value`（未识别位为 `?`）、`status`、`digits`（只列出非 ok 的位）、`image` |
| `student.barcodes.{key}` | `value`、`symbology`、`status`、`image` |
| `student.absent` | `value`、`fill` |
| `student.fields.{key}` | `label`、`image`、`text`、`ocr_status` |
| `student.consistent` | 考号与条码都识别成功时比较结果，否则 `null` |
| `objective[]` | `no`、`type`、`answer`（选中选项按顺序拼接）、`status`、`fill`（每个选项的涂黑度 0~1）、`image`（仅非 ok 时） |
| `subjective[]` | `no`、`type`、`content`、`images`（每个区域一张，按模板顺序）、`text`、`ocr_status`、`ocr_model`、`ocr_confidence` |
| `omr_fields`、`choose_one` | 本期为 `{}`、`[]` |
| `manual` | 预留人工校对，本期 `null` |
| `warnings[]` | `code`、可选 `field`、可选 `msg` |
````

- [ ] **Step 4: 运行快速测试与命令行**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: `1 passed`

```bash
.venv/bin/scan-answer synth --template templates/guanzong199 --out /tmp/sa_ds --count 20 --seed 1
.venv/bin/scan-answer recognize --template templates/guanzong199 --input /tmp/sa_ds --out /tmp/sa_out --batch-id demo
.venv/bin/scan-answer eval --truth /tmp/sa_ds --batch /tmp/sa_out/demo
```
Expected: 最后一行输出 `全部达标`，退出码 0。打开 `/tmp/sa_out/demo/students/` 下任一学生目录，确认 `result.json` 与 `images/` 存在。

- [ ] **Step 5: 运行完整回归（约 4 分钟）**

Run: `.venv/bin/python -m pytest -m slow -q`
Expected: `1 passed`

- [ ] **Step 6: 运行全部快速测试**

Run: `.venv/bin/python -m pytest -q`
Expected: 全部通过（36 passed, 1 deselected）

- [ ] **Step 7: Commit**

```bash
git add src/scan_answer/cli.py docs/output-format.md tests/test_cli.py tests/test_regression.py
git commit -m "feat(cli): scan-answer synth/recognize/eval 命令与 200 张回归测试"
```

---

## 与设计文档的对应与偏差

| 设计文档 | 本计划 |
|---|---|
| 4.2 区域类型 | 实现：定位块、定位外框、考号、条码、缺考、信息栏、客观题块、主观题、忽略区。**未实现**（模型留到界面计划一并加）：同步头、自定义填涂字段、选做组 —— 199 卡用不到，`result.json` 中 `omr_fields` 输出 `{}`、`choose_one` 输出 `[]` |
| 4.3 定位兜底链 | 实现：主方式 → 特征匹配 → 物理异常。**“借用正面推算”顺延到计划 4**：需要真实扫描仪的双面几何标定数据 |
| 5.3 OMR | 全部实现，判定规则见 Task 5 |
| 5.4 物理异常 | 实现图像可判定的 5 类；扫描仪上报的双张、卡纸属于设备事件，在计划 2/4 接入 |
| 6 输出 | 全部字段已输出；`ocr_status` 固定 `none`（OCR 在计划 2） |
| 9.1–9.2 模拟测试集与指标 | 已覆盖场景：正常/漏涂/多涂/浅涂/擦改客观题、考号漏涂与多涂一位、条码、缺考、折角/旋转/交换/空白页物理异常、中间缺面配对。**未覆盖**：数据集中的奇数页/正反面不匹配/倾斜超限（有单元测试覆盖部分）、对齐误差与吞吐量未作为自动化指标 |
| 6 输出差异 | `pages` 用 `raw_1`/`raw_2`（而非设计文档的 `raw_front`/`raw_back`）；`scan` 顶层无 `dpi`/`color`（dpi 在 `scan.faces.*` 中）；`template.json` 快照在批次目录 |
| 条码 | 未实现全页搜索兜底（只扩大 10mm 区域重试）；`paper_barcode` 失败不出警告 |
| 5.2① 配对规则 | 文件名带 `_N` 后缀时按前缀分组（修复中间缺面串卷），否则按排序顺序两两配对 |
