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
