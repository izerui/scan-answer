"""模板 JSON 读写。模板目录结构：template.json + 各面空白底图。"""
from __future__ import annotations

import json
import os
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
    final = directory / "template.json"
    tmp = directory / "template.json.tmp"
    tmp.write_text(json.dumps(template_to_dict(t), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, final)
