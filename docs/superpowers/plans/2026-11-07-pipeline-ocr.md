# 流水线与手写转写（第 1 期 · 计划 2/4）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把计划 1 的“一次性两遍批量识别”升级为可边扫边识别的流水线：统一的图像源接口、多进程识别、扫描中实时出结果（先用默认阈值、满 100 张改用拟合值）、结束批次时按整批阈值重新判定、物理异常的重扫与作废、程序重启后继续批次、扫描仪事件记录；并接入兼容 OpenAI 格式的大模型做后台手写转写（可选、可重试）。命令行 `recognize` 增加 `--workers`、`--ocr-config`，新增 `ocr` 子命令。

**Architecture:** `source/` 定义图像源接口（`SheetImages` / `DeviceEvent`），`FolderSource` 负责文件夹配对；`pipeline/worker.py` 是在识别进程里执行的纯函数（识别一张卡并写出 / 按最终阈值重新判定），`pipeline/session.py` 管批次目录、序号、原图、阈值和 manifest（只在主进程），`pipeline/runner.py` 调度：从图像源取卡 → 保存原图 → 提交进程池（在途 ≤ workers×2）→ 收集结果、发事件。`engine/state.py` 把识别中间量存成 `state/{seq}.json`，结束批次时不必重新对齐。`ocr/` 是独立的后台线程池，按项转写并用文件锁（`output/jsonfile.py`）回写 `result.json`，与重新判定互不覆盖。界面（计划 3）只需订阅事件、调用 `run / rescan / void / finalize`。

**Tech Stack:** 与计划 1 相同（Python 3.14、NumPy、OpenCV、zxing-cpp、Pillow；pytest、PyMuPDF）。多进程用标准库 `concurrent.futures.ProcessPoolExecutor`（统一 `spawn` 启动方式，与 Windows 一致）；HTTP 用标准库 `urllib`，**不新增依赖**。

**设计文档:** `docs/superpowers/specs/2026-11-06-scan-answer-design.md`（第 3、5.4、6.3、7 节）。**前置:** 计划 1 已完成（`docs/superpowers/plans/2026-11-06-recognition-core.md`）。

**用户已确认的决策：** 扫描中先用默认阈值实时出结果，成功识别满约 100 张后改用已扫部分的拟合值；点“结束批次”时用整批拟合值重新判定，有变化的卡重写 `result.json`，manifest 标记 `finalized`。

## Global Constraints

- 沿用计划 1 的全部全局约束（依赖、mm 坐标、原子 JSON 写入、中文注释英文标识符、状态取值、物理异常与数据警告代码、验收指标）
- 不新增运行依赖：HTTP 用 `urllib`，并发用 `concurrent.futures`、`threading`
- 进程池一律 `multiprocessing.get_context("spawn")`；交给进程池的 job 只含可序列化的基本类型（路径用 `str`）
- 新增数据警告代码 `OCR_FAILED`（`field` 为 `subjective.{题号}` 或 `student.fields.{键}`）
- `result.json` 的跨进程改写（重新判定、转写回写）一律经 `output/jsonfile.update_json`（锁文件 `<文件>.lock` + 原子写）
- 学生目录名 `{seq:04d}_{考号}` 在首次识别时确定，重新判定不改目录名
- 原图存为 `raw/{seq:04d}_{原文件名}`；重扫时先删除该序号的旧原图、旧输出与 `state/{seq:04d}.json`
- 阈值常量：`REFIT_MIN = 100`、`REFIT_EVERY = 50`；manifest 最短写盘间隔 `MANIFEST_INTERVAL = 2.0` 秒（关键操作强制写）
- 事件只在调用 `run()` / `rescan()` / `void()` / `finalize()` 的线程里发出（转写进度事件在转写线程里发出）
- 测试运行：`.venv/bin/python -m pytest -q`（快速）；`.venv/bin/python -m pytest -m slow -q`（200 张回归，约 4 分钟）

## 文件结构

```
src/scan_answer/
  output/jsonfile.py        # 新增：file_lock / read_json / update_json（跨进程安全的读-改-写）
  engine/state.py           # 新增：SheetAnalysis <-> JSON，analysis_fills
  engine/result.py          # 修改：build_result 增加 ocr 参数（pending / none）
  source/
    __init__.py
    base.py                 # 新增：SheetImages、DeviceEvent、ImageSource 协议
    folder.py               # 新增：FolderSource + 从 batch.py 迁来的配对/DPI 函数
  pipeline/
    __init__.py
    worker.py               # 新增：SheetJob/process_sheet、FinalizeJob/finalize_sheet、clear_outputs、state_path
    events.py               # 新增：SheetEvent、ThresholdEvent、DeviceNotice、VoidEvent、FinalizedEvent、OcrProgress
    session.py              # 新增：BatchSession（create / open / save_raw / record / void / finalize 辅助 / manifest）
    runner.py               # 新增：BatchPipeline（run / rescan / void / finalize）
  ocr/
    __init__.py
    config.py               # 新增：OcrConfig、load_ocr_config、save_ocr_config
    prompts.py              # 新增：系统提示与按内容类型的提示词
    client.py               # 新增：OcrClient（OpenAI 兼容、重试、ping）、OcrError、Transcription
    queue.py                # 新增：OcrQueue、reset_failed、enable_ocr
  batch.py                  # 修改：Task 3 改为从 source.folder 导入；Task 7 改为基于流水线实现
  cli.py                    # 修改：--workers、--ocr-config、ocr 子命令、进度输出
tests/
  ocr_mock.py               # 新增：OpenAI 兼容接口的本地模拟服务
  test_jsonfile.py test_state.py test_folder_source.py test_worker.py test_pipeline.py
  test_ocr_client.py test_ocr_queue.py   # 新增
  test_cli.py               # 修改：增加 workers / OCR / ocr 子命令用例
docs/output-format.md       # 修改：state/、manifest 字段、OCR 说明与 OCR_FAILED
```

---

### Task 1: 跨进程安全的 JSON 读-改-写

**Files:**
- Create: `src/scan_answer/output/jsonfile.py`
- Test: `tests/test_jsonfile.py`

**Interfaces:**
- Consumes: `output.local.write_json_atomic(path, data)`（计划 1）
- Produces: `STALE_SECONDS = 60.0`；`file_lock(path, timeout=30.0, poll=0.02)`（上下文管理器，锁文件 `<path>.lock`，超时抛 `TimeoutError`，超过 60 秒的锁视为过期并清除）；`read_json(path) -> dict`；`update_json(path, fn) -> dict`（`fn` 可原地修改返回 None，或返回新 dict）

- [ ] **Step 1: 写失败测试**

`tests/test_jsonfile.py`:
```python
import json
import os
import threading
import time

import pytest

from scan_answer.output.jsonfile import file_lock, read_json, update_json
from scan_answer.output.local import write_json_atomic


def test_update_json_serializes_concurrent_writers(tmp_path):
    p = tmp_path / "r.json"
    write_json_atomic(p, {"n": 0})

    def bump():
        for _ in range(20):
            update_json(p, lambda d: d.update(n=d["n"] + 1))

    ts = [threading.Thread(target=bump) for _ in range(5)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert read_json(p)["n"] == 100
    assert not list(tmp_path.glob("*.lock")) and not list(tmp_path.glob("*.tmp"))


def test_update_json_accepts_returned_dict(tmp_path):
    p = tmp_path / "r.json"
    write_json_atomic(p, {"a": 1})
    assert update_json(p, lambda d: {"b": 2}) == {"b": 2}
    assert json.loads(p.read_text(encoding="utf-8")) == {"b": 2}


def test_stale_lock_is_broken_and_live_lock_times_out(tmp_path):
    p = tmp_path / "r.json"
    lock = tmp_path / "r.json.lock"
    lock.write_text("")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    with file_lock(p, timeout=1):  # 过期锁被清除
        pass
    lock.write_text("")
    with pytest.raises(TimeoutError):
        with file_lock(p, timeout=0.2):
            pass
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_jsonfile.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.output.jsonfile'`）

- [ ] **Step 3: 实现**

`src/scan_answer/output/jsonfile.py`:
```python
"""跨进程安全的 JSON 读-改-写。

结束批次时的重新判定（在识别进程里）和手写转写（在主进程线程里）都会改写同一个 result.json，
用一个 `<文件>.lock` 锁文件（O_CREAT | O_EXCL 原子创建）串行化，Windows / macOS 通用。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .local import write_json_atomic

STALE_SECONDS = 60.0  # 锁文件超过该时长视为持有者已崩溃


@contextmanager
def file_lock(path: str | Path, timeout: float = 30.0, poll: float = 0.02) -> Iterator[None]:
    lock = Path(str(path) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > STALE_SECONDS:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() > deadline:
                raise TimeoutError(f"等待文件锁超时: {lock}")
            time.sleep(poll)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def read_json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def update_json(path: str | Path, fn: Callable[[dict], dict | None]) -> dict:
    """加锁读取 JSON，交给 fn 修改（可原地改并返回 None，或返回新 dict），再原子写回。"""
    path = Path(path)
    with file_lock(path):
        data = read_json(path)
        new = fn(data)
        if new is not None:
            data = new
        write_json_atomic(path, data)
    return data
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_jsonfile.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/output/jsonfile.py tests/test_jsonfile.py
git commit -m "feat(output): 跨进程安全的 JSON 读-改-写（锁文件 + 原子替换）"
```

---

### Task 2: 识别中间量的序列化

**Files:**
- Create: `src/scan_answer/engine/state.py`
- Test: `tests/test_state.py`

**Interfaces:**
- Consumes: `engine.recognize.SheetAnalysis / FacePlacement`、`engine.align.AlignResult`（计划 1）
- Produces: `analysis_to_dict(a) -> dict`（全部为 JSON 基本类型，涂黑度保留完整精度）；`analysis_from_dict(d) -> SheetAnalysis`（同样的阈值下判定结果与原对象完全一致）；`analysis_fills(a) -> np.ndarray`（客观题 + 考号的全部涂黑度，一维）

- [ ] **Step 1: 写失败测试**

`tests/test_state.py`:
```python
import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.engine.recognize import analyze_sheet, decide_sheet
from scan_answer.engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from scan_answer.output.local import write_json_atomic
from scan_answer.output.jsonfile import read_json
from scan_answer.synth.generate import make_scans, make_sheet


def test_round_trip_gives_same_decision(ctx, tmp_path):
    rng = np.random.default_rng(21)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    a = analyze_sheet(ctx, make_scans(ctx.tpl, paper, truth, rng, 300, "rot180"), [300, 300])
    write_json_atomic(tmp_path / "s.json", analysis_to_dict(a))  # 必须能写成 JSON
    b = analysis_from_dict(read_json(tmp_path / "s.json"))
    th = Thresholds()
    da, db = decide_sheet(ctx, a, th), decide_sheet(ctx, b, th)
    assert da.objective == db.objective and da.exam_no == db.exam_no and da.barcodes == db.barcodes
    assert b.placements["front"].rotated == 180
    assert np.allclose(b.placements["front"].align.H, a.placements["front"].align.H)
    assert analysis_fills(b).shape == analysis_fills(a).shape == (55 * 5 + 15 * 10,)
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_state.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.engine.state'`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/state.py`:
```python
"""SheetAnalysis 与 JSON 互转：结束批次时据此重新判定，不必重新对齐和计算涂黑度。"""
from __future__ import annotations

import numpy as np

from .align import AlignResult
from .recognize import FacePlacement, SheetAnalysis


def analysis_to_dict(a: SheetAnalysis) -> dict:
    return {
        "swapped": a.swapped,
        "placements": {f: {"source": p.source, "rotated": p.rotated, "dpi": p.dpi,
                           "method": p.align.method, "H": np.asarray(p.align.H).tolist(),
                           "skew_deg": p.align.skew_deg, "residual_mm": p.align.residual_mm}
                       for f, p in a.placements.items()},
        "exam_fills": None if a.exam_fills is None else np.asarray(a.exam_fills, dtype=np.float64).tolist(),
        "absent_fill": a.absent_fill,
        "objective_fills": [np.asarray(f, dtype=np.float64).tolist() for f in a.objective_fills],
        "barcodes": {k: (list(v) if v else None) for k, v in a.barcodes.items()},
    }


def analysis_from_dict(d: dict) -> SheetAnalysis:
    placements = {
        f: FacePlacement(source=int(p["source"]), rotated=int(p["rotated"]), dpi=float(p["dpi"]),
                         align=AlignResult(True, p["method"], H=np.array(p["H"], dtype=np.float64),
                                           skew_deg=float(p["skew_deg"]), residual_mm=float(p["residual_mm"])))
        for f, p in d["placements"].items()
    }
    return SheetAnalysis(
        placements=placements,
        swapped=bool(d["swapped"]),
        exam_fills=None if d["exam_fills"] is None else np.array(d["exam_fills"], dtype=np.float32),
        absent_fill=d["absent_fill"],
        objective_fills=[np.array(f, dtype=np.float32) for f in d["objective_fills"]],
        barcodes={k: (tuple(v) if v else None) for k, v in d["barcodes"].items()},
    )


def analysis_fills(a: SheetAnalysis) -> np.ndarray:
    """拟合批次阈值用的全部涂黑度（客观题 + 考号）。"""
    parts = [np.asarray(f).ravel() for f in a.objective_fills]
    if a.exam_fills is not None:
        parts.append(np.asarray(a.exam_fills).ravel())
    return np.concatenate(parts) if parts else np.zeros(0, np.float32)
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_state.py -q`
Expected: `1 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/state.py tests/test_state.py
git commit -m "feat(engine): 识别中间量与 JSON 互转，供结束批次时重新判定"
```

---

### Task 3: 图像源接口与文件夹图像源

**Files:**
- Create: `src/scan_answer/source/__init__.py`（空文件）、`src/scan_answer/source/base.py`、`src/scan_answer/source/folder.py`
- Modify: `src/scan_answer/batch.py`（整文件替换：删除迁走的配对/DPI 函数，改为从 `source.folder` 导入并保留旧名字，`run_batch` 主体不变）
- Test: `tests/test_folder_source.py`（`tests/test_batch.py` 不改，必须继续通过）

**Interfaces:**
- Produces:
  - `source.base.SheetImages(source: str, names: list[str], dpis: list[float|None], paths: list[Path] = [], images: list[ndarray] = [])`
  - `source.base.DeviceEvent(kind: str, message: str)`（kind：`double_feed` / `paper_jam` / `no_paper` / `error`）
  - `source.base.ImageSource`（Protocol：`sheets() -> Iterator[SheetImages | DeviceEvent]`）
  - `source.folder`：`IMAGE_EXT`、`list_images`、`read_dpi`、`choose_dpi`（新增：`estimated <= 0` 时返回 `meta or estimated`）、`source_name`、`pair_images`、`FolderSource(input_dir, faces).sheets()`
  - `scan_answer.batch` 仍可导入 `choose_dpi, list_images, pair_images, read_dpi, source_name`

- [ ] **Step 1: 写失败测试**

`tests/test_folder_source.py`:
```python
from scan_answer.source.base import SheetImages
from scan_answer.source.folder import FolderSource, choose_dpi
from scan_answer.synth.generate import generate_dataset


def test_folder_source_groups_faces_and_reads_dpi(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path, 3, seed=5, out_dpi=200)
    (tmp_path / "0002_2.jpg").unlink()
    sheets = list(FolderSource(tmp_path, 2).sheets())
    assert [s.source for s in sheets] == ["0001", "0002", "0003"]
    assert all(isinstance(s, SheetImages) for s in sheets)
    assert sheets[0].names == ["0001_1.jpg", "0001_2.jpg"] and len(sheets[1].paths) == 1
    assert sheets[0].dpis == [200.0, 200.0]


def test_choose_dpi_handles_zero_estimate():
    assert choose_dpi(None, 0) == 0
    assert choose_dpi(300.0, 0) == 300.0
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_folder_source.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.source'`）

- [ ] **Step 3: 实现**

`src/scan_answer/source/base.py`:
```python
"""图像源接口：按顺序产出“一张卡的各面图像”或“设备事件”。

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
```

`src/scan_answer/source/folder.py`:
```python
"""文件夹图像源：按文件名配对正反面（macOS 上的主要输入方式，Windows 上作兜底）。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

from PIL import Image

from .base import SheetImages

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
_SUFFIX = re.compile(r"_(\d+)$")


def list_images(input_dir: str | Path) -> list[Path]:
    return sorted(p for p in Path(input_dir).iterdir() if p.suffix.lower() in IMAGE_EXT)


def read_dpi(path: Path) -> float | None:
    """读取图片文件记录的 DPI（扫描仪输出的 JPEG/TIFF 通常都有）；没有或文件损坏返回 None。"""
    try:
        with Image.open(path) as im:
            dpi = im.info.get("dpi")
    except (Image.UnidentifiedImageError, OSError):
        return None
    if dpi and dpi[0] and float(dpi[0]) > 50:
        return float(dpi[0])
    return None


def choose_dpi(meta: float | None, estimated: float) -> float:
    """元数据 DPI 与按纸张尺寸估算的值相差超过 20% 时使用估算值，否则用元数据值。"""
    if meta is None or estimated <= 0:
        return meta or estimated
    if abs(meta / estimated - 1) > 0.2:
        return estimated
    return meta


def source_name(path: Path) -> str:
    stem = path.stem
    return stem.rsplit("_", 1)[0] if "_" in stem else stem


def pair_images(paths: list[Path], faces: int) -> list[list[Path]]:
    """配对：所有文件名都带 _N 后缀时按前缀分组（组内按 N 排序），否则按排序顺序每 faces 张一组。"""
    if paths and all(_SUFFIX.search(p.stem) for p in paths):
        groups: dict[str, list[Path]] = {}
        for p in paths:
            groups.setdefault(source_name(p), []).append(p)
        return [sorted(g, key=lambda p: int(_SUFFIX.search(p.stem).group(1))) for g in groups.values()]
    return [paths[i:i + faces] for i in range(0, len(paths), faces)]


class FolderSource:
    def __init__(self, input_dir: str | Path, faces: int):
        self.input_dir = Path(input_dir)
        self.faces = faces

    def sheets(self) -> Iterator[SheetImages]:
        for group in pair_images(list_images(self.input_dir), self.faces):
            yield SheetImages(source=source_name(group[0]), names=[p.name for p in group],
                              dpis=[read_dpi(p) for p in group], paths=list(group))
```

`src/scan_answer/batch.py`（整文件替换）:
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

from .engine.omr import fit_thresholds
from .engine.recognize import (PhysicalError, SheetAnalysis, TemplateContext, analyze_sheet, decide_sheet,
                               estimate_dpi)
from .engine.result import build_result, render_images
from .output.local import LocalExporter
from .source.folder import choose_dpi, list_images, pair_images, read_dpi, source_name  # noqa: F401  兼容旧导入
from .template import load_template


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
        bad = [p.name for p, im in zip(group, imgs) if im is None]
        if bad:
            analyses.append(PhysicalError("ALIGN_FAILED", f"无法读取图片: {bad}"))
            continue
        dpis = [choose_dpi(read_dpi(p), estimate_dpi(img, tpl.paper)) for p, img in zip(group, imgs)]
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
            exporter.write_error(seq, a.code, a.msg, [im for im in imgs if im is not None])
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

Run: `.venv/bin/python -m pytest tests/test_folder_source.py tests/test_batch.py -q`
Expected: `14 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/source src/scan_answer/batch.py tests/test_folder_source.py
git commit -m "refactor(source): 图像源接口与 FolderSource，配对与 DPI 逻辑迁出 batch.py"
```

---

### Task 4: 识别进程中的工作函数

**Files:**
- Modify: `src/scan_answer/engine/result.py`（整文件替换：`build_result` 增加 `ocr: bool = False` 参数）
- Create: `src/scan_answer/pipeline/__init__.py`（空文件）、`src/scan_answer/pipeline/worker.py`
- Test: `tests/test_worker.py`

**Interfaces:**
- Consumes: Task 1 `read_json / update_json`；Task 2 `analysis_*`；Task 3 `choose_dpi`；计划 1 的 `analyze_sheet / decide_sheet / render_images / build_result / LocalExporter / write_jpg / write_json_atomic / load_template`
- Produces:
  - `build_result(..., ocr=False)`：`ocr=True` 时主观题与信息栏 `ocr_status` 为 `"pending"`，否则 `"none"`
  - `worker.get_context(template_dir: str) -> TemplateContext`（每进程缓存）
  - `SheetJob(seq, raw_paths: list[str], dpis, mark, uncertain, batch_dir: str, template_dir: str, batch_id, exam={}, scanner="folder", ocr=False)`
  - `SheetOutcome(seq, ok, physical=None, message="", result=None, exam_no=None, warnings=0, fills=[])`（`result` 相对批次目录）
  - `state_path(batch_dir, seq) -> Path`（`state/{seq:04d}.json`，内容 `{"analysis", "raw_paths", "result"}`）
  - `clear_outputs(batch_dir, seq)`：删 `students/` 与 `errors/` 下该序号的目录（精确匹配 `{seq:04d}` 或 `{seq:04d}_*`）及 state 文件
  - `process_sheet(job) -> SheetOutcome`：读原图（读不出 → `ALIGN_FAILED` “无法读取图片”）、`choose_dpi`、识别、写 `students/` 或 `errors/`、写 state
  - `FinalizeJob(seq, mark, uncertain, batch_dir, template_dir)`、`FinalizeOutcome(seq, changed, exam_no=None, warnings=0)`
  - `finalize_sheet(job) -> FinalizeOutcome`：按新阈值判定；客观题（题号/答案/状态）、考号（值/状态）、缺考值都不变则不改写；否则删旧 `obj_*.jpg`、重新裁图，并用 `update_json` 重建 `result.json`，保留 `scan.time` 与已有转写结果（`text / ocr_status / ocr_model / ocr_confidence`、信息栏 `text / ocr_status`、`OCR_FAILED` 警告）

- [ ] **Step 1: 写失败测试**

`tests/test_worker.py`:
```python
import numpy as np

from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json
from scan_answer.output.local import write_jpg
from scan_answer.pipeline.worker import (FinalizeJob, SheetJob, clear_outputs, finalize_sheet, process_sheet,
                                         state_path)
from scan_answer.synth.generate import make_scans, make_sheet

from conftest import TEMPLATE_DIR


def _raw(ctx, tmp_path, seed, special=None, seq=1):
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    scans = make_scans(ctx.tpl, paper, truth, rng, 300, special)
    paths = []
    for i, img in enumerate(scans, start=1):
        p = tmp_path / "raw" / f"{seq:04d}_s_{i}.jpg"
        write_jpg(p, img, quality=95)
        paths.append(str(p))
    return paths, truth


def _job(tmp_path, seq, paths, th=Thresholds(), ocr=False):
    return SheetJob(seq=seq, raw_paths=paths, dpis=[300.0] * len(paths), mark=th.mark, uncertain=th.uncertain,
                    batch_dir=str(tmp_path), template_dir=str(TEMPLATE_DIR), batch_id="b", exam={"name": "考试"},
                    scanner="folder", ocr=ocr)


def test_process_sheet_writes_result_and_state(ctx, tmp_path):
    paths, truth = _raw(ctx, tmp_path, 101)
    out = process_sheet(_job(tmp_path, 1, paths, ocr=True))
    assert out.ok and out.seq == 1 and out.exam_no == truth["exam_no"]["value"]
    assert len(out.fills) == 55 * 5 + 15 * 10
    r = read_json(tmp_path / out.result)
    assert r["exam"] == {"name": "考试"} and r["pages"]["raw_1"] == "../../raw/0001_s_1.jpg"
    assert all(q["ocr_status"] == "pending" for q in r["subjective"])
    st = read_json(state_path(tmp_path, 1))
    assert st["result"] == out.result and st["raw_paths"] == paths


def test_process_sheet_physical_and_unreadable(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 102, "blank", seq=2)
    out = process_sheet(_job(tmp_path, 2, paths))
    assert not out.ok and out.physical == "BLANK_PAGE"
    assert (tmp_path / "errors" / "0002_BLANK_PAGE" / "error.json").exists()
    bad = tmp_path / "raw" / "0003_x_1.jpg"
    bad.write_bytes(b"not an image")
    out = process_sheet(_job(tmp_path, 3, [str(bad), paths[0]]))
    assert out.physical == "ALIGN_FAILED" and "无法读取图片" in out.message


def test_finalize_sheet_only_rewrites_on_change(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 103)
    out = process_sheet(_job(tmp_path, 1, paths, th=Thresholds(0.97, 0.5)))
    same = finalize_sheet(FinalizeJob(1, 0.97, 0.5, str(tmp_path), str(TEMPLATE_DIR)))
    assert not same.changed
    fixed = finalize_sheet(FinalizeJob(1, 0.45, 0.2025, str(tmp_path), str(TEMPLATE_DIR)))
    assert fixed.changed
    r = read_json(tmp_path / out.result)
    assert sum(o["status"] == "uncertain" for o in r["objective"]) < 5


def test_clear_outputs(ctx, tmp_path):
    paths, _ = _raw(ctx, tmp_path, 104, seq=12)
    out = process_sheet(_job(tmp_path, 12, paths))
    (tmp_path / "students" / "0120_x").mkdir(parents=True)  # 序号 120 不应被误删
    clear_outputs(tmp_path, 12)
    assert not (tmp_path / out.result).exists() and not state_path(tmp_path, 12).exists()
    assert (tmp_path / "students" / "0120_x").exists()
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_worker.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.pipeline'`）

- [ ] **Step 3: 实现**

`src/scan_answer/engine/result.py`（整文件替换）:
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
                 scan_time: datetime | None = None, ocr: bool = False) -> dict:
    """ocr=True 表示已配置手写转写：主观题与信息栏的 ocr_status 置为 pending，等待后台转写。"""
    t = ctx.tpl
    ocr_status = "pending" if ocr else "none"
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
        student["fields"][fd.key] = {"label": fd.label, "image": f"images/{fd.key}.jpg", "text": None, "ocr_status": ocr_status}
    objective = []
    for o in d.objective:
        item = dict(o)
        if f"images/obj_{o['no']}.jpg" in image_paths:
            item["image"] = f"images/obj_{o['no']}.jpg"
        objective.append(item)
    subjective = [{"no": sq.no, "type": sq.qtype, "content": sq.content,
                   "images": [f"images/q{sq.no}_{i}.jpg" for i in range(1, len(sq.regions) + 1)],
                   "text": None, "ocr_status": ocr_status, "ocr_model": None, "ocr_confidence": None}
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

`src/scan_answer/pipeline/worker.py`:
```python
"""在识别进程里执行的工作：识别一张卡并写出；结束批次时按最终阈值重新判定。

函数只接收可序列化的 job，模板在每个进程里加载一次并缓存。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cv2

from ..engine.omr import Thresholds
from ..engine.recognize import PhysicalError, TemplateContext, analyze_sheet, decide_sheet, estimate_dpi
from ..engine.result import build_result, render_images
from ..engine.state import analysis_fills, analysis_from_dict, analysis_to_dict
from ..output.jsonfile import read_json, update_json
from ..output.local import LocalExporter, write_jpg, write_json_atomic
from ..source.folder import choose_dpi
from ..template import load_template

_CTX: dict[str, TemplateContext] = {}


def get_context(template_dir: str) -> TemplateContext:
    if template_dir not in _CTX:
        tpl, bases = load_template(template_dir)
        _CTX[template_dir] = TemplateContext(tpl, bases)
    return _CTX[template_dir]


@dataclass
class SheetJob:
    seq: int
    raw_paths: list[str]  # 批次 raw/ 下原图的绝对路径
    dpis: list[float | None]  # 元数据 DPI
    mark: float
    uncertain: float
    batch_dir: str
    template_dir: str
    batch_id: str
    exam: dict = field(default_factory=dict)
    scanner: str = "folder"
    ocr: bool = False


@dataclass
class SheetOutcome:
    seq: int
    ok: bool
    physical: str | None = None
    message: str = ""
    result: str | None = None  # 相对批次目录
    exam_no: str | None = None
    warnings: int = 0
    fills: list[float] = field(default_factory=list)


def state_path(batch_dir: str | Path, seq: int) -> Path:
    return Path(batch_dir) / "state" / f"{seq:04d}.json"


def clear_outputs(batch_dir: str | Path, seq: int) -> None:
    """删除某个序号之前的识别输出（重扫前调用）；原图不在这里删。"""
    b = Path(batch_dir)
    for parent in (b / "students", b / "errors"):
        if parent.exists():
            for d in parent.glob(f"{seq:04d}*"):
                if d.name == f"{seq:04d}" or d.name.startswith(f"{seq:04d}_"):
                    shutil.rmtree(d, ignore_errors=True)
    state_path(b, seq).unlink(missing_ok=True)


def process_sheet(job: SheetJob) -> SheetOutcome:
    ctx = get_context(job.template_dir)
    exporter = LocalExporter(job.batch_dir)
    imgs = [cv2.imread(p, cv2.IMREAD_GRAYSCALE) for p in job.raw_paths]
    bad = [Path(p).name for p, im in zip(job.raw_paths, imgs) if im is None]
    try:
        if bad:
            raise PhysicalError("ALIGN_FAILED", f"无法读取图片: {bad}")
        dpis = [choose_dpi(m, estimate_dpi(im, ctx.tpl.paper)) for m, im in zip(job.dpis, imgs)]
        a = analyze_sheet(ctx, imgs, dpis)
    except PhysicalError as e:
        exporter.write_error(job.seq, e.code, e.msg, [im for im in imgs if im is not None])
        return SheetOutcome(job.seq, False, physical=e.code, message=e.msg)
    th = Thresholds(job.mark, job.uncertain)
    d = decide_sheet(ctx, a, th)
    images = render_images(ctx, a, d, imgs)
    raw_rel = [f"../../raw/{Path(p).name}" for p in job.raw_paths]
    result = build_result(ctx, a, d, seq=job.seq, batch_id=job.batch_id, exam=job.exam, scanner=job.scanner,
                          raw_paths=raw_rel, image_paths=set(images), ocr=job.ocr)
    exam_value = d.exam_no["value"] if d.exam_no else None
    path = exporter.write_student(job.seq, exam_value, result, images)
    write_json_atomic(state_path(job.batch_dir, job.seq),
                      {"analysis": analysis_to_dict(a), "raw_paths": job.raw_paths,
                       "result": str(path.relative_to(job.batch_dir))})
    return SheetOutcome(job.seq, True, result=str(path.relative_to(job.batch_dir)), exam_no=exam_value,
                        warnings=len(d.warnings), fills=analysis_fills(a).tolist())


@dataclass
class FinalizeJob:
    seq: int
    mark: float
    uncertain: float
    batch_dir: str
    template_dir: str


@dataclass
class FinalizeOutcome:
    seq: int
    changed: bool
    exam_no: str | None = None
    warnings: int = 0


def _decision_key(result: dict) -> tuple:
    """判定结果中会受阈值影响的部分。"""
    exam = result["student"].get("exam_no") or {}
    absent = result["student"].get("absent") or {}
    return (tuple((o["no"], o["answer"], o["status"]) for o in result["objective"]),
            exam.get("value"), exam.get("status"), absent.get("value"))


def _carry_ocr(new: dict, old: dict) -> dict:
    """重新判定时保留已有的转写结果与 OCR 警告。"""
    old_sub = {q["no"]: q for q in old.get("subjective", [])}
    for q in new["subjective"]:
        o = old_sub.get(q["no"])
        if o:
            for k in ("text", "ocr_status", "ocr_model", "ocr_confidence"):
                q[k] = o.get(k)
    old_fields = old["student"].get("fields", {})
    for key, f in new["student"]["fields"].items():
        if key in old_fields:
            f["text"] = old_fields[key].get("text")
            f["ocr_status"] = old_fields[key].get("ocr_status")
    new["warnings"] += [w for w in old.get("warnings", []) if w.get("code") == "OCR_FAILED"]
    return new


def finalize_sheet(job: FinalizeJob) -> FinalizeOutcome:
    """按最终阈值重新判定；结果有变化才重新裁图并改写 result.json（保留转写结果）。"""
    ctx = get_context(job.template_dir)
    state = read_json(state_path(job.batch_dir, job.seq))
    a = analysis_from_dict(state["analysis"])
    d = decide_sheet(ctx, a, Thresholds(job.mark, job.uncertain))
    result_path = Path(job.batch_dir) / state["result"]
    old = read_json(result_path)
    probe = {"objective": d.objective, "student": {"exam_no": d.exam_no, "absent": d.absent}}
    exam_value = d.exam_no["value"] if d.exam_no else None
    if _decision_key(probe) == _decision_key(old):
        return FinalizeOutcome(job.seq, False, exam_value, len(old["warnings"]))
    imgs = [cv2.imread(p, cv2.IMREAD_GRAYSCALE) for p in state["raw_paths"]]
    images = render_images(ctx, a, d, imgs)
    for stale in (result_path.parent / "images").glob("obj_*.jpg"):
        stale.unlink()
    for rel, img in images.items():
        write_jpg(result_path.parent / rel, img)

    def rebuild(cur: dict) -> dict:
        raw = [cur["pages"][k] for k in sorted(cur["pages"]) if k.startswith("raw_")]
        new = build_result(ctx, a, d, seq=cur["seq"], batch_id=cur["batch_id"], exam=cur["exam"],
                           scanner=cur["scan"]["scanner"], raw_paths=raw, image_paths=set(images),
                           scan_time=datetime.fromisoformat(cur["scan"]["time"]))
        return _carry_ocr(new, cur)

    new = update_json(result_path, rebuild)
    return FinalizeOutcome(job.seq, True, exam_value, len(new["warnings"]))
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_worker.py tests/test_result.py -q`
Expected: `6 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/engine/result.py src/scan_answer/pipeline tests/test_worker.py
git commit -m "feat(pipeline): 识别进程工作函数——单张识别写出、按最终阈值重新判定"
```

---

### Task 5: 批次会话与流水线调度

**Files:**
- Create: `src/scan_answer/pipeline/events.py`、`src/scan_answer/pipeline/session.py`、`src/scan_answer/pipeline/runner.py`
- Test: `tests/test_pipeline.py`

**Interfaces:**
- Consumes: Task 3 `SheetImages / DeviceEvent / ImageSource / FolderSource`；Task 4 worker 全部接口；计划 1 `Thresholds / fit_thresholds`
- Produces:
  - 事件：`SheetEvent(seq, source, ok, physical, message, exam_no, warnings, rescan=False)`、`ThresholdEvent(mark, uncertain, sheets)`、`DeviceNotice(kind, message, after_seq)`、`VoidEvent(seq)`、`FinalizedEvent(mark, uncertain, changed)`、`OcrProgress(done, failed, total)`
  - `session.REFIT_MIN = 100`、`REFIT_EVERY = 50`、`MANIFEST_INTERVAL = 2.0`
  - `BatchSession.create(out_root, template_dir, *, batch_id=None, exam=None, scanner="folder", ocr=False)`（目录已存在且非空抛 `FileExistsError`）、`BatchSession.open(batch_dir, template_dir)`；属性 `batch_dir`、`template_dir`、`manifest`、`sheets: dict[int, dict]`、`fills: dict[int, ndarray]`、`thresholds`、`batch_id`、`faces`；方法 `next_seq()`、`save_raw(seq, sheet) -> list[Path]`、`remove_raw(seq)`、`prepare_rescan(seq)`、`register(seq, sheet)`、`record(outcome) -> Thresholds | None`、`void(seq)`（只能作废 `physical` 状态，否则 `ValueError`）、`add_event(kind, message)`、`final_thresholds()`、`mark_finalized(th)`、`counts()`、`save(force=False)`
  - `BatchPipeline(session, *, workers=0, listener=None, on_result=None)`（上下文管理器）：`run(source, stop=None)`、`rescan(seq, sheet) -> SheetEvent`、`void(seq)`、`finalize() -> FinalizedEvent`、`close()`；`on_result(result_json_abs_path)` 在每张成功卡写出后回调

- [ ] **Step 1: 写失败测试**

`tests/test_pipeline.py`:
```python
import json
import threading

import numpy as np
import pytest

from scan_answer.engine.omr import Thresholds
from scan_answer.output.jsonfile import read_json, update_json
from scan_answer.pipeline import session as session_mod
from scan_answer.pipeline.events import DeviceNotice, FinalizedEvent, SheetEvent, ThresholdEvent, VoidEvent
from scan_answer.pipeline.runner import BatchPipeline
from scan_answer.pipeline.session import BatchSession
from scan_answer.source.base import DeviceEvent, SheetImages
from scan_answer.source.folder import FolderSource
from scan_answer.synth.generate import generate_dataset, make_scans, make_sheet

from conftest import TEMPLATE_DIR


class ListSource:
    def __init__(self, items):
        self.items = items

    def sheets(self):
        yield from self.items


def _sheet(ctx, seed, special=None, source="s"):
    rng = np.random.default_rng(seed)
    paper, truth = make_sheet(ctx.tpl, ctx.bases, rng)
    scans = make_scans(ctx.tpl, paper, truth, rng, 300, special)
    return SheetImages(source=source, names=[f"{source}_{i + 1}.jpg" for i in range(len(scans))],
                       dpis=[300.0] * len(scans), images=scans), truth


def test_run_inline_records_events_and_manifest(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 5, seed=31)
    events = []
    s = BatchSession.create(tmp_path / "out", TEMPLATE_DIR, batch_id="b", exam={"name": "测试"})
    with BatchPipeline(s, listener=events.append) as p:
        p.run(FolderSource(tmp_path / "ds", s.faces))
    sheet_events = [e for e in events if isinstance(e, SheetEvent)]
    assert [e.seq for e in sheet_events] == [1, 2, 3, 4, 5]
    m = read_json(s.batch_dir / "manifest.json")
    assert m["counts"]["total"] == 5 and m["counts"]["ok"] + m["counts"]["physical"] == 5
    assert m["finalized"] is False and m["exam"] == {"name": "测试"}
    ok = [x for x in m["sheets"] if x["status"] == "ok"]
    assert ok and all((s.batch_dir / x["result"]).exists() for x in ok)
    assert all((s.batch_dir / "state" / f"{x['seq']:04d}.json").exists() for x in ok)
    assert len(list((s.batch_dir / "raw").iterdir())) == 10


def test_process_pool_matches_inline(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 4, seed=32)
    answers = {}
    for workers in (0, 2):
        s = BatchSession.create(tmp_path / f"out{workers}", TEMPLATE_DIR, batch_id="b")
        with BatchPipeline(s, workers=workers) as p:
            p.run(FolderSource(tmp_path / "ds", s.faces))
        m = read_json(s.batch_dir / "manifest.json")
        answers[workers] = {x["seq"]: [(o["no"], o["answer"], o["status"]) for o in
                                       read_json(s.batch_dir / x["result"])["objective"]]
                            for x in m["sheets"] if x["status"] == "ok"}
    assert answers[0] == answers[2] and answers[0]


def test_refit_threshold_event(ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(session_mod, "REFIT_MIN", 2)
    monkeypatch.setattr(session_mod, "REFIT_EVERY", 2)
    events = []
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s, listener=events.append) as p:
        p.run(ListSource([_sheet(ctx, 40 + i, source=f"s{i}")[0] for i in range(4)]))
    th = [e for e in events if isinstance(e, ThresholdEvent)]
    assert [e.sheets for e in th] == [2, 4]
    assert 0.3 <= th[-1].mark <= 0.6


def test_device_event_and_stop(ctx, tmp_path):
    events = []
    stop = threading.Event()
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")

    def gen():
        yield _sheet(ctx, 50, source="a")[0]
        yield DeviceEvent("double_feed", "检测到双张")
        stop.set()
        yield _sheet(ctx, 51, source="b")[0]

    class GenSource:
        def sheets(self):
            return gen()

    with BatchPipeline(s, listener=events.append) as p:
        p.run(GenSource(), stop=stop)
    notices = [e for e in events if isinstance(e, DeviceNotice)]
    assert notices == [DeviceNotice("double_feed", "检测到双张", 1)]
    m = read_json(s.batch_dir / "manifest.json")
    assert [x["seq"] for x in m["sheets"]] == [1]  # stop 之后的卡没有处理
    assert m["events"][0]["kind"] == "double_feed" and m["events"][0]["after_seq"] == 1


def test_rescan_replaces_physical_error_and_void(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    bad, _ = _sheet(ctx, 60, "blank", source="x")
    bad2, _ = _sheet(ctx, 61, "blank", source="y")
    with BatchPipeline(s) as p:
        p.run(ListSource([bad, bad2]))
        assert s.sheets[1]["status"] == "physical" and (s.batch_dir / "errors" / "0001_BLANK_PAGE").exists()
        good, truth = _sheet(ctx, 62, source="x-rescan")
        ev = p.rescan(1, good)
        assert ev.seq == 1 and ev.ok and ev.rescan
        assert not (s.batch_dir / "errors" / "0001_BLANK_PAGE").exists()
        assert sorted(x.name for x in (s.batch_dir / "raw").glob("0001_*")) == ["0001_x-rescan_1.jpg", "0001_x-rescan_2.jpg"]
        r = read_json(s.batch_dir / s.sheets[1]["result"])
        assert r["seq"] == 1 and r["student"]["exam_no"]["value"] == truth["exam_no"]["value"]
        events = []
        p.listener = events.append
        p.void(2)
        assert events == [VoidEvent(2)]
        with pytest.raises(ValueError):
            p.void(1)
    m = read_json(s.batch_dir / "manifest.json")
    assert m["counts"] == {"total": 2, "ok": 1, "physical": 0, "voided": 1, "warnings": m["counts"]["warnings"]}


def test_finalize_redecides_and_keeps_ocr_text(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    s.thresholds = Thresholds(mark=0.97, uncertain=0.5)  # 扫描时阈值极端偏高 → 大量题目 uncertain
    events = []
    with BatchPipeline(s, listener=events.append) as p:
        p.run(ListSource([_sheet(ctx, 70 + i, source=f"s{i}")[0] for i in range(3)]))
        path = s.batch_dir / s.sheets[1]["result"]
        before = read_json(path)
        assert sum(o["status"] == "uncertain" for o in before["objective"]) > 20
        update_json(path, lambda r: r["subjective"][0].update(text="已转写", ocr_status="done"))
        fe = p.finalize()
    assert isinstance(fe, FinalizedEvent) and fe.changed == 3 and events[-1] == fe
    after = read_json(path)
    assert sum(o["status"] == "uncertain" for o in after["objective"]) < 5
    assert after["subjective"][0]["text"] == "已转写" and after["subjective"][0]["ocr_status"] == "done"
    assert after["scan"]["time"] == before["scan"]["time"]
    bad_now = {o["no"] for o in after["objective"] if o["status"] != "ok"}
    imgs = {p.stem.split("_")[1] for p in (path.parent / "images").glob("obj_*.jpg")}
    assert imgs == bad_now
    m = read_json(s.batch_dir / "manifest.json")
    assert m["finalized"] is True and abs(m["thresholds"]["mark"] - fe.mark) < 1e-3
    # 再次结束批次：阈值不变 → 不再改写
    with BatchPipeline(BatchSession.open(s.batch_dir, TEMPLATE_DIR)) as p2:
        assert p2.finalize().changed == 0


def test_open_continues_numbering(ctx, tmp_path):
    s = BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
    with BatchPipeline(s) as p:
        p.run(ListSource([_sheet(ctx, 80, source="a")[0]]))
    s2 = BatchSession.open(s.batch_dir, TEMPLATE_DIR)
    assert s2.next_seq() == 2 and len(s2.fills) == 1
    with BatchPipeline(s2) as p:
        p.run(ListSource([_sheet(ctx, 81, source="b")[0]]))
    assert [x["seq"] for x in read_json(s.batch_dir / "manifest.json")["sheets"]] == [1, 2]


def test_create_refuses_non_empty_dir(tmp_path):
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "x").write_text("1")
    with pytest.raises(FileExistsError):
        BatchSession.create(tmp_path, TEMPLATE_DIR, batch_id="b")
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.pipeline.events'`）

- [ ] **Step 3: 实现**

`src/scan_answer/pipeline/events.py`:
```python
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
```

`src/scan_answer/pipeline/session.py`:
```python
"""批次会话：批次目录、序号分配、原图保存、阈值、manifest。只在主进程中使用。"""
from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image

from ..engine.omr import Thresholds, fit_thresholds
from ..engine.state import analysis_fills, analysis_from_dict
from ..output.jsonfile import read_json
from ..output.local import write_json_atomic
from ..source.base import SheetImages
from .worker import SheetOutcome, clear_outputs, state_path

REFIT_MIN = 100  # 成功识别满这么多张后改用拟合阈值
REFIT_EVERY = 50  # 之后每多这么多张重新拟合一次
MANIFEST_INTERVAL = 2.0  # manifest 最短写盘间隔（秒）


class BatchSession:
    def __init__(self, batch_dir: Path, template_dir: Path, manifest: dict):
        self.batch_dir = Path(batch_dir)
        self.template_dir = Path(template_dir)
        self.manifest = manifest
        self.sheets: dict[int, dict] = {s["seq"]: s for s in manifest.get("sheets", [])}
        self.fills: dict[int, np.ndarray] = {}
        self.thresholds = Thresholds(**manifest["thresholds"]) if manifest.get("thresholds") else Thresholds()
        self._last_fit_count = 0
        self._last_save = 0.0

    # ---------- 创建 / 打开 ----------
    @classmethod
    def create(cls, out_root: str | Path, template_dir: str | Path, *, batch_id: str | None = None,
               exam: dict | None = None, scanner: str = "folder", ocr: bool = False) -> "BatchSession":
        template_dir = Path(template_dir)
        batch_id = batch_id or datetime.now().strftime("batch_%Y%m%d_%H%M%S")
        batch_dir = Path(out_root) / batch_id
        if batch_dir.exists() and any(batch_dir.iterdir()):
            raise FileExistsError(f"批次目录已存在且非空: {batch_dir}")
        batch_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(template_dir / "template.json", batch_dir / "template.json")
        tpl = read_json(template_dir / "template.json")
        manifest = {
            "schema_version": "1.0", "batch_id": batch_id, "exam": exam or {},
            "template": {"name": tpl["name"], "version": tpl["version"], "snapshot": "template.json"},
            "scanner": scanner, "ocr": ocr, "created": datetime.now().astimezone().isoformat(timespec="seconds"),
            "thresholds": None, "finalized": False, "counts": {}, "events": [], "sheets": [],
        }
        s = cls(batch_dir, template_dir, manifest)
        s.save(force=True)
        return s

    @classmethod
    def open(cls, batch_dir: str | Path, template_dir: str | Path) -> "BatchSession":
        """打开已有批次（程序重启后继续扫描）；涂黑度从 state/ 恢复。"""
        s = cls(Path(batch_dir), Path(template_dir), read_json(Path(batch_dir) / "manifest.json"))
        for seq, item in s.sheets.items():
            sp = state_path(s.batch_dir, seq)
            if item.get("result") and sp.exists():
                s.fills[seq] = analysis_fills(analysis_from_dict(read_json(sp)["analysis"]))
        return s

    # ---------- 属性 ----------
    @property
    def batch_id(self) -> str:
        return self.manifest["batch_id"]

    @property
    def faces(self) -> int:
        return len(read_json(self.batch_dir / "template.json")["faces"])

    def next_seq(self) -> int:
        return max(self.sheets, default=0) + 1

    # ---------- 原图 ----------
    def save_raw(self, seq: int, sheet: SheetImages) -> list[Path]:
        """把一张卡的各面原图存到 raw/，文件名 {seq:04d}_{原文件名}。"""
        raw = self.batch_dir / "raw"
        raw.mkdir(parents=True, exist_ok=True)
        out = []
        for i, name in enumerate(sheet.names):
            dst = raw / f"{seq:04d}_{name}"
            if sheet.paths:
                shutil.copy(sheet.paths[i], dst)
            else:
                dpi = sheet.dpis[i] or 300
                Image.fromarray(sheet.images[i]).save(dst, quality=95, dpi=(dpi, dpi))
            out.append(dst)
        return out

    def remove_raw(self, seq: int) -> None:
        for p in (self.batch_dir / "raw").glob(f"{seq:04d}_*"):
            p.unlink()

    def prepare_rescan(self, seq: int) -> None:
        if seq not in self.sheets:
            raise KeyError(f"序号不存在: {seq}")
        clear_outputs(self.batch_dir, seq)
        self.remove_raw(seq)
        self.fills.pop(seq, None)

    # ---------- 记录结果 ----------
    def register(self, seq: int, sheet: SheetImages) -> None:
        self.sheets[seq] = {"seq": seq, "source": sheet.source, "files": list(sheet.names), "status": "processing"}

    def record(self, out: SheetOutcome) -> Thresholds | None:
        """记录一张卡的结果；若因此重新拟合了阈值，返回新阈值。"""
        item = self.sheets[out.seq]
        for k in ("result", "exam_no", "warnings", "physical", "message", "voided"):
            item.pop(k, None)
        if out.ok:
            item.update({"status": "ok", "result": out.result, "exam_no": out.exam_no, "warnings": out.warnings})
            self.fills[out.seq] = np.asarray(out.fills, dtype=np.float32)
        else:
            item.update({"status": "physical", "physical": out.physical, "message": out.message})
            self.fills.pop(out.seq, None)
        refit = None
        n = len(self.fills)
        if not self.manifest["finalized"] and n >= REFIT_MIN and n - self._last_fit_count >= (REFIT_EVERY if self._last_fit_count else 0):
            self.thresholds = fit_thresholds(np.concatenate(list(self.fills.values())))
            self._last_fit_count = n
            refit = self.thresholds
        self.save()
        return refit

    def void(self, seq: int) -> None:
        item = self.sheets[seq]
        if item.get("status") != "physical":
            raise ValueError(f"只能作废物理异常的卡: {seq}")
        item["status"] = "voided"
        item["voided"] = True
        self.save(force=True)

    def add_event(self, kind: str, message: str) -> None:
        self.manifest["events"].append({"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                                        "kind": kind, "message": message, "after_seq": self.next_seq() - 1})
        self.save(force=True)

    # ---------- 结束批次 ----------
    def final_thresholds(self) -> Thresholds:
        if not self.fills:
            return Thresholds()
        return fit_thresholds(np.concatenate(list(self.fills.values())))

    def mark_finalized(self, th: Thresholds) -> None:
        self.thresholds = th
        self.manifest["finalized"] = True
        self.save(force=True)

    # ---------- manifest ----------
    def counts(self) -> dict:
        items = list(self.sheets.values())
        return {"total": len(items),
                "ok": sum(1 for s in items if s.get("status") == "ok"),
                "physical": sum(1 for s in items if s.get("status") == "physical"),
                "voided": sum(1 for s in items if s.get("status") == "voided"),
                "warnings": sum(s.get("warnings", 0) for s in items if s.get("status") == "ok")}

    def save(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_save < MANIFEST_INTERVAL:
            return
        self.manifest["sheets"] = [self.sheets[k] for k in sorted(self.sheets)]
        self.manifest["counts"] = self.counts()
        self.manifest["thresholds"] = {"mark": round(self.thresholds.mark, 4),
                                       "uncertain": round(self.thresholds.uncertain, 4)}
        write_json_atomic(self.batch_dir / "manifest.json", self.manifest)
        self._last_save = now
```

`src/scan_answer/pipeline/runner.py`:
```python
"""流水线：图像源 → 保存原图 → 识别进程池 → 记录结果 → 发事件；结束批次时按最终阈值重新判定。

扫描与识别并行：主线程从图像源取卡、提交给进程池，同时收集已完成的结果；
在途任务数不超过 workers*2，防止扫描远快于识别时内存堆积。workers=0 时在当前进程内顺序执行（测试和调试用）。
所有事件都在调用 run()/rescan()/finalize() 的线程里发出。
"""
from __future__ import annotations

import multiprocessing
import threading
from concurrent.futures import FIRST_COMPLETED, Executor, Future, ProcessPoolExecutor, wait
from typing import Callable

from ..source.base import DeviceEvent, ImageSource, SheetImages
from .events import DeviceNotice, FinalizedEvent, SheetEvent, ThresholdEvent, VoidEvent
from .session import BatchSession
from .worker import FinalizeJob, SheetJob, SheetOutcome, finalize_sheet, process_sheet

Listener = Callable[[object], None]


class _InlineExecutor(Executor):
    """在当前线程立即执行的执行器，接口与进程池一致。"""

    def submit(self, fn, /, *args, **kwargs) -> Future:
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except BaseException as e:  # noqa: BLE001 — 交给调用方的 result() 抛出
            f.set_exception(e)
        return f


class BatchPipeline:
    def __init__(self, session: BatchSession, *, workers: int = 0, listener: Listener | None = None,
                 on_result: Callable[[str], None] | None = None):
        """on_result：每张卡成功写出 result.json 后回调其绝对路径（用于投递手写转写）。"""
        self.session = session
        self.workers = workers
        self.listener = listener or (lambda e: None)
        self.on_result = on_result
        self._executor: Executor = (ProcessPoolExecutor(workers, mp_context=multiprocessing.get_context("spawn"))
                                    if workers > 0 else _InlineExecutor())

    # ---------- 生命周期 ----------
    def close(self) -> None:
        self._executor.shutdown(wait=True)

    def __enter__(self) -> "BatchPipeline":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---------- 内部 ----------
    def _job(self, seq: int, sheet: SheetImages) -> SheetJob:
        s = self.session
        raw = s.save_raw(seq, sheet)
        m = s.manifest
        return SheetJob(seq=seq, raw_paths=[str(p) for p in raw], dpis=list(sheet.dpis),
                        mark=s.thresholds.mark, uncertain=s.thresholds.uncertain,
                        batch_dir=str(s.batch_dir), template_dir=str(s.template_dir), batch_id=s.batch_id,
                        exam=m["exam"], scanner=m["scanner"], ocr=m["ocr"])

    def _collect(self, out: SheetOutcome, rescan: bool = False) -> SheetEvent:
        refit = self.session.record(out)
        item = self.session.sheets[out.seq]
        ev = SheetEvent(out.seq, item["source"], out.ok, out.physical, out.message, out.exam_no,
                        out.warnings, rescan)
        self.listener(ev)
        if out.ok and self.on_result:
            self.on_result(str(self.session.batch_dir / out.result))
        if refit:
            self.listener(ThresholdEvent(refit.mark, refit.uncertain, len(self.session.fills)))
        return ev

    # ---------- 对外 ----------
    def run(self, source: ImageSource, stop: threading.Event | None = None) -> None:
        """处理图像源中的全部卡，直到取完或 stop 被置位。返回前等待所有在途任务完成。"""
        pending: dict[Future, int] = {}
        window = max(1, self.workers * 2)
        for item in source.sheets():
            if stop is not None and stop.is_set():
                break
            if isinstance(item, DeviceEvent):
                self.session.add_event(item.kind, item.message)
                self.listener(DeviceNotice(item.kind, item.message, self.session.next_seq() - 1))
                continue
            seq = self.session.next_seq()
            self.session.register(seq, item)
            pending[self._executor.submit(process_sheet, self._job(seq, item))] = seq
            if len(pending) >= window:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for f in done:
                    pending.pop(f)
                    self._collect(f.result())
        for f in list(pending):
            self._collect(f.result())
        self.session.save(force=True)

    def rescan(self, seq: int, sheet: SheetImages) -> SheetEvent:
        """用新图像替换某个序号的卡（物理异常重扫）；序号不变。"""
        self.session.prepare_rescan(seq)
        self.session.register(seq, sheet)
        ev = self._collect(self._executor.submit(process_sheet, self._job(seq, sheet)).result(), rescan=True)
        self.session.save(force=True)
        return ev

    def void(self, seq: int) -> None:
        self.session.void(seq)
        self.listener(VoidEvent(seq))

    def finalize(self) -> FinalizedEvent:
        """结束批次：用整批涂黑度拟合最终阈值，逐张重新判定；有变化的卡重写 result.json。"""
        s = self.session
        th = s.final_thresholds()
        jobs = [FinalizeJob(seq, th.mark, th.uncertain, str(s.batch_dir), str(s.template_dir))
                for seq, item in sorted(s.sheets.items()) if item.get("status") == "ok"]
        changed = 0
        for f in [self._executor.submit(finalize_sheet, j) for j in jobs]:
            r = f.result()
            item = s.sheets[r.seq]
            item["warnings"] = r.warnings
            item["exam_no"] = r.exam_no
            changed += int(r.changed)
        s.mark_finalized(th)
        ev = FinalizedEvent(th.mark, th.uncertain, changed)
        self.listener(ev)
        return ev
```

实现要点：
- `run()` 在途任务数不超过 `max(1, workers*2)`，满了就等至少一个完成再取下一张，防止扫描快于识别时内存堆积；`stop` 置位后不再取新卡，但会等在途任务完成。
- 重新拟合阈值只发生在未结束的批次里，结束批次后阈值固定为最终值（重扫的卡直接按最终阈值判定）。
- `test_process_pool_matches_inline` 用真实进程池（`spawn`），首次启动每个进程要加载模板，约需几秒。

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_pipeline.py -q`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/pipeline/events.py src/scan_answer/pipeline/session.py src/scan_answer/pipeline/runner.py tests/test_pipeline.py
git commit -m "feat(pipeline): 批次会话与流水线调度——实时识别、阈值重拟合、重扫、作废、结束批次"
```

---

### Task 6: 手写转写客户端

**Files:**
- Create: `src/scan_answer/ocr/__init__.py`（空文件）、`src/scan_answer/ocr/config.py`、`src/scan_answer/ocr/prompts.py`、`src/scan_answer/ocr/client.py`
- Create: `tests/ocr_mock.py`（测试辅助，模拟 OpenAI 兼容接口）
- Test: `tests/test_ocr_client.py`

**Interfaces:**
- Produces:
  - `OcrConfig(base_url, api_key, model, concurrency=4, timeout_s=60.0, max_retries=2)`、`.validate()`；`load_ocr_config(path)`（忽略未知字段并校验）、`save_ocr_config(cfg, path)`
  - `prompts.SYSTEM`、`question_instruction(content)`（`zh` / `en` / `math`，未知按 `zh`）、`field_instruction(label)`（文本含“栏的图片”）
  - `OcrClient(cfg, sleep=time.sleep)`：`transcribe(jpegs: list[bytes], instruction) -> Transcription(text, confidence, model)`，失败抛 `OcrError`；HTTP 4xx（429 除外）不重试，其余错误按 0.5s、1s… 退避重试 `max_retries` 次；`ping() -> str`
  - `client._parse(content) -> (text, confidence|None)`：优先解析回复中的 JSON 对象，置信度不在 [0,1] 时为 None，解析不到时整段文本作为结果
  - `tests/ocr_mock.MockOcrServer(mode="ok", fail_times=1, delay=0.0, fail_marker=None)`：上下文管理器，`.url`、`.calls`、`.requests`

- [ ] **Step 1: 写失败测试**

`tests/ocr_mock.py`:
```python
"""测试用的 OpenAI 兼容接口模拟服务。"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockOcrServer:
    """mode: ok | fail_first（前 fail_times 次返回 500）| error（总是 500）| auth（401）| slow | plain（非 JSON 回复）
    fail_marker：请求的提示词里含该字符串时总是返回 500（用于让某一类题目失败）。"""

    def __init__(self, mode="ok", fail_times=1, delay=0.0, fail_marker=None):
        self.mode, self.fail_times, self.delay, self.fail_marker = mode, fail_times, delay, fail_marker
        self.calls = 0
        self.requests = []
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with outer._lock:
                    outer.calls += 1
                    n = outer.calls
                    outer.requests.append({"auth": self.headers.get("Authorization"), "body": body})
                user = body["messages"][-1]["content"]
                text = user if isinstance(user, str) else user[0]["text"]
                images = 0 if isinstance(user, str) else len(user) - 1
                if outer.mode == "slow":
                    time.sleep(outer.delay)
                if outer.mode == "auth":
                    return self._send(401, {"error": "bad key"})
                if outer.mode == "error" or (outer.mode == "fail_first" and n <= outer.fail_times) \
                        or (outer.fail_marker and outer.fail_marker in text):
                    return self._send(500, {"error": "boom"})
                content = (f"转写了 {images} 张图" if outer.mode == "plain"
                           else json.dumps({"text": f"转写了 {images} 张图", "confidence": 0.9}, ensure_ascii=False))
                self._send(200, {"model": body["model"], "choices": [{"message": {"content": content}}]})

            def _send(self, code, obj):
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()
```

`tests/test_ocr_client.py`:
```python
import json

import pytest

from scan_answer.ocr.client import OcrClient, OcrError, _parse
from scan_answer.ocr.config import OcrConfig, load_ocr_config, save_ocr_config

from ocr_mock import MockOcrServer


def cfg(url, **kw):
    return OcrConfig(base_url=url, api_key="sk-test", model="mock-vl", **kw)


def test_parse_json_and_plain():
    assert _parse('```json\n{"text": "你好", "confidence": 0.8}\n```') == ("你好", 0.8)
    assert _parse('{"text": "a", "confidence": 7}') == ("a", None)  # 越界置信度丢弃
    assert _parse("  直接文本 ") == ("直接文本", None)


def test_client_success_retry_and_auth():
    with MockOcrServer("ok") as srv:
        t = OcrClient(cfg(srv.url)).transcribe([b"x", b"y"], "提示")
        assert (t.text, t.confidence, t.model) == ("转写了 2 张图", 0.9, "mock-vl")
        assert srv.requests[0]["auth"] == "Bearer sk-test"
        assert srv.requests[0]["body"]["messages"][1]["content"][1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    with MockOcrServer("fail_first", fail_times=2) as srv:
        t = OcrClient(cfg(srv.url, max_retries=2), sleep=lambda s: None).transcribe([b"x"], "提示")
        assert t.text == "转写了 1 张图" and srv.calls == 3
    with MockOcrServer("auth") as srv:
        with pytest.raises(OcrError):
            OcrClient(cfg(srv.url, max_retries=3), sleep=lambda s: None).transcribe([b"x"], "提示")
        assert srv.calls == 1  # 401 不重试
    with MockOcrServer("slow", delay=1.0) as srv:
        with pytest.raises(OcrError):
            OcrClient(cfg(srv.url, timeout_s=0.2, max_retries=0)).transcribe([b"x"], "提示")


def test_config_roundtrip_and_validation(tmp_path):
    c = cfg("https://example.com/v1", concurrency=2)
    save_ocr_config(c, tmp_path / "c.json")
    assert load_ocr_config(tmp_path / "c.json") == c
    (tmp_path / "bad.json").write_text(json.dumps({"base_url": "ftp://x", "api_key": "", "model": "m"}))
    with pytest.raises(ValueError):
        load_ocr_config(tmp_path / "bad.json")
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_ocr_client.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.ocr'`）

- [ ] **Step 3: 实现**

`src/scan_answer/ocr/config.py`:
```python
"""手写转写配置（兼容 OpenAI 格式的接口）。界面的“设置”页（计划 3）读写同一个 JSON 文件。"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class OcrConfig:
    base_url: str  # 如 https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key: str
    model: str  # 如 qwen-vl-max
    concurrency: int = 4
    timeout_s: float = 60.0
    max_retries: int = 2

    def validate(self) -> None:
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("base_url 必须以 http:// 或 https:// 开头")
        if not self.model:
            raise ValueError("model 不能为空")
        if self.concurrency < 1:
            raise ValueError("concurrency 至少为 1")


def load_ocr_config(path: str | Path) -> OcrConfig:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    known = {k: d[k] for k in OcrConfig.__dataclass_fields__ if k in d}
    cfg = OcrConfig(**known)
    cfg.validate()
    return cfg


def save_ocr_config(cfg: OcrConfig, path: str | Path) -> None:
    Path(path).write_text(json.dumps(asdict(cfg), ensure_ascii=False, indent=1), encoding="utf-8")
```

`src/scan_answer/ocr/prompts.py`:
```python
"""转写提示词：按题目内容类型切换。要求模型只输出 JSON，便于解析置信度。"""
from __future__ import annotations

SYSTEM = ("你是答题卡手写内容转写助手。只转写图片中考生手写的内容，逐字照录：不改正错别字、不补全、不解释、不评价；"
          "印刷的题号、格线、栏目名称不要转写；看不清的字用□代替。"
          '只输出一个 JSON 对象：{"text": "转写内容", "confidence": 0到1之间的小数，表示你对整体转写准确度的把握}。')

CONTENT = {
    "zh": "以下是一道题的中文作答区域图片，可能有多张，按顺序拼接即为完整作答。请转写全部手写文字，保留分段（用换行表示）。",
    "en": "以下是一道题的英文作答区域图片，可能有多张，按顺序拼接即为完整作答。请按原拼写转写，保留换行。",
    "math": "以下是一道题的数学作答区域图片，可能有多张，按顺序拼接即为完整作答。文字照录，公式用 LaTeX 表示（行内用 $...$，独立公式用 $$...$$）。",
}


def question_instruction(content: str) -> str:
    return CONTENT.get(content, CONTENT["zh"])


def field_instruction(label: str) -> str:
    return f"这是答题卡上“{label}”栏的图片，只输出考生填写的内容，不要输出栏目名称本身。"
```

`src/scan_answer/ocr/client.py`:
```python
"""兼容 OpenAI Chat Completions 格式的视觉模型客户端（只用标准库，不增加依赖）。"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .config import OcrConfig
from .prompts import SYSTEM


class OcrError(Exception):
    pass


@dataclass
class Transcription:
    text: str
    confidence: float | None
    model: str


def _parse(content: str) -> tuple[str, float | None]:
    """优先解析 {"text": ..., "confidence": ...}；模型没按格式输出时把全文当作转写结果。"""
    m = re.search(r"\{.*\}", content, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict) and isinstance(d.get("text"), str):
                c = d.get("confidence")
                conf = float(c) if isinstance(c, (int, float)) and 0 <= float(c) <= 1 else None
                return d["text"], conf
        except json.JSONDecodeError:
            pass
    return content.strip(), None


class OcrClient:
    def __init__(self, cfg: OcrConfig, sleep=time.sleep):
        self.cfg = cfg
        self._sleep = sleep

    def _post(self, payload: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.cfg.api_key}"})
        with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def transcribe(self, jpegs: list[bytes], instruction: str) -> Transcription:
        content = [{"type": "text", "text": instruction}]
        content += [{"type": "image_url",
                     "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(b).decode("ascii")}}
                    for b in jpegs]
        payload = {"model": self.cfg.model, "temperature": 0,
                   "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}]}
        last: Exception | None = None
        for attempt in range(self.cfg.max_retries + 1):
            try:
                data = self._post(payload)
                text, conf = _parse(data["choices"][0]["message"]["content"])
                return Transcription(text, conf, data.get("model") or self.cfg.model)
            except urllib.error.HTTPError as e:
                last = e
                # 4xx（限流 429 除外）是请求或鉴权问题，重试无意义
                if 400 <= e.code < 500 and e.code != 429:
                    break
            except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
                last = e
            if attempt < self.cfg.max_retries:
                self._sleep(0.5 * (2 ** attempt))
        raise OcrError(f"转写失败: {last}")

    def ping(self) -> str:
        """“测试连接”：发一条纯文本请求，返回模型名。"""
        data = self._post({"model": self.cfg.model, "max_tokens": 1,
                           "messages": [{"role": "user", "content": "ping"}]})
        return data.get("model") or self.cfg.model
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_ocr_client.py -q`
Expected: `3 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/ocr tests/ocr_mock.py tests/test_ocr_client.py
git commit -m "feat(ocr): OpenAI 兼容的手写转写客户端（重试、超时、JSON 解析）与配置"
```

---

### Task 7: 转写队列，批量识别改为基于流水线

**Files:**
- Create: `src/scan_answer/ocr/queue.py`
- Modify: `src/scan_answer/batch.py`（整文件替换为基于 `BatchPipeline` 的实现）
- Test: `tests/test_ocr_queue.py`（`tests/test_batch.py`、`tests/test_cli.py` 不改，必须继续通过）

**Interfaces:**
- Consumes: Task 1 `read_json / update_json`；Task 5 `BatchSession / BatchPipeline / OcrProgress`；Task 6 `OcrClient / OcrError / OcrConfig / prompts`
- Produces:
  - `OcrQueue(cfg, listener=None, client=None)`：`enqueue(result_path) -> int`（投递一个 `result.json` 中所有 `pending` 项，返回项数）、`wait()`、`close()`；属性 `total / done / failed`；每完成一项发 `OcrProgress`
  - 每项成功：`text / ocr_status="done" / ocr_model / ocr_confidence`；失败：`ocr_status="failed"`，追加 `OCR_FAILED` 警告（同一 field 只保留一条）
  - `reset_failed(result_path) -> int`（failed → pending，并清除 OCR_FAILED 警告）、`enable_ocr(result_path) -> int`（none → pending）
  - `run_batch(template_dir, input_dir, out_root, *, exam=None, batch_id=None, scanner="folder", workers=0, ocr_config=None, listener=None) -> Path`：创建会话 → 流水线处理 `FolderSource` → `finalize()` → 若有转写则等待完成

- [ ] **Step 1: 写失败测试**

`tests/test_ocr_queue.py`:
```python
from scan_answer.batch import run_batch
from scan_answer.ocr.config import OcrConfig
from scan_answer.ocr.queue import OcrQueue, enable_ocr, reset_failed
from scan_answer.output.jsonfile import read_json
from scan_answer.pipeline.events import OcrProgress
from scan_answer.synth.generate import generate_dataset

from conftest import TEMPLATE_DIR
from ocr_mock import MockOcrServer


def cfg(url, **kw):
    return OcrConfig(base_url=url, api_key="sk-test", model="mock-vl", **kw)


def test_run_batch_with_ocr_end_to_end(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 3, seed=91)
    events = []
    # 作文 57 两个区域：提示词相同；让信息栏（提示词含“栏”）全部失败
    with MockOcrServer("ok", fail_marker="栏的图片") as srv:
        bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b",
                       ocr_config=cfg(srv.url, max_retries=0), listener=events.append)
    m = read_json(bd / "manifest.json")
    assert m["ocr"] is True and m["finalized"] is True
    ok = [x for x in m["sheets"] if x["status"] == "ok"]
    for x in ok:
        r = read_json(bd / x["result"])
        for q in r["subjective"]:
            assert q["ocr_status"] == "done" and q["text"] == f"转写了 {len(q['images'])} 张图"
            assert q["ocr_model"] == "mock-vl" and q["ocr_confidence"] == 0.9
        for f in r["student"]["fields"].values():
            assert f["ocr_status"] == "failed" and f["text"] is None
        assert sum(w["code"] == "OCR_FAILED" for w in r["warnings"]) == len(r["student"]["fields"])
    prog = [e for e in events if isinstance(e, OcrProgress)]
    per_sheet = len(t.subjective) + len(t.fields)
    assert prog[-1] == OcrProgress(done=len(ok) * len(t.subjective), failed=len(ok) * len(t.fields),
                                   total=len(ok) * per_sheet)

    # 重试失败项：服务恢复后全部成功，OCR_FAILED 警告被清除
    with MockOcrServer("ok") as srv:
        q = OcrQueue(cfg(srv.url))
        for x in ok:
            assert reset_failed(bd / x["result"]) == len(t.fields)
            q.enqueue(bd / x["result"])
        q.wait()
        q.close()
    r = read_json(bd / ok[0]["result"])
    assert all(f["ocr_status"] == "done" for f in r["student"]["fields"].values())
    assert not any(w["code"] == "OCR_FAILED" for w in r["warnings"])


def test_enable_ocr_after_the_fact(tpl_bases, tmp_path):
    t, bases = tpl_bases
    generate_dataset(t, bases, tmp_path / "ds", 1, seed=92)
    bd = run_batch(TEMPLATE_DIR, tmp_path / "ds", tmp_path / "out", batch_id="b")
    x = next(s for s in read_json(bd / "manifest.json")["sheets"] if s["status"] == "ok")
    r = read_json(bd / x["result"])
    assert all(q["ocr_status"] == "none" for q in r["subjective"])
    assert enable_ocr(bd / x["result"]) == len(t.subjective) + len(t.fields)
    with MockOcrServer("plain") as srv:
        q = OcrQueue(cfg(srv.url))
        q.enqueue(bd / x["result"])
        q.wait()
        q.close()
    r = read_json(bd / x["result"])
    assert r["subjective"][0]["text"].startswith("转写了") and r["subjective"][0]["ocr_confidence"] is None
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_ocr_queue.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'scan_answer.ocr.queue'`）

- [ ] **Step 3: 实现**

`src/scan_answer/ocr/queue.py`:
```python
"""后台手写转写队列：对 result.json 中 ocr_status 为 pending 的主观题和信息栏逐项转写并回写。

每项转写完成后单独加锁回写（与“结束批次重新判定”互不覆盖）；扫描与识别不等待转写。
"""
from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..output.jsonfile import read_json, update_json
from ..pipeline.events import OcrProgress
from .client import OcrClient, OcrError
from .config import OcrConfig
from .prompts import field_instruction, question_instruction


def _tasks(result: dict) -> list[tuple[str, str, list[str], str]]:
    """列出待转写项：(类别, 键, 图片相对路径, 提示词)。"""
    out = []
    for q in result.get("subjective", []):
        if q.get("ocr_status") == "pending":
            out.append(("subjective", q["no"], q["images"], question_instruction(q.get("content", "zh"))))
    for key, f in result["student"].get("fields", {}).items():
        if f.get("ocr_status") == "pending":
            out.append(("field", key, [f["image"]], field_instruction(f.get("label", key))))
    return out


def _apply(result: dict, kind: str, key: str, text: str | None, status: str, model: str | None,
           confidence: float | None, error: str | None) -> None:
    if kind == "subjective":
        q = next(q for q in result["subjective"] if q["no"] == key)
        q.update({"text": text, "ocr_status": status, "ocr_model": model, "ocr_confidence": confidence})
        field = f"subjective.{key}"
    else:
        f = result["student"]["fields"][key]
        f.update({"text": text, "ocr_status": status})
        field = f"student.fields.{key}"
    result["warnings"] = [w for w in result["warnings"]
                          if not (w.get("code") == "OCR_FAILED" and w.get("field") == field)]
    if status == "failed":
        result["warnings"].append({"code": "OCR_FAILED", "field": field, "msg": f"手写转写失败：{error}"})


class OcrQueue:
    def __init__(self, cfg: OcrConfig, listener: Callable[[object], None] | None = None,
                 client: OcrClient | None = None):
        self.client = client or OcrClient(cfg)
        self.listener = listener or (lambda e: None)
        self._pool = ThreadPoolExecutor(cfg.concurrency, thread_name_prefix="ocr")
        self._lock = threading.Lock()
        self._futures: list[Future] = []
        self.total = self.done = self.failed = 0

    def _progress(self) -> None:
        self.listener(OcrProgress(self.done, self.failed, self.total))

    def _run(self, result_path: Path, kind: str, key: str, images: list[str], instruction: str) -> None:
        try:
            jpegs = [(result_path.parent / rel).read_bytes() for rel in images]
            t = self.client.transcribe(jpegs, instruction)
            update_json(result_path, lambda r: _apply(r, kind, key, t.text, "done", t.model, t.confidence, None))
            ok = True
        except (OcrError, OSError) as e:
            update_json(result_path, lambda r: _apply(r, kind, key, None, "failed", None, None, str(e)))
            ok = False
        with self._lock:
            self.done += int(ok)
            self.failed += int(not ok)
        self._progress()

    def enqueue(self, result_path: str | Path) -> int:
        """投递一个学生的 result.json，返回新增的待转写项数。"""
        result_path = Path(result_path)
        tasks = _tasks(read_json(result_path))
        with self._lock:
            self.total += len(tasks)
            for kind, key, images, instruction in tasks:
                self._futures.append(self._pool.submit(self._run, result_path, kind, key, images, instruction))
        if tasks:
            self._progress()
        return len(tasks)

    def wait(self) -> None:
        """等待已投递的全部转写完成。"""
        while True:
            with self._lock:
                pending = [f for f in self._futures if not f.done()]
            if not pending:
                return
            for f in pending:
                f.result()

    def close(self) -> None:
        self._pool.shutdown(wait=True)


def reset_failed(result_path: str | Path) -> int:
    """把 failed 的转写项改回 pending（命令行 `ocr --retry-failed` 用），返回改动项数。"""
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for q in r.get("subjective", []):
            if q.get("ocr_status") == "failed":
                q["ocr_status"] = "pending"
                count += 1
        for f in r["student"].get("fields", {}).values():
            if f.get("ocr_status") == "failed":
                f["ocr_status"] = "pending"
                count += 1
        r["warnings"] = [w for w in r["warnings"] if w.get("code") != "OCR_FAILED"]

    update_json(result_path, fix)
    return count


def enable_ocr(result_path: str | Path) -> int:
    """把 ocr_status 为 none 的项置为 pending（批次识别时未配置转写，事后补做），返回改动项数。"""
    count = 0

    def fix(r: dict) -> None:
        nonlocal count
        for q in r.get("subjective", []):
            if q.get("ocr_status") == "none":
                q["ocr_status"] = "pending"
                count += 1
        for f in r["student"].get("fields", {}).values():
            if f.get("ocr_status") == "none":
                f["ocr_status"] = "pending"
                count += 1

    update_json(result_path, fix)
    return count
```

`src/scan_answer/batch.py`（整文件替换）:
```python
"""文件夹批量识别：FolderSource + 流水线 + 结束批次重新判定（+ 可选手写转写）。

命令行 `scan-answer recognize` 与界面的“导入图片”都走这里；扫描仪入口（计划 4）直接使用 BatchPipeline。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .ocr.config import OcrConfig
from .ocr.queue import OcrQueue
from .pipeline.runner import BatchPipeline
from .pipeline.session import BatchSession
from .source.folder import FolderSource, choose_dpi, list_images, pair_images, read_dpi, source_name  # noqa: F401

__all__ = ["run_batch", "choose_dpi", "list_images", "pair_images", "read_dpi", "source_name"]


def run_batch(template_dir: str | Path, input_dir: str | Path, out_root: str | Path, *,
              exam: dict | None = None, batch_id: str | None = None, scanner: str = "folder",
              workers: int = 0, ocr_config: OcrConfig | None = None,
              listener: Callable[[object], None] | None = None) -> Path:
    session = BatchSession.create(out_root, template_dir, batch_id=batch_id, exam=exam, scanner=scanner,
                                  ocr=ocr_config is not None)
    ocr = OcrQueue(ocr_config, listener) if ocr_config else None
    try:
        with BatchPipeline(session, workers=workers, listener=listener,
                           on_result=ocr.enqueue if ocr else None) as pipe:
            pipe.run(FolderSource(input_dir, session.faces))
            pipe.finalize()
        if ocr:
            ocr.wait()
    finally:
        if ocr:
            ocr.close()
    return session.batch_dir
```

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest tests/test_ocr_queue.py tests/test_batch.py tests/test_cli.py -q`
Expected: `16 passed`

- [ ] **Step 5: Commit**

```bash
git add src/scan_answer/ocr/queue.py src/scan_answer/batch.py tests/test_ocr_queue.py
git commit -m "feat(ocr): 后台转写队列；批量识别改为基于流水线（实时阈值 + 结束批次重新判定）"
```

---

### Task 8: 命令行与文档

**Files:**
- Modify: `src/scan_answer/cli.py`（整文件替换）、`docs/output-format.md`（整文件替换）
- Test: `tests/test_cli.py`（整文件替换：保留原 2 个用例，新增 2 个）

**Interfaces:**
- Consumes: `run_batch`、`load_ocr_config`、`OcrQueue / enable_ocr / reset_failed`、事件类
- Produces:
  - `scan-answer recognize ... [--workers N] [--ocr-config 文件]`（`--workers` 默认 `default_workers() = max(0, min(4, cpu_count-1))`）；运行时在 stderr 打印每张卡的进度、结束批次与转写汇总
  - `scan-answer ocr --batch 目录 --config 文件 [--retry-failed] [--enable]`：对批次中所有成功卡投递转写并等待；有失败项返回 1，否则 0
  - `docs/output-format.md` 增加 `state/`、manifest 字段说明、`OCR_FAILED`、手写转写说明

- [ ] **Step 1: 写失败测试**

`tests/test_cli.py`（整文件替换）:
```python
from scan_answer.cli import main

from conftest import TEMPLATE_DIR


def test_cli_synth_recognize_eval(tmp_path):
    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "4", "--seed", "5"]) == 0
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                 "--out", str(tmp_path / "out"), "--batch-id", "b"]) == 0
    assert main(["eval", "--truth", str(tmp_path / "ds"), "--batch", str(tmp_path / "out" / "b")]) == 0


def test_cli_recognize_bad_exam(tmp_path):
    # 非法 JSON 字符串
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path),
                 "--out", str(tmp_path / "o"), "--exam", "{bad"]) == 2
    # 合法 JSON 但不是对象
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path),
                 "--out", str(tmp_path / "o"), "--exam", "[1]"]) == 2


def test_cli_recognize_with_workers_and_ocr(tmp_path):
    import json as _json

    from ocr_mock import MockOcrServer

    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "3", "--seed", "6"]) == 0
    with MockOcrServer("ok", fail_marker="栏的图片") as srv:
        cfg = tmp_path / "ocr.json"
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m", "max_retries": 0}))
        assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                     "--out", str(tmp_path / "out"), "--batch-id", "b", "--workers", "2", "--ocr-config", str(cfg)]) == 0
        # 识别时信息栏转写全部失败；不加 --retry-failed 时没有待转写项，返回 0
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg)]) == 0  # 无待转写项
    with MockOcrServer("ok") as srv:
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m"}))
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg), "--retry-failed"]) == 0
    assert main(["eval", "--truth", str(tmp_path / "ds"), "--batch", str(tmp_path / "out" / "b")]) == 0


def test_cli_ocr_failure_exit_code(tmp_path):
    import json as _json

    from ocr_mock import MockOcrServer

    assert main(["synth", "--template", str(TEMPLATE_DIR), "--out", str(tmp_path / "ds"), "--count", "1", "--seed", "7"]) == 0
    assert main(["recognize", "--template", str(TEMPLATE_DIR), "--input", str(tmp_path / "ds"),
                 "--out", str(tmp_path / "out"), "--batch-id", "b", "--workers", "0"]) == 0
    with MockOcrServer("error") as srv:
        cfg = tmp_path / "ocr.json"
        cfg.write_text(_json.dumps({"base_url": srv.url, "api_key": "k", "model": "m", "max_retries": 0}))
        assert main(["ocr", "--batch", str(tmp_path / "out" / "b"), "--config", str(cfg), "--enable"]) == 1
```

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: FAIL（新增的 2 个用例失败：`unrecognized arguments: --workers` 等）

- [ ] **Step 3: 实现**

`src/scan_answer/cli.py`（整文件替换）:
```python
"""命令行入口：scan-answer synth | recognize | ocr | eval"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .batch import run_batch
from .evaluate import evaluate_dirs
from .ocr.config import load_ocr_config
from .ocr.queue import OcrQueue, enable_ocr, reset_failed
from .pipeline.events import FinalizedEvent, OcrProgress, SheetEvent
from .synth.generate import generate_dataset
from .template import load_template

GATES = {"normal_accuracy": 0.999, "objective_accuracy": 0.999, "exam_digit_accuracy": 0.999,
         "physical_detect_rate": 1.0}


def default_workers() -> int:
    return max(0, min(4, (os.cpu_count() or 2) - 1))


def _print_event(e: object) -> None:
    if isinstance(e, SheetEvent):
        state = "成功" if e.ok else f"物理异常 {e.physical}"
        print(f"#{e.seq:04d} {e.source} {state} 警告 {e.warnings}", file=sys.stderr)
    elif isinstance(e, FinalizedEvent):
        print(f"结束批次：最终阈值 {e.mark:.3f}，重新判定后变化 {e.changed} 张", file=sys.stderr)
    elif isinstance(e, OcrProgress) and e.done + e.failed == e.total:
        print(f"手写转写：完成 {e.done}，失败 {e.failed}，共 {e.total}", file=sys.stderr)


def _run_ocr(batch: Path, cfg, retry_failed: bool, enable: bool) -> int:
    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    paths = [batch / s["result"] for s in manifest["sheets"] if s.get("status") == "ok" and s.get("result")]
    q = OcrQueue(cfg, _print_event)
    try:
        for p in paths:
            if retry_failed:
                reset_failed(p)
            if enable:
                enable_ocr(p)
            q.enqueue(p)
        q.wait()
    finally:
        q.close()
    print(f"手写转写：完成 {q.done}，失败 {q.failed}，共 {q.total}")
    return 1 if q.failed else 0


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
    r.add_argument("--workers", type=int, default=default_workers(), help="识别进程数，0 表示在当前进程内顺序执行")
    r.add_argument("--ocr-config", help="手写转写配置 JSON 文件；不提供则只切图")

    o = sub.add_parser("ocr", help="对已识别批次执行（或重试）手写转写")
    o.add_argument("--batch", required=True)
    o.add_argument("--config", required=True)
    o.add_argument("--retry-failed", action="store_true", help="把失败的转写项改回待转写后重试")
    o.add_argument("--enable", action="store_true", help="批次识别时未开启转写，现在补做")

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
        if a.exam:
            try:
                exam = json.loads(a.exam)
            except json.JSONDecodeError as exc:
                print(f"--exam 不是合法的 JSON 对象: {exc}", file=sys.stderr)
                return 2
            if not isinstance(exam, dict):
                print(f"--exam 不是合法的 JSON 对象: 期望对象，得到 {type(exam).__name__}", file=sys.stderr)
                return 2
        else:
            exam = {}
        cfg = load_ocr_config(a.ocr_config) if a.ocr_config else None
        bd = run_batch(a.template, a.input, a.out, exam=exam, batch_id=a.batch_id, workers=a.workers,
                       ocr_config=cfg, listener=_print_event)
        print(bd)
        return 0
    if a.cmd == "ocr":
        return _run_ocr(Path(a.batch), load_ocr_config(a.config), a.retry_failed, a.enable)
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

`docs/output-format.md`（整文件替换）:
````markdown
# 输出格式（schema_version 1.0）

## 目录结构

```
<输出根目录>/<batch_id>/
  template.json                       # 本批次使用的模板快照
  manifest.json                       # 批次汇总（扫描中约每 2 秒更新一次）
  raw/{seq:04d}_{原文件名}             # 原始扫描图副本
  state/{seq:04d}.json                # 识别中间量（涂黑度、对齐矩阵），结束批次重新判定时使用
  students/{seq:04d}_{考号}/           # 考号识别不完整时只用 {seq:04d}；目录名在首次识别时确定，之后不改
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
| `ocr_status` | `none`（未开启转写）/ `pending`（等待转写）/ `done` / `failed` |
| `scan.faces.*.align_method` | `anchors` / `frames` / `features` |

## 物理异常代码（不生成 result.json，写入 errors/）

| 代码 | 含义 | 现场处理 |
|---|---|---|
| `ALIGN_FAILED` | 所有面都无法定位，或图片文件无法读取 | 检查折角、放反，重扫 |
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
| `OCR_FAILED` | 手写转写失败（可用 `scan-answer ocr --retry-failed` 重试） | `subjective.{题号}` 或 `student.fields.{键}` |

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

## manifest.json

| 字段 | 说明 |
|---|---|
| `batch_id`、`exam`、`template`、`scanner`、`created` | 批次信息 |
| `ocr` | 本批次是否开启手写转写 |
| `thresholds` | 当前判定阈值 `{"mark", "uncertain"}`：扫描中先用默认值，成功识别满 100 张后改用已扫部分的拟合值（之后每 50 张更新）；结束批次后为整批拟合的最终值 |
| `finalized` | 是否已结束批次。结束批次时用最终阈值逐张重新判定，判定有变化的卡会重写 `result.json`（保留已有转写结果）；未结束的批次中个别临界题的状态以后可能变化 |
| `counts` | `total`、`ok`、`physical`、`voided`、`warnings` |
| `events[]` | 扫描仪事件：`time`、`kind`（`double_feed` / `paper_jam` / `no_paper` / `error`）、`message`、`after_seq`（事件发生时已分配的最大序号） |
| `sheets[]` | 每张卡：`seq`、`source`、`files`、`status`（`processing` / `ok` / `physical` / `voided`）；成功时有 `result`、`exam_no`、`warnings`；物理异常时有 `physical`、`message`；作废时 `voided: true` |

## 手写转写

- 识别时提供转写配置（`recognize --ocr-config`）则主观题与信息栏的 `ocr_status` 为 `pending`，后台转写完成后回写 `text`、`ocr_model`、`ocr_confidence`（模型自评 0~1，模型未给出时为 `null`）。
- 配置文件（JSON）：`{"base_url": "https://.../v1", "api_key": "...", "model": "...", "concurrency": 4, "timeout_s": 60, "max_retries": 2}`，接口为 OpenAI Chat Completions 兼容格式。
- 事后补做或重试：`scan-answer ocr --batch <批次目录> --config <配置> [--enable] [--retry-failed]`；有失败项时退出码为 1。
````

- [ ] **Step 4: 运行命令行测试与全部快速测试**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: `4 passed`

Run: `.venv/bin/python -m pytest -q`
Expected: `80 passed, 1 deselected`

- [ ] **Step 5: 运行完整回归（约 4 分钟）**

Run: `.venv/bin/python -m pytest -m slow -q`
Expected: `1 passed`

- [ ] **Step 6: 手工验证多进程速度**

```bash
.venv/bin/scan-answer synth --template templates/guanzong199 --out /tmp/sa2_ds --count 60 --seed 3
time .venv/bin/scan-answer recognize --template templates/guanzong199 --input /tmp/sa2_ds --out /tmp/sa2_out --batch-id w0 --workers 0
time .venv/bin/scan-answer recognize --template templates/guanzong199 --input /tmp/sa2_ds --out /tmp/sa2_out --batch-id w4 --workers 4
.venv/bin/scan-answer eval --truth /tmp/sa2_ds --batch /tmp/sa2_out/w4
```
Expected: 两次都成功，`--workers 4` 明显更快（原型实测 60 张：单进程 28 秒，4 进程 11 秒，约 654 面/分钟）；`eval` 输出“全部达标”。把实测时间写进报告。

- [ ] **Step 7: Commit**

```bash
git add src/scan_answer/cli.py docs/output-format.md tests/test_cli.py
git commit -m "feat(cli): recognize 支持多进程与手写转写，新增 ocr 子命令；更新输出文档"
```

---

## 与设计文档的对应与偏差

| 设计文档 | 本计划 |
|---|---|
| 3 架构：图像源 / 流水线 / 识别进程池 / OCR 队列 | 全部实现；界面层（Qt 信号）在计划 3 中订阅本计划的事件对象 |
| 5.1 OMR 批次自适应阈值 | 按用户决策改为：扫描中默认值 → 满 100 张拟合 → 结束批次整批拟合并重新判定 |
| 5.4 物理异常：重扫（沿用序号）/ 作废 | 实现 `rescan` / `void`；“停机时把卡放回进纸口继续”是扫描仪行为，计划 4 的 TwainSource 实现，本计划只记录 `DeviceEvent` |
| 5.4 扫描仪上报的双张、卡纸 | 本计划定义事件并写入 manifest `events`；真实上报在计划 4 |
| 6.3 manifest | 增加 `scanner / ocr / created / finalized / events / counts.voided`、每张卡的 `status` |
| 7 手写转写 | 全部实现；未实现“界面上手动单项重试”（命令行 `--retry-failed` 按批次重试，界面单项重试在计划 3） |
| 4.3 定位兜底链“借用正面推算” | 仍顺延到计划 4（需要真实扫描仪双面几何数据） |
| 程序崩溃后继续批次 | 新增 `BatchSession.open`（设计文档未写明，扫描现场需要） |

原型实测：快速测试 80 个、200 张回归全部通过；60 张模拟卡单进程 28 秒、4 进程 11 秒。
