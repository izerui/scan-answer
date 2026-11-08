"""模板 JSON 读写。模板目录结构：template.json + 各面空白底图。"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ..imageio import imread_gray, imwrite
from .model import (AlignSpec, BarcodeSpec, ChooseOneSpec, ExamNoSpec, Face, FieldSpec, ObjectiveBlock,
                    OmrFieldSpec, OmrGrid, Region, SubjectiveSpec, SyncSpec, Template)

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
        objective=[ObjectiveBlock(name=o["name"], qtype=o["qtype"], start=int(o["start"]),
                                  grid=_grid(o["grid"]), group=int(o.get("group", 0)))
                   for o in d.get("objective", [])],
        subjective=[SubjectiveSpec(no=s["no"], qtype=s["qtype"], content=s["content"],
                                   regions=[Region(face=r["face"], rect=_rect(r["rect"])) for r in s["regions"]],
                                   sub=str(s.get("sub") or ""))
                    for s in d.get("subjective", [])],
        ignore=[Region(face=r["face"], rect=_rect(r["rect"])) for r in d.get("ignore", [])],
        sync=[SyncSpec(face=s["face"], direction=s["direction"], marks=[_rect(m) for m in s["marks"]])
              for s in d.get("sync", [])],
        omr_fields=[OmrFieldSpec(key=f["key"], label=f["label"], grid=_grid(f["grid"]))
                    for f in d.get("omr_fields", [])],
        choose_one=[ChooseOneSpec(key=c["key"], label=c["label"], face=c["face"],
                                  questions=list(c["questions"]), cells=[_rect(r) for r in c["cells"]])
                    for c in d.get("choose_one", [])],
    )


def template_to_dict(t: Template) -> dict:
    return asdict(t)


def load_template(directory: str | Path) -> tuple[Template, dict[str, np.ndarray]]:
    """读取模板及各面空白底图（灰度，dpi = template.dpi）。"""
    directory = Path(directory)
    t = template_from_dict(json.loads((directory / "template.json").read_text(encoding="utf-8")))
    bases = {}
    for name, face in t.faces.items():
        img = imread_gray(directory / face.image)
        if img is None:
            raise FileNotFoundError(directory / face.image)
        bases[name] = img
    return t, bases


def save_template(t: Template, directory: str | Path, bases: dict[str, np.ndarray] | None = None) -> None:
    """保存模板到目录。给了 bases 时，先把各面底图写成 PNG（临时文件再原子替换）。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # 写底图
    if bases is not None:
        import tempfile
        for name, img in bases.items():
            final_img = directory / f"{name}.png"
            # 编码要求文件扩展名为图片格式，用 .png 临时文件后原子替换
            fd, tmp_path = tempfile.mkstemp(suffix=".png", dir=str(directory))
            os.close(fd)
            if not imwrite(tmp_path, img):
                os.unlink(tmp_path)
                raise OSError(f"底图写入失败：{final_img}")
            os.replace(tmp_path, final_img)
    # 写 JSON
    final = directory / "template.json"
    tmp = directory / "template.json.tmp"
    tmp.write_text(json.dumps(template_to_dict(t), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, final)
