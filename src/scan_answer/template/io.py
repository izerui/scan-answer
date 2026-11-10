"""模板 JSON 读写。模板目录结构：template.json + 各面空白底图。"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path, PureWindowsPath

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
        identity=[Region(face=r["face"], rect=_rect(r["rect"])) for r in d.get("identity", [])],
    )


def template_to_dict(t: Template) -> dict:
    return asdict(t)


def _resource_path(directory: Path, reference: str) -> Path:
    """只接受模板目录内的相对资源路径，包括解析符号链接后的路径。"""
    if not isinstance(reference, str) or not reference.strip() or "\x00" in reference:
        raise ValueError(f"底图资源路径无效：{reference!r}")
    relative = Path(reference)
    if (relative.is_absolute() or PureWindowsPath(reference).drive
            or "\\" in reference or ":" in reference
            or ".." in relative.parts or not relative.parts):
        raise ValueError(f"底图资源路径必须位于模板目录内：{reference!r}")
    root = directory.resolve()
    try:
        resolved = (root / relative).resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"底图资源路径无效：{reference!r}") from exc
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError(f"底图资源路径越出模板目录：{reference!r}")
    return root / relative


def load_template(directory: str | Path) -> tuple[Template, dict[str, np.ndarray]]:
    """读取模板及各面空白底图（灰度，dpi = template.dpi）。"""
    directory = Path(directory)
    t = template_from_dict(json.loads((directory / "template.json").read_text(encoding="utf-8")))
    paths = {name: _resource_path(directory, face.image) for name, face in t.faces.items()}
    bases = {}
    for name, path in paths.items():
        img = imread_gray(path)
        if img is None:
            raise FileNotFoundError(path)
        bases[name] = img
    return t, bases


def _save_resource(directory: Path, name: str, image: np.ndarray) -> str:
    fd, temporary = tempfile.mkstemp(prefix=".base-", suffix=".png", dir=directory)
    os.close(fd)
    tmp = Path(temporary)
    try:
        if not imwrite(tmp, image):
            raise OSError(f"底图写入失败：{name}")
        data = tmp.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        reference = f"{name}-{digest}.png"
        final = _resource_path(directory, reference)
        # 排他发布，不覆盖已被任何版本 JSON 引用的资源。
        try:
            try:
                os.link(tmp, final)
            except FileExistsError:
                raise
            except OSError:
                # exFAT / FAT / 部分网络盘不支持硬链接：退回排他创建并写入
                _write_exclusive(final, data)
        except FileExistsError:
            if final.read_bytes() != data:
                raise OSError(f"底图摘要资源内容冲突：{reference}")
        return reference
    finally:
        tmp.unlink(missing_ok=True)


def _write_exclusive(path: Path, data: bytes) -> None:
    """以排他方式新建文件并写入（已存在时抛 FileExistsError）；写入失败删除半成品。"""
    with open(path, "xb") as stream:  # "x"：目标已存在时失败，不会覆盖
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            path.unlink(missing_ok=True)
            raise


def save_template(t: Template, directory: str | Path, bases: dict[str, np.ndarray] | None = None) -> None:
    """先发布不可变底图资源，再原子替换 JSON；失败不修改已有模板与内存引用。"""
    directory = Path(directory)
    for face in t.faces.values():
        _resource_path(directory, face.image)
    if bases is not None:
        if set(bases) != set(t.faces) or not set(bases) <= {"front", "back"}:
            raise ValueError("底图必须与模板的有效面一一对应")
    directory.mkdir(parents=True, exist_ok=True)
    data = template_to_dict(t)
    references = {}
    if bases is not None:
        for name, image in bases.items():
            references[name] = _save_resource(directory, name, image)
            data["faces"][name]["image"] = references[name]
    text = json.dumps(data, ensure_ascii=False, indent=1, allow_nan=False)
    fd, temporary = tempfile.mkstemp(prefix=".template-", suffix=".tmp", dir=directory)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, directory / "template.json")
    finally:
        tmp.unlink(missing_ok=True)
    for name, reference in references.items():
        t.faces[name].image = reference
