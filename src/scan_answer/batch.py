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
